// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {MockPyth} from "@pythnetwork/pyth-sdk-solidity/MockPyth.sol";
import {PythErrors} from "@pythnetwork/pyth-sdk-solidity/PythErrors.sol";
import {PythStructs} from "@pythnetwork/pyth-sdk-solidity/PythStructs.sol";
import {Test} from "forge-std/Test.sol";

/// @notice Checks the Pyth SDK wiring and pins down what its mock does and does not model.
contract PythSdkSmokeTest is Test {
    bytes32 internal constant FEED = keccak256("ETH/USD");
    uint256 internal constant UPDATE_FEE = 1 wei;
    uint64 internal constant NOW = 1_800_000_000;

    MockPyth internal pyth;

    function setUp() public {
        pyth = new MockPyth(60, UPDATE_FEE);
        vm.warp(NOW);
    }

    function test_parsesAnUpdateInsideTheWindow() public {
        PythStructs.PriceFeed memory feed = _parse(_update(3_000e8, NOW - 1, NOW - 3), NOW - 2, NOW + 2, UPDATE_FEE);
        assertEq(feed.price.price, 3_000e8);
        assertEq(feed.price.publishTime, NOW - 1);
    }

    function test_rejectsAnUpdateOutsideTheWindow() public {
        bytes memory stale = _update(3_000e8, NOW - 3, NOW - 4);
        vm.expectRevert(PythErrors.PriceFeedNotFoundWithinRange.selector);
        this.parseExternal(stale, NOW - 2, NOW + 2, UPDATE_FEE);
    }

    function test_requiresTheUpdateFee() public {
        bytes memory update = _update(3_000e8, NOW, NOW - 1);
        vm.expectRevert(PythErrors.InsufficientFee.selector);
        this.parseExternal(update, NOW - 2, NOW + 2, 0);
    }

    /// @dev `poke` (docs/spec.md §3) relies on the real contract (v1.4.6) accepting an update only if
    /// `minPublishTime > prevPublishTime`, which leaves one valid update per window. MockPyth 4.3.1 skips that check,
    /// so the rule must be tested with our own mock and on a fork of the real contract. This test fails, as a
    /// reminder, if a later SDK release makes the mock enforce it.
    function test_mockDoesNotEnforceUniqueness() public {
        // A later update in the window: the real contract rejects it because an earlier one already qualifies.
        PythStructs.PriceFeed memory feed = _parse(_update(3_001e8, NOW, NOW - 1), NOW - 2, NOW + 2, UPDATE_FEE);
        assertEq(feed.price.price, 3_001e8);
    }

    /// @dev External and without a return value, so that `vm.expectRevert` can wrap it.
    function parseExternal(bytes memory update, uint64 minPublishTime, uint64 maxPublishTime, uint256 fee) external {
        _parse(update, minPublishTime, maxPublishTime, fee);
    }

    function _parse(bytes memory update, uint64 minPublishTime, uint64 maxPublishTime, uint256 fee)
        internal
        returns (PythStructs.PriceFeed memory)
    {
        bytes[] memory updates = new bytes[](1);
        updates[0] = update;
        bytes32[] memory ids = new bytes32[](1);
        ids[0] = FEED;
        return pyth.parsePriceFeedUpdatesUnique{value: fee}(updates, ids, minPublishTime, maxPublishTime)[0];
    }

    function _update(int64 price, uint64 publishTime, uint64 prevPublishTime) internal view returns (bytes memory) {
        return pyth.createPriceFeedUpdateData(FEED, price, 1e6, -8, price, 1e6, publishTime, prevPublishTime);
    }
}
