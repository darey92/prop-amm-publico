"""Backtest of the pool against a pair's real flow (W2), block by block.

The simulated pool joins the venues that traded in the data. It is small next to them, so the
market's price path is taken as given, and nothing it does changes the other venues:

- **Organic orders** are the routed transactions of the data (`registry.ROUTERS`), one per
  transaction and direction, with the totals each one paid and got. An aggregator sends an
  order to the pool when the pool's price beats what the order got, net of the extra gas. With
  `routing="all"` the pool must take the whole order within its per-block cap. With
  `routing="split"` it takes the slice that its steps priced better than the order's average
  price can cover, roughly how a splitting router equalises marginal prices.
- **The arbitrageur** trades at the top of each block. It already knows the reference price at
  the end of the block, and takes every step it can unwind at that price for a profit after
  `arb_cost` and gas. That is the worst case for LPs.
- **The anchor** is the pool's oracle: a staking token's rate, or the prices a keeper pushes
  (`sim.oracle`). Each one applies from the block after it, so arbitrageurs always move first.

When the reference is a pool's own mid, it also moves when an order that the pool took had
moved it in the data, so the arbitrageur sometimes trades on price changes that would not have
happened. That errs against the pool.
"""

from __future__ import annotations

import math
from array import array
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, NamedTuple

import polars as pl

from propamm_analyzer import markouts
from propamm_analyzer.sim.pool import AnchoredPool, PoolParams

GWEI = 1e-9

FILL_SCHEMA = pl.Schema(
    {
        "block": pl.Int64,
        "ts_ms": pl.Int64,
        "flow": pl.String,
        "order": pl.Int64,
        "d0": pl.Float64,
        "d1": pl.Float64,
    }
)
STATE_SCHEMA = pl.Schema(
    {
        "block": pl.Int64,
        "ts_ms": pl.Int64,
        "anchor": pl.Float64,
        "spread": pl.Float64,
        "q": pl.Float64,
        "reserve0": pl.Float64,
        "reserve1": pl.Float64,
    }
)


@dataclass(frozen=True)
class SimConfig:
    """One backtest: the pool, its size in token1, and the routing, gas and arbitrage model.

    Gas costs are in ETH at `gas_gwei`. With `gas_in_base`, token0 is (W)ETH and gas is turned
    into token1 at the reference price; otherwise token1 is WETH. `extra_gas` is what a swap
    through the pool costs over the venue it replaces, and `leg_gas` what an added leg through
    the pool costs a splitting router. `block_ms` stamps blocks that the data has none for.
    """

    pool: PoolParams
    tvl: float
    routing: str = "all"
    gas_gwei: float = 0.1
    gas_in_base: bool = False
    extra_gas: int = 40_000
    leg_gas: int = 120_000
    arbitrage: bool = True
    arb_cost: float = 1e-4
    arb_gas: int = 150_000
    block_ms: int = 12_000

    def __post_init__(self) -> None:
        if self.routing not in ("all", "split"):
            raise ValueError(f"routing must be 'all' or 'split', not {self.routing!r}")


@dataclass(frozen=True)
class SimResult:
    """The pool's fills as pool-side deltas (like swaps), its state at the start of each block
    it ran in full, its reserves at the start and the end, the simulated span, the anchor at the
    end, and the keeper's fees in token1."""

    fills: pl.DataFrame
    states: pl.DataFrame
    initial: tuple[float, float]
    final: tuple[float, float]
    start_ms: int
    end_ms: int
    end_anchor: float
    keeper_fees: float


class _Order(NamedTuple):
    order: int
    block: int
    ts_ms: int
    sells_base: bool
    amount_in: float
    amount_out: float


