"""LP markouts: what each swap gained or lost for the pool, marked to a later reference price.

For a swap that changes the pool's balances by (d0, d1), the LP markout at horizon h is

    markout_h = (d0 * P(t + h) + d1) / |d1|     (in basis points)

where P is the reference price (token1 per token0) last known at t + h. Positive means the
LPs gained. With fees excluded, the input-side delta is reduced by the pool fee, which
separates adverse selection from fee income. The negative of the fee-excluded markout at
h = 0 is the usual LVR proxy.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

import polars as pl

DEFAULT_HORIZONS_S = (0, 2, 12, 60, 300)


def orient(swaps: pl.DataFrame, quote_tokens: Collection[str]) -> pl.DataFrame:
    """Put the quote token on side 1 of every swap, so that one price (quote per base) marks
    every pool. Rows whose token0 is a quote token, like Uniswap v4's native-ETH pools, swap
    `d0` with `d1` and `token0` with `token1`, and invert `price_after`."""
    flip = pl.col("token0").is_in(list(quote_tokens))

    def pick(name: str, flipped: str) -> pl.Expr:
        return pl.when(flip).then(pl.col(flipped)).otherwise(pl.col(name)).alias(name)

    columns = [
        pick("d0", "d1"),
        pick("d1", "d0"),
        pick("token0", "token1"),
        pick("token1", "token0"),
    ]
    if "price_after" in swaps.columns:
        after = pl.col("price_after")
        columns.append(pl.when(flip).then(1 / after).otherwise(after).alias("price_after"))
    return swaps.with_columns(columns)


def attach_reference_prices(
    swaps: pl.DataFrame,
    prices: pl.DataFrame,
    horizons_s: Sequence[int] = DEFAULT_HORIZONS_S,
    *,
    max_staleness_ms: int = 5_000,
) -> pl.DataFrame:
    """Add one `p_{h}s` column per horizon: the last price known at `ts_ms + h`.

    The join looks backward, so no markout uses a future price. A price older than
    `max_staleness_ms` at the query time counts as missing.
    """
    reference = prices.select(pl.col("ts_ms").alias("ref_ts_ms"), "price").sort("ref_ts_ms")
    out = swaps.with_row_index("_row")
    for h in horizons_s:
        query = out.select("_row", (pl.col("ts_ms") + h * 1_000).alias("_t")).sort("_t")
        joined = query.join_asof(reference, left_on="_t", right_on="ref_ts_ms", strategy="backward")
        is_fresh = pl.col("_t") - pl.col("ref_ts_ms") <= max_staleness_ms
        fresh = pl.when(is_fresh).then(pl.col("price")).alias(f"p_{h}s")
        out = out.join(joined.select("_row", fresh), on="_row", how="left")
    return out.sort("_row").drop("_row")


def _pool_deltas(*, exclude_fees: bool) -> tuple[pl.Expr, pl.Expr]:
    """The pool's balance deltas, optionally with the fee removed from the input side."""
    d0, d1 = pl.col("d0"), pl.col("d1")
    if not exclude_fees:
        return d0, d1
    keep = 1.0 - pl.col("fee_rate")
    return (
        pl.when(d0 > 0).then(d0 * keep).otherwise(d0),
        pl.when(d1 > 0).then(d1 * keep).otherwise(d1),
    )


def lp_markouts(
    priced: pl.DataFrame,
    horizons_s: Sequence[int] = DEFAULT_HORIZONS_S,
    *,
    exclude_fees: bool = False,
) -> pl.DataFrame:
    """Per-swap markouts in bps, one `markout_{h}s_bps` column per horizon."""
    d0, d1 = _pool_deltas(exclude_fees=exclude_fees)
    notional = pl.col("d1").abs()
    return priced.with_columns(
        [
            ((d0 * pl.col(f"p_{h}s") + d1) / notional * 1e4).alias(f"markout_{h}s_bps")
            for h in horizons_s
        ]
    )


def summarize(
    priced: pl.DataFrame,
    horizons_s: Sequence[int] = DEFAULT_HORIZONS_S,
    *,
    by: Sequence[str] = ("venue", "pool"),
    exclude_fees: bool = False,
) -> pl.DataFrame:
    """Volume-weighted markouts per group, in bps: sum(d0 * P + d1) / sum(|d1|).

    Swaps without a fresh price at a horizon are left out of that horizon only.
    """
    d0, d1 = _pool_deltas(exclude_fees=exclude_fees)
    notional = pl.col("d1").abs()
    aggregations = [pl.len().alias("swaps"), notional.sum().alias("volume1")]
    for h in horizons_s:
        price = pl.col(f"p_{h}s")
        has_price = price.is_not_null()
        value = (d0 * price + d1).filter(has_price).sum()
        volume = notional.filter(has_price).sum()
        aggregations.append((value / volume * 1e4).alias(f"markout_{h}s_bps"))
    return priced.group_by(list(by)).agg(aggregations).sort(list(by))


def summarize_segments(
    priced: pl.DataFrame,
    segments: Sequence[tuple[str, pl.Expr]],
    horizons_s: Sequence[int] = DEFAULT_HORIZONS_S,
    *,
    exclude_fees: bool = False,
) -> pl.DataFrame:
    """`summarize` over named, possibly overlapping subsets of the swaps, in the given order."""
    parts = [
        priced.filter(condition).with_columns(
            pl.lit(order).alias("_order"), pl.lit(name).alias("segment")
        )
        for order, (name, condition) in enumerate(segments)
    ]
    table = summarize(
        pl.concat(parts), horizons_s, by=("_order", "segment"), exclude_fees=exclude_fees
    )
    return table.drop("_order")
