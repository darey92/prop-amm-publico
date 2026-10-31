// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BaseCustomCurve} from "@openzeppelin/uniswap-hooks/base/BaseCustomCurve.sol";
import {BaseHook} from "@openzeppelin/uniswap-hooks/base/BaseHook.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";

/// @notice Test-only custom curve that quotes a fixed mid price with a symmetric spread.
/// @dev Exercises the v4 custom-curve plumbing (hook-owned ERC-6909 inventory, BeforeSwapDelta signs) with the
/// rounding rule of docs/spec.md §5: every amount is rounded in the pool's favour. It accepts a single deposit.
/// The production pricing (PricingLib) was never written: Phase 0 ended in a no-go (docs/phase-0-results.md).
contract FixedPriceCurve is BaseCustomCurve, ERC20 {
    uint256 internal constant WAD = 1e18;
    uint256 internal constant BPS = 10_000;

    /// @notice Prices in token1 per token0, WAD-scaled.
    uint256 public immutable bid;
    uint256 public immutable ask;

    error SingleDepositOnly();

    constructor(IPoolManager manager, uint256 mid, uint256 spreadBps) BaseHook(manager) ERC20("Smoke LP", "SLP") {
        bid = mid * (BPS - spreadBps) / BPS;
        ask = Math.mulDiv(mid, BPS + spreadBps, BPS, Math.Rounding.Ceil);
    }

    /// @notice The inventory: the hook's ERC-6909 claims on the PoolManager.
    function reserves() public view returns (uint256 reserve0, uint256 reserve1) {
        PoolKey memory key = poolKey();
        reserve0 = poolManager.balanceOf(address(this), key.currency0.toId());
        reserve1 = poolManager.balanceOf(address(this), key.currency1.toId());
    }

    function _getUnspecifiedAmount(SwapParams calldata params) internal view override returns (uint256) {
        bool exactInput = params.amountSpecified < 0;
        uint256 specified = exactInput ? uint256(-params.amountSpecified) : uint256(params.amountSpecified);
        if (params.zeroForOne) {
            // The pool buys token0 at the bid: token1 out for exact input, token0 in for exact output.
            return exactInput ? Math.mulDiv(specified, bid, WAD) : Math.mulDiv(specified, WAD, bid, Math.Rounding.Ceil);
        }
        // The pool sells token0 at the ask: token0 out for exact input, token1 in for exact output.
        return exactInput ? Math.mulDiv(specified, WAD, ask) : Math.mulDiv(specified, ask, WAD, Math.Rounding.Ceil);
    }

    function _getSwapFeeAmount(SwapParams calldata, uint256) internal pure override returns (uint256) {
        return 0;
    }

    function _getAmountIn(AddLiquidityParams memory params)
        internal
        view
        override
        returns (uint256 amount0, uint256 amount1, uint256 shares)
    {
        if (totalSupply() != 0) revert SingleDepositOnly();
        amount0 = params.amount0Desired;
        amount1 = params.amount1Desired;
        shares = amount0 + amount1;
    }

    function _getAmountOut(RemoveLiquidityParams memory params)
        internal
        view
        override
        returns (uint256 amount0, uint256 amount1, uint256 shares)
    {
        (uint256 reserve0, uint256 reserve1) = reserves();
        shares = params.liquidity;
        amount0 = Math.mulDiv(reserve0, shares, totalSupply());
        amount1 = Math.mulDiv(reserve1, shares, totalSupply());
    }

    function _mint(AddLiquidityParams memory, BalanceDelta, BalanceDelta, uint256 shares) internal override {
        _mint(msg.sender, shares);
    }

    function _burn(RemoveLiquidityParams memory, BalanceDelta, BalanceDelta, uint256 shares) internal override {
        _burn(msg.sender, shares);
    }
}