def organic_orders(swaps: pl.DataFrame, routers: Collection[str]) -> pl.DataFrame:
    """One order per routed transaction and direction, with the totals it paid and got.

    `swaps` must be oriented (`markouts.orient`), so token0 is the base and token1 the quote.
    An order that `sells_base` paid `amount_in` of token0 for `amount_out` of token1; the others
    paid token1 for token0. `quote` is the order's size in token1.
    """
    sells = pl.col("sells_base")
    return (
        swaps.filter(
            pl.col("tx_to").is_in(list(routers)) & (pl.col("d0") != 0) & (pl.col("d1") != 0)
        )
        .with_columns((pl.col("d0") > 0).alias("sells_base"))
        .group_by("tx_hash", "sells_base")
        .agg(
            pl.col("block").min(),
            pl.col("ts_ms").min(),
            pl.col("log_index").min(),
            pl.col("d0").sum(),
            pl.col("d1").sum(),
            pl.when(pl.col("venue").n_unique() == 1)
            .then(pl.col("venue").first())
            .otherwise(pl.lit("several"))
            .alias("venue"),
        )
        .with_columns(
            pl.when(sells).then(pl.col("d0")).otherwise(pl.col("d1")).alias("amount_in"),
            pl.when(sells).then(-pl.col("d1")).otherwise(-pl.col("d0")).alias("amount_out"),
            pl.when(sells).then(-pl.col("d1")).otherwise(pl.col("d1")).alias("quote"),
        )
        .filter((pl.col("amount_in") > 0) & (pl.col("amount_out") > 0))
        .sort("block", "log_index")
        .with_row_index("order")
        .select(
            pl.col("order").cast(pl.Int64),
            "block",
            "ts_ms",
            "tx_hash",
            "venue",
            "sells_base",
            "amount_in",
            "amount_out",
            "quote",
        )
    )


def simulate(
    orders: pl.DataFrame, reference: pl.DataFrame, anchors: pl.DataFrame, config: SimConfig
) -> SimResult:
    """Run the pool from the first block of `reference` or `orders` to the last.

    `reference` (`block`, `ts_ms`, `price`) is the market price the arbitrageur trades on, held
    from block to block until it changes. `anchors` (`block`, `price`, and optionally
    `publish_ms` and `sigma`) is the oracle: each row applies from the block after its own, and
    one must come before the first block. Without `publish_ms` the anchor never ages. The pool
    starts with `config.tvl` split evenly at its mid.

    A block runs in full only when it has an order, follows a block in which the pool traded,
    or offers the arbitrageur a price beyond the clamps. The clamps keep every quote at least
    `s_min` from the mid, so no other block can trade.
    """
    by_block: dict[int, list[_Order]] = defaultdict(list)
    columns = ("order", "block", "ts_ms", "sells_base", "amount_in", "amount_out")
    for row in orders.select(columns).iter_rows():
        order = _Order(*row)
        by_block[order.block].append(order)
    ref_blocks: list[int] = reference["block"].to_list()
    if not ref_blocks and not by_block:
        raise ValueError("nothing to simulate")
    first = min([*ref_blocks[:1], *by_block])
    last = max([*ref_blocks[-1:], *by_block])
    size = last - first + 1
    prices = array("d", [math.nan]) * size
    stamps = array("q", [-1]) * size
    for block, ts, price in reference.select("block", "ts_ms", "price").iter_rows():
        prices[block - first] = price
        stamps[block - first] = ts
    for block, waiting in by_block.items():
        if stamps[block - first] < 0:
            stamps[block - first] = waiting[0].ts_ms
    for i in range(1, size):
        if math.isnan(prices[i]):
            prices[i] = prices[i - 1]
        if stamps[i] < 0:
            stamps[i] = stamps[i - 1] + config.block_ms

    anchor_blocks: list[int] = anchors["block"].to_list()
    anchor_prices: list[float] = anchors["price"].to_list()
    ages = "publish_ms" in anchors.columns
    publish: list[int] = anchors["publish_ms"].to_list() if ages else []
    sigmas: list[float] = (
        anchors["sigma"].to_list() if "sigma" in anchors.columns else [0.0] * len(anchor_blocks)
    )
    current = bisect_left(anchor_blocks, first) - 1
    if current < 0:
        raise ValueError("anchors must include one before the first block")

    params = config.pool
    start_mid = anchor_prices[current] * (1 - params.offset)
    pool = AnchoredPool(params, config.tvl / 2 / start_mid, config.tvl / 2)
    initial = (pool.reserve0, pool.reserve1)
    # The lowest ask and the highest bid that any skew allows, relative to the mid.
    skew = 0.0 if params.clamp else params.k_inv
    ask_floor = (1 - skew) * (1 + params.s_min)
    bid_ceiling = (1 + skew) * (1 - params.s_min)
    gas_price = config.gas_gwei * GWEI
    fills: list[tuple[int, int, str, int, float, float]] = []
    states: list[tuple[int, int, float, float, float, float, float]] = []
    follow_up = -1
    for i in range(size):
        block = first + i
        while current + 1 < len(anchor_blocks) and anchor_blocks[current + 1] < block:
            current += 1
        price = prices[i]
        block_orders = by_block.get(block)
        if block_orders is None and block != follow_up:
            if not config.arbitrage or math.isnan(price):
                continue
            mid = anchor_prices[current] * (1 - params.offset)
            if (
                price * (1 - config.arb_cost) <= mid * ask_floor
                and price * (1 + config.arb_cost) >= mid * bid_ceiling
            ):
                continue
        ts = stamps[i]
        age = (ts - publish[current]) / 1_000 if ages else 0.0
        pool.new_block(anchor_prices[current], age, sigmas[current])
        states.append(
            (block, ts, anchor_prices[current], pool.spread, pool.q, pool.reserve0, pool.reserve1)
        )
        worth = price if config.gas_in_base and not math.isnan(price) else 1.0
        traded = False
        if config.arbitrage and not math.isnan(price):
            for zero_for_one, amount_in, amount_out in _arbitrage(
                pool, price, config, gas_price * worth
            ):
                fills.append(_fill(block, ts, "arb", -1, zero_for_one, amount_in, amount_out))
                traded = True
        for order in block_orders or ():
            won = _route(pool, order, config, gas_price * worth)
            if won is not None:
                fills.append(_fill(block, ts, "organic", order.order, order.sells_base, *won))
                traded = True
        if traded:
            follow_up = block + 1
    return SimResult(
        fills=pl.DataFrame(fills, schema=FILL_SCHEMA, orient="row"),
        states=pl.DataFrame(states, schema=STATE_SCHEMA, orient="row"),
        initial=initial,
        final=(pool.reserve0, pool.reserve1),
        start_ms=stamps[0],
        end_ms=stamps[-1],
        end_anchor=anchor_prices[current],
        keeper_fees=pool.keeper_fees,
    )


