"""The pool in floating point: `docs/spec.md` §4 and §5, for both oracle variants.

The pool's mid comes from an anchor. In the staking-token pilot the anchor is the token's own
exchange rate (`wstETH.stEthPerToken()` on Ethereum) times `1 - offset`, because the market
trades a little below the redemption value. In the fast-oracle variant it is the last price a
keeper pushed (§3), and the spread widens with that price's age and the measured volatility.

Quotes follow the spec: a block snapshot of the reserves sets the inventory skew, steps advance
with each side's gross outflow in the block, the last step is the per-block cap, and the clamps
keep every price at least `s_min` away from the mid.

This is the economic model the backtests run. The planned integer version, which would share test
vectors with the Solidity code and round as §5 says, was not written: Phase 0 ended in a no-go.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

# Relative slack for re-walking an input that a quote computed, so float error at a step
# boundary neither spills into the next step nor fails the swap.
_EPS = 1e-12


@dataclass(frozen=True)
class PoolParams:
    """Constructor parameters. Spreads, offsets and fractions are plain ratios (1e-4 = 1 bp).

    - `s_min`: the smallest half-spread, and the clamps' distance from the mid.
    - `s0`, `k_sigma`, `latency_s`: the spread is `max(s_min, s0 + k_sigma·sigma·√(age + L))`,
      with `sigma` the oracle's volatility per √second and `age` its price's age in seconds
      (§4).
    - `max_age_s`: swaps revert once the oracle price is older than this.
    - `offset`: the mid's discount to the anchor.
    - `k_inv`: inventory skew, the mid's shift at a fully one-sided pool (`m* = m·(1 - k·q)`).
    - `steps`: `(β_{j+1}, δ_j)` for each step j. Step j covers a side's gross outflow in the
      block from `β_j` to `β_{j+1}` times its reserve snapshot, at `δ_j` over the spread. The
      last β is the per-block cap.
    - `clamp`: keep every quote at least `s_min` from the mid whatever the skew (§4). Without it,
      the skew moves both sides.
    - `q_max`: the pool stops buying token0 while the snapshot imbalance `q` is at least this,
      and stops selling it while `q` is at most `-q_max`.
    - `keeper_share`: φ, the keeper's part of the spread revenue (§6).
    """

    s_min: float
    s0: float = 0.0
    k_sigma: float = 0.0
    latency_s: float = 0.0
    max_age_s: float = math.inf
    offset: float = 0.0
    k_inv: float = 0.0
    steps: tuple[tuple[float, float], ...] = ((0.1, 0.0),)
    clamp: bool = True
    q_max: float = 1.0
    keeper_share: float = 0.0

    def __post_init__(self) -> None:
        betas = [beta for beta, _ in self.steps]
        deltas = [delta for _, delta in self.steps]
        if not self.s_min > 0:
            raise ValueError("s_min must be positive")
        if not 0 <= self.k_inv < 1:
            raise ValueError("k_inv must be in [0, 1)")
        if not 0 < self.q_max <= 1:
            raise ValueError("q_max must be in (0, 1]")
        if not 0 <= self.keeper_share < 1:
            raise ValueError("keeper_share must be in [0, 1)")
        if (
            not self.steps
            or deltas[0] != 0
            or any(b <= a for a, b in pairwise([0.0, *betas]))
            or betas[-1] > 1
            or any(b < a for a, b in pairwise(deltas))
        ):
            raise ValueError("steps need 0 < β1 < … ≤ 1 and 0 = δ0 ≤ δ1 ≤ …")


@dataclass(frozen=True)
class Quote:
    """A walk of one side's steps for an exact-input swap.

    `complete` means the whole input was used. Otherwise `capped` tells whether the walk ran
    out of steps (the per-block cap, a side closed by `q_max`, or a stale oracle) rather than
    stopping at its limit price.
    """

    amount_in: float
    amount_out: float
    complete: bool
    capped: bool


class AnchoredPool:
    """Reserves and per-block state of one pool. Prices are token1 per token0."""

    def __init__(self, params: PoolParams, reserve0: float, reserve1: float) -> None:
        self.params = params
        self.reserve0 = reserve0
        self.reserve1 = reserve1
        self.mid = math.nan
        self.skewed_mid = math.nan
        self.spread = params.s_min
        self.stale = False
        self.q = 0.0
        self.outflow0 = 0.0
        self.outflow1 = 0.0
        self.keeper_fees = 0.0
        """Keeper fees so far, in token1 at each swap's mid."""
        self._snapshot0 = reserve0
        self._snapshot1 = reserve1

    def new_block(self, anchor: float, age_s: float = 0.0, sigma: float = 0.0) -> None:
        """Start a block at `anchor`, whose price is `age_s` old and whose volatility estimate
        is `sigma` per √second: snapshot the reserves, reset the outflows, set the spread and
        the skew."""
        params = self.params
        self.mid = anchor * (1 - params.offset)
        self.stale = age_s > params.max_age_s
        dynamic = params.s0 + params.k_sigma * sigma * math.sqrt(max(age_s, 0.0) + params.latency_s)
        self.spread = max(params.s_min, dynamic)
        if self.spread + params.steps[-1][1] >= 1:
            raise ValueError("the spread reached 100 %")
        self._snapshot0, self._snapshot1 = self.reserve0, self.reserve1
        self.outflow0 = self.outflow1 = 0.0
        value0 = self.reserve0 * self.mid
        total = value0 + self.reserve1
        if not total > 0:
            raise ValueError("the pool is empty")
        self.q = (value0 - self.reserve1) / total
        self.skewed_mid = self.mid * (1 - params.k_inv * self.q)

    def price(self, zero_for_one: bool, step: int) -> float:
        """Step `step`'s bid when the user sells token0 (`zero_for_one`), else its ask."""
        params = self.params
        spread = self.spread + params.steps[step][1]
        if zero_for_one:
            bid = self.skewed_mid * (1 - spread)
            return min(bid, self.mid * (1 - params.s_min)) if params.clamp else bid
        ask = self.skewed_mid * (1 + spread)
        return max(ask, self.mid * (1 + params.s_min)) if params.clamp else ask

    def quote(self, zero_for_one: bool, amount_in: float, limit: float | None = None) -> Quote:
        """Walk the steps for `amount_in` from the current outflow, as both the quote view and
        the swap do (§5).

        `zero_for_one` sells token0 to the pool at its bids. Otherwise the input is token1 and
        the pool sells token0 at its asks. With `limit`, the walk stops at the first step that
        is worse for the user: a bid below `limit`, or an ask above it.
        """
        params = self.params
        if zero_for_one:
            closed = self.q >= params.q_max
            snapshot, outflow = self._snapshot1, self.outflow1
        else:
            closed = self.q <= -params.q_max
            snapshot, outflow = self._snapshot0, self.outflow0
        used = amount_out = 0.0
        if not closed and not self.stale:
            for step, (beta, _) in enumerate(params.steps):
                top = beta * snapshot
                if outflow >= top:
                    continue
                price = self.price(zero_for_one, step)
                if limit is not None and (price < limit if zero_for_one else price > limit):
                    return Quote(used, amount_out, used >= amount_in, capped=False)
                room = top - outflow
                need = room / price if zero_for_one else room * price
                if amount_in - used <= need * (1 + _EPS):
                    rest = amount_in - used
                    out = rest * price if zero_for_one else rest / price
                    return Quote(amount_in, amount_out + out, complete=True, capped=False)
                used += need
                amount_out += room
                outflow = top
        return Quote(used, amount_out, complete=False, capped=True)

    def swap(self, zero_for_one: bool, amount_in: float) -> tuple[Quote, float]:
        """Execute an exact-input swap, and return it with the keeper's fee in input-token
        units. Like the hook, it fails past the per-block cap.

        The fee is φ times the spread revenue against the unskewed mid (§6), and stays out of
        the reserves.
        """
        quote = self.quote(zero_for_one, amount_in)
        if not quote.complete:
            raise ValueError("BlockCapExceeded")
        share = self.params.keeper_share
        if zero_for_one:
            fee = share * max(quote.amount_in - quote.amount_out / self.mid, 0.0)
            self.reserve0 += quote.amount_in - fee
            self.reserve1 -= quote.amount_out
            self.outflow1 += quote.amount_out
            self.keeper_fees += fee * self.mid
        else:
            fee = share * max(quote.amount_in - quote.amount_out * self.mid, 0.0)
            self.reserve1 += quote.amount_in - fee
            self.reserve0 -= quote.amount_out
            self.outflow0 += quote.amount_out
            self.keeper_fees += fee
        return quote, fee

    def value(self, price: float) -> float:
        """The reserves' worth in token1 at `price`."""
        return self.reserve0 * price + self.reserve1
