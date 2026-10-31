// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IPyth} from "@pythnetwork/pyth-sdk-solidity/IPyth.sol";
import {PythErrors} from "@pythnetwork/pyth-sdk-solidity/PythErrors.sol";
import {PythStructs} from "@pythnetwork/pyth-sdk-solidity/PythStructs.sol";
import {Test, console} from "forge-std/Test.sol";

/// @notice Runs the deployed Pyth contract (v1.4.6) on a Base fork with updates that were really pushed on-chain.
/// It measures `parsePriceFeedUpdatesUnique` and checks the rule that `poke` relies on (docs/spec.md §3, invariant
/// I6): an update is accepted only if no earlier update of its feed fits the window. MockPyth does not model it.
/// @dev The vectors in `vectors/` come from `propamm pyth-pushed` in the analyzer. Each test forks Base one block
/// before its update was pushed, so it needs an endpoint that serves historical state. Set BASE_RPC_URL to one
/// (https://mainnet.base.org does; PublicNode does not without a token). Without it the tests are skipped.
contract PythUniqueForkTest is Test {
    IPyth internal constant PYTH = IPyth(0x8250f4aF4B972684F7b336503E2D6dFeDeB1487a);

    /// @dev Measured cold at the vectors' blocks: 113,170 gas for one feed (a 631-byte update) and 130,965 for two
    /// (959 bytes). The caps leave 2 % for tooling drift; more than that means the measurement changed. Pyth alone
    /// exceeds the plan's 120k budget for a whole `poke` (docs/spec.md §12).
    uint256 internal constant ONE_FEED_GAS_CAP = 115_500;
    uint256 internal constant TWO_FEEDS_GAS_CAP = 133_600;

    struct Vector {
        bytes[] updateData;
        bytes32[] ids;
        int256[] prices;
        /// @dev Latest publish time and latest previous publish time over the vector's messages.
        uint64 publishTime;
        uint64 prevPublishTime;
    }

    function test_acceptsTheFirstUpdateOfItsSecond() public {
        Vector memory v = _fork("pyth-1-feed.json");
        uint256 used = _parseAndCheck(v, v.prevPublishTime + 1, v.publishTime + 2);
        console.log("parsePriceFeedUpdatesUnique, 1 feed, %d-byte update: %d gas", v.updateData[0].length, used);
        assertLe(used, ONE_FEED_GAS_CAP);
    }

    function test_acceptsTwoFeedsFromOneUpdate() public {
        Vector memory v = _fork("pyth-2-feeds.json");
        uint256 used = _parseAndCheck(v, v.prevPublishTime + 1, v.publishTime + 2);
        console.log("parsePriceFeedUpdatesUnique, 2 feeds, %d-byte update: %d gas", v.updateData[0].length, used);
        assertLe(used, TWO_FEEDS_GAS_CAP);
    }

    /// @dev If the window reaches back to the previous update's second, that earlier update would fit it, so this one
    /// is not the unique answer and is rejected. This is what stops a keeper from choosing among several updates.
    function test_rejectsAWindowThatAnEarlierUpdateFits() public {
        Vector memory v = _fork("pyth-1-feed.json");
        vm.expectRevert(PythErrors.PriceFeedNotFoundWithinRange.selector);
        PYTH.parsePriceFeedUpdatesUnique(v.updateData, v.ids, v.prevPublishTime, v.publishTime + 2);
    }

    function test_rejectsAnUpdateOlderThanTheWindow() public {
        Vector memory v = _fork("pyth-1-feed.json");
        vm.expectRevert(PythErrors.PriceFeedNotFoundWithinRange.selector);
        PYTH.parsePriceFeedUpdatesUnique(v.updateData, v.ids, v.publishTime + 1, v.publishTime + 3);
    }

    /// @dev `parsePriceFeedUpdatesUnique` verifies but does not store, so the hook must keep its own copy of the price.
    function test_parsingStoresNothingAndCostsNoFeeOnBase() public {
        Vector memory v = _fork("pyth-1-feed.json");
        assertEq(PYTH.getUpdateFee(v.updateData), 0);
        uint256 storedBefore = PYTH.getPriceUnsafe(v.ids[0]).publishTime;
        assertLt(storedBefore, v.publishTime);
        PYTH.parsePriceFeedUpdatesUnique(v.updateData, v.ids, v.prevPublishTime + 1, v.publishTime);
        assertEq(PYTH.getPriceUnsafe(v.ids[0]).publishTime, storedBefore);
    }

    /// @dev Parses with a cold Pyth account and checks every price against the vector. Returns the gas used.
    function _parseAndCheck(Vector memory v, uint64 minPublishTime, uint64 maxPublishTime)
        internal
        returns (uint256 used)
    {
        vm.cool(address(PYTH));
        uint256 gasBefore = gasleft();
        // The update fee on Base is zero (checked in test_parsingStoresNothingAndCostsNoFeeOnBase).
        PythStructs.PriceFeed[] memory feeds =
            PYTH.parsePriceFeedUpdatesUnique(v.updateData, v.ids, minPublishTime, maxPublishTime);
        used = gasBefore - gasleft();
        for (uint256 i; i < v.ids.length; ++i) {
            assertEq(feeds[i].id, v.ids[i]);
            assertEq(feeds[i].price.price, v.prices[i]);
        }
    }

    function _fork(string memory name) internal returns (Vector memory v) {
        string memory rpc = vm.envOr("BASE_RPC_URL", string(""));
        vm.skip(bytes(rpc).length == 0, "set BASE_RPC_URL to a Base endpoint that serves historical state");
        string memory json = vm.readFile(string.concat(vm.projectRoot(), "/test/fork/vectors/", name));
        v.updateData = new bytes[](1);
        v.updateData[0] = vm.parseJsonBytes(json, ".data");
        v.ids = vm.parseJsonBytes32Array(json, ".feedIds");
        v.prices = vm.parseJsonIntArray(json, ".prices");
        uint256[] memory publishTimes = vm.parseJsonUintArray(json, ".publishTimes");
        uint256[] memory prevPublishTimes = vm.parseJsonUintArray(json, ".prevPublishTimes");
        for (uint256 i; i < publishTimes.length; ++i) {
            if (publishTimes[i] > v.publishTime) v.publishTime = uint64(publishTimes[i]);
            if (prevPublishTimes[i] > v.prevPublishTime) v.prevPublishTime = uint64(prevPublishTimes[i]);
        }
        vm.createSelectFork(rpc, vm.parseJsonUint(json, ".block") - 1);
    }
}