def _fill(
    block: int,
    ts: int,
    flow: str,
    order: int,
    zero_for_one: bool,
    amount_in: float,
    amount_out: float,
) -> tuple[int, int, str, int, float, float]:
    """A fill as the pool's deltas: it keeps the input net of the keeper's fee, and pays the
    output."""
    if zero_for_one:
        return block, ts, flow, order, amount_in, -amount_out
    return block, ts, flow, order, -amount_out, amount_in


def _arbitrage(
    pool: AnchoredPool, price: float, config: SimConfig, gas_price: float
) -> list[tuple[bool, float, float]]:
    """The arbitrageur's swaps against the pool, unwound at the reference `price` ± `arb_cost`.

    Each is (zero_for_one, input kept by the pool, output)."""
    swaps = []
    gas = config.arb_gas * gas_price
    # Buy token0 from the pool and sell it at the reference, less the cost.
    unwind = price * (1 - config.arb_cost)
    quote = pool.quote(False, math.inf, limit=unwind)
    if quote.amount_out * unwind - quote.amount_in > gas:
        _, fee = pool.swap(False, quote.amount_in)
        swaps.append((False, quote.amount_in - fee, quote.amount_out))
    # Sell token0 to the pool, bought at the reference plus the cost.
    cover = price * (1 + config.arb_cost)
    quote = pool.quote(True, math.inf, limit=cover)
    if quote.amount_out - quote.amount_in * cover > gas:
        _, fee = pool.swap(True, quote.amount_in)
        swaps.append((True, quote.amount_in - fee, quote.amount_out))
    return swaps


def _route(
    pool: AnchoredPool, order: _Order, config: SimConfig, gas_price: float
) -> tuple[float, float] | None:
    """The input the pool keeps and the output it pays when a router sends it `order`, or a
    slice of it."""
    zero_for_one = order.sells_base
    got = order.amount_out / order.amount_in
    # token1 per unit of the order's output, to value a better output in token1
    worth = 1.0 if zero_for_one else order.amount_in / order.amount_out
    if config.routing == "all":
        quote = pool.quote(zero_for_one, order.amount_in)
        if not quote.complete:
            return None
        gain = (quote.amount_out - order.amount_out) * worth
        gas = config.extra_gas * gas_price
    else:
        quote = pool.quote(zero_for_one, order.amount_in, limit=got if zero_for_one else 1 / got)
        gain = (quote.amount_out - quote.amount_in * got) * worth
        gas = config.leg_gas * gas_price
    if not gain > gas:
        return None
    _, fee = pool.swap(zero_for_one, quote.amount_in)
    return quote.amount_in - fee, quote.amount_out


