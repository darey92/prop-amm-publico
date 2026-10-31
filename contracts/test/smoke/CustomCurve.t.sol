// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BaseCustomAccounting} from "@openzeppelin/uniswap-hooks/base/BaseCustomAccounting.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {Hooks} from "@uniswap/v4-core/src/libraries/Hooks.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {Deployers} from "@uniswap/v4-core/test/utils/Deployers.sol";

import {FixedPriceCurve} from "../mocks/FixedPriceCurve.sol";

/// @notice End-to-end check of the architecture: a hook that owns all the liquidity and quotes every swap itself,
/// driven through a real PoolManager and router.
contract CustomCurveSmokeTest is Deployers {
    uint256 internal constant WAD = 1e18;
    uint256 internal constant MID = 2e18; // 2 token1 per token0
    uint256 internal constant SPREAD_BPS = 10;
    uint256 internal constant SEED0 = 100 ether;
    uint256 internal constant SEED1 = 200 ether;

    FixedPriceCurve internal hook;

    function setUp() public {
        deployFreshManagerAndRouters();
        deployMintAndApprove2Currencies();

        address hookAddress = address(
            uint160(
                Hooks.BEFORE_INITIALIZE_FLAG | Hooks.BEFORE_ADD_LIQUIDITY_FLAG | Hooks.BEFORE_REMOVE_LIQUIDITY_FLAG
                    | Hooks.BEFORE_SWAP_FLAG | Hooks.BEFORE_SWAP_RETURNS_DELTA_FLAG
            )
        );
        deployCodeTo("FixedPriceCurve.sol:FixedPriceCurve", abi.encode(manager, MID, SPREAD_BPS), hookAddress);
        hook = FixedPriceCurve(hookAddress);
        // The pool's own curve never runs, so its fee is irrelevant.
        (key,) = initPool(currency0, currency1, IHooks(hookAddress), 0, 1, SQRT_PRICE_1_1);

        ERC20(Currency.unwrap(currency0)).approve(hookAddress, type(uint256).max);
        ERC20(Currency.unwrap(currency1)).approve(hookAddress, type(uint256).max);
        hook.addLiquidity(
            BaseCustomAccounting.AddLiquidityParams(SEED0, SEED1, SEED0, SEED1, block.timestamp, 0, 0, bytes32(0))
        );
    }

    function test_depositBecomesHookOwnedClaims() public view {
        (uint256 reserve0, uint256 reserve1) = hook.reserves();
        assertEq(reserve0, SEED0);
        assertEq(reserve1, SEED1);
        assertEq(hook.balanceOf(address(this)), SEED0 + SEED1);
    }

    function test_swapsFillAtTheQuoteInAllFourCases() public {
        uint256 amount = 1 ether + 1; // odd, so that rounding matters
        uint256 bid = hook.bid();
        uint256 ask = hook.ask();

        _swapAndCheck(true, -int256(amount), amount, Math.mulDiv(amount, bid, WAD));
        _swapAndCheck(true, int256(amount), Math.mulDiv(amount, WAD, bid, Math.Rounding.Ceil), amount);
        _swapAndCheck(false, -int256(amount), amount, Math.mulDiv(amount, WAD, ask));
        _swapAndCheck(false, int256(amount), Math.mulDiv(amount, ask, WAD, Math.Rounding.Ceil), amount);
    }

    function testFuzz_poolValueAtMidNeverDecreases(uint256 amount, bool zeroForOne, bool exactInput) public {
        amount = bound(amount, 1, 50 ether);
        uint256 valueBefore = _valueAtMid();
        swap(key, zeroForOne, exactInput ? -int256(amount) : int256(amount), ZERO_BYTES);
        assertGe(_valueAtMid(), valueBefore);
    }

    function test_liquidityCannotBypassTheHook() public {
        vm.expectRevert();
        modifyLiquidityRouter.modifyLiquidity(key, LIQUIDITY_PARAMS, ZERO_BYTES);
    }

    function test_withdrawalReturnsTheWholeInventory() public {
        swap(key, true, -3 ether, ZERO_BYTES);
        (uint256 reserve0, uint256 reserve1) = hook.reserves();
        uint256 balance0 = currency0.balanceOf(address(this));
        uint256 balance1 = currency1.balanceOf(address(this));

        hook.removeLiquidity(
            BaseCustomAccounting.RemoveLiquidityParams(
                hook.balanceOf(address(this)), reserve0, reserve1, block.timestamp, 0, 0, bytes32(0)
            )
        );

        assertEq(currency0.balanceOf(address(this)) - balance0, reserve0);
        assertEq(currency1.balanceOf(address(this)) - balance1, reserve1);
        (reserve0, reserve1) = hook.reserves();
        assertEq(reserve0 + reserve1, 0);
    }

    /// @dev Swaps through the router and checks the trader's balances, the returned delta and the hook's claims.
    function _swapAndCheck(bool zeroForOne, int256 amountSpecified, uint256 expectedIn, uint256 expectedOut) internal {
        (Currency input, Currency output) = zeroForOne ? (currency0, currency1) : (currency1, currency0);
        uint256 inputBefore = input.balanceOf(address(this));
        uint256 outputBefore = output.balanceOf(address(this));
        (uint256 reserve0, uint256 reserve1) = hook.reserves();

        BalanceDelta delta = swap(key, zeroForOne, amountSpecified, ZERO_BYTES);

        assertEq(inputBefore - input.balanceOf(address(this)), expectedIn, "trader input");
        assertEq(output.balanceOf(address(this)) - outputBefore, expectedOut, "trader output");
        (int128 deltaIn, int128 deltaOut) =
            zeroForOne ? (delta.amount0(), delta.amount1()) : (delta.amount1(), delta.amount0());
        assertEq(deltaIn, -int128(int256(expectedIn)), "delta input");
        assertEq(deltaOut, int128(int256(expectedOut)), "delta output");

        (uint256 reserve0After, uint256 reserve1After) = hook.reserves();
        if (zeroForOne) {
            assertEq(reserve0After, reserve0 + expectedIn, "reserve0");
            assertEq(reserve1After, reserve1 - expectedOut, "reserve1");
        } else {
            assertEq(reserve1After, reserve1 + expectedIn, "reserve1");
            assertEq(reserve0After, reserve0 - expectedOut, "reserve0");
        }
    }

    function _valueAtMid() internal view returns (uint256) {
        (uint256 reserve0, uint256 reserve1) = hook.reserves();
        return Math.mulDiv(reserve0, MID, WAD) + reserve1;
    }
}