def _days(start_ms: int, end_ms: int) -> list[date]:
    days = pl.date_range(
        pl.from_epoch(pl.lit(start_ms), time_unit="ms").dt.date(),
        pl.from_epoch(pl.lit(end_ms), time_unit="ms").dt.date(),
        eager=True,
    )
    return list(days)


def evaluate(
    result: SimResult,
    orders: pl.DataFrame,
    mids: pl.DataFrame,
    horizons_s: Sequence[int],
    *,
    burst_days: Collection[date] = (),
    max_staleness_ms: int = 6 * 3_600_000,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Markouts of the pool's fills by flow, and its volume, P&L and inventory.

    The markouts are volume-weighted bps against `mids`, which may reach past the simulated
    window. P&L is against holding the starting reserves, at the last mid of the window; it
    equals the markout of every fill at that moment, so it also counts what the inventory made
    or lost afterwards. `markout_pnl` holds the fills' value at each horizon instead, in token1:
    the spread kept after that horizon's adverse selection, with `markout_apr` per year.
    """
    fills = result.fills.with_columns(
        pl.from_epoch("ts_ms", time_unit="ms").dt.date().is_in(list(burst_days)).alias("burst")
    )
    priced = markouts.attach_reference_prices(
        fills, mids.select("ts_ms", "price"), horizons_s, max_staleness_ms=max_staleness_ms
    )
    organic, arb, burst = pl.col("flow") == "organic", pl.col("flow") == "arb", pl.col("burst")
    segments = [
        ("all fills", pl.lit(True)),
        ("organic", organic),
        ("arbitrage", arb),
        ("organic, calm days", organic & ~burst),
        ("organic, burst days", organic & burst),
        ("arbitrage, calm days", arb & ~burst),
        ("arbitrage, burst days", arb & burst),
    ]
    present = [(name, rule) for name, rule in segments if not fills.filter(rule).is_empty()]
    table = markouts.summarize_segments(priced, present, horizons_s) if present else pl.DataFrame()

    states = result.states
    start_ms, end_ms = result.start_ms, result.end_ms
    window = mids.filter(pl.col("ts_ms") <= end_ms)
    end_mid: float = window["price"][-1] if window.height else math.nan
    days = _days(start_ms, end_ms)
    per_day = (
        fills.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.date().alias("day"))
        .group_by("day", "flow")
        .agg(pl.col("d1").abs().sum().alias("volume"))
    )

    def daily(flow: str) -> pl.Series:
        volumes = dict(per_day.filter(pl.col("flow") == flow).select("day", "volume").iter_rows())
        return pl.Series([volumes.get(day, 0.0) for day in days], dtype=pl.Float64)

    organic_daily, arb_daily = daily("organic"), daily("arb")
    won = fills.filter(organic).select("order", pl.col("d1").abs().alias("won"))
    routed = orders.filter(pl.col("ts_ms").is_between(start_ms, end_ms))
    (x0, y0), (x1, y1) = result.initial, result.final
    hodl = x0 * end_mid + y0
    pnl = (x1 - x0) * end_mid + (y1 - y0)
    end_anchor = result.end_anchor
    markout_pnl = {
        h: float(
            priced.select((pl.col("d0") * pl.col(f"p_{h}s") + pl.col("d1")).sum()).item() or 0.0
        )
        for h in horizons_s
    }
    stats: dict[str, Any] = {
        "days": len(days),
        "routed_orders": routed.height,
        "routed_volume": routed["quote"].sum(),
        "organic_fills": won.height,
        "organic_volume": organic_daily.sum(),
        "organic_per_day_mean": organic_daily.mean(),
        "organic_per_day_median": organic_daily.median(),
        "arb_fills": fills.filter(arb).height,
        "arb_volume": arb_daily.sum(),
        "arb_per_day_mean": arb_daily.mean(),
        "end_mid": end_mid,
        "pnl": pnl,
        "pnl_bp": pnl / hodl * 1e4,
        "apr": pnl / hodl * 365 / len(days),
        "pnl_at_anchor": (x1 - x0) * end_anchor + (y1 - y0),
        "keeper_fees": result.keeper_fees,
        "markout_pnl": markout_pnl,
        "markout_apr": {h: v / hodl * 365 / len(days) for h, v in markout_pnl.items()},
        "q_min": states["q"].min() if states.height else 0.0,
        "q_max": states["q"].max() if states.height else 0.0,
        "q_last": states["q"][-1] if states.height else 0.0,
    }
    return table, stats
