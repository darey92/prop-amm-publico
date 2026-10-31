"""The fast-oracle variant's on-chain price (§3), simulated from a reference price series.

Pyth's prices are no longer free to read (§12), so the backtests use a reference market price
in their place, such as Binance's 1 s closes. The model:

- The keeper sees the reference `latency_s` late. That delay is the pool's whole information
  lag behind the arbitrageur, who knows the reference at the end of each block: Pyth's own
  delay, the keeper's, and the inclusion of its update.
- The keeper pushes when the price it sees has moved `threshold` away from the on-chain one,
  or when the on-chain one is `heartbeat_s` old.
- A push needs a strictly newer publish time, as `parsePriceFeedUpdatesUnique` does.
- Each push updates the volatility estimate of §3.5.

The engine applies an anchor from the block after its own. So each push is stamped with the
block before the first block it prices: during block b the pool quotes the price the keeper saw
`latency_s` before b's end.
"""

from __future__ import annotations

import math

import polars as pl

PUSH_SCHEMA = pl.Schema(
    {"block": pl.Int64, "price": pl.Float64, "publish_ms": pl.Int64, "sigma": pl.Float64}
)


def block_times(first: int, last: int, stamped: pl.DataFrame, block_ms: int) -> pl.DataFrame:
    """`block` and `ts_ms` for every block from `first` to `last`, on a chain whose blocks are
    `block_ms` apart, like Base. `stamped` (`block`, `ts_ms`) anchors the grid, and every stamp
    in it must fall on the grid."""
    block0, ts0 = stamped.select("block", "ts_ms").row(0)
    grid = pl.DataFrame({"block": pl.int_range(first, last + 1, eager=True)}).with_columns(
        (ts0 + (pl.col("block") - block0) * block_ms).alias("ts_ms")
    )
    off = stamped.join(grid, on="block", suffix="_grid").filter(
        pl.col("ts_ms") != pl.col("ts_ms_grid")
    )
    if off.height:
        raise ValueError(f"{off.height} blocks are not {block_ms} ms apart")
    return grid


def subsecond_block_times(headers: pl.DataFrame) -> pl.DataFrame:
    """`block` and `ts_ms` on a chain whose blocks come several to a second but are stamped in
    whole seconds, like Monad's, about 300 ms apart. `headers` holds every block's `block` and
    `ts` (Unix seconds). The blocks of each second are spread evenly over it, each at the middle
    of an equal slot, which is within about 150 ms of a steady cadence."""
    slot = pl.int_range(pl.len()).over("ts")
    return (
        headers.sort("block")
        .with_columns(
            (pl.col("ts") * 1_000 + ((slot + 0.5) * 1_000 / pl.len().over("ts")).cast(pl.Int64))
            .cast(pl.Int64)
            .alias("ts_ms")
        )
        .select("block", "ts_ms")
    )


def keeper_pushes(
    blocks: pl.DataFrame,
    prices: pl.DataFrame,
    *,
    latency_s: float,
    threshold: float,
    heartbeat_s: float,
    sigma0: float,
    sigma_weight: float = 0.05,
) -> pl.DataFrame:
    """The keeper's pushes over `blocks` (`block`, `ts_ms`), from `prices` (`ts_ms`, `price`).

    Each row has the `block` it is stamped with, the `price`, its `publish_ms` and `sigma`, the
    volatility per √second after the push. `sigma0` starts the estimate, and `sigma_weight` is
    the EWMA weight of §3.5.
    """
    lag_ms = round(latency_s * 1_000)
    reference = prices.select(pl.col("ts_ms").alias("publish_ms"), "price").sort("publish_ms")
    seen = (
        blocks.select("block", "ts_ms", (pl.col("ts_ms") - lag_ms).alias("seen_ms"))
        .sort("seen_ms")
        .join_asof(reference, left_on="seen_ms", right_on="publish_ms", strategy="backward")
        .drop_nulls("price")
        .sort("block")
        .select("block", "ts_ms", "publish_ms", "price")
    )
    rows: list[tuple[int, float, int, float]] = []
    variance = sigma0**2
    onchain = math.nan
    published = -1
    for block, ts_ms, publish_ms, price in seen.iter_rows():
        if publish_ms <= published:
            continue
        if rows:
            moved = abs(price / onchain - 1) >= threshold
            if not moved and ts_ms - published < heartbeat_s * 1_000:
                continue
            change = price / onchain - 1
            elapsed = (publish_ms - published) / 1_000
            variance = (1 - sigma_weight) * variance + sigma_weight * change**2 / elapsed
        onchain, published = price, publish_ms
        rows.append((block - 1, price, publish_ms, math.sqrt(variance)))
    return pl.DataFrame(rows, schema=PUSH_SCHEMA, orient="row")


def reference_at_blocks(blocks: pl.DataFrame, prices: pl.DataFrame) -> pl.DataFrame:
    """The arbitrageur's reference for each block: the last price known at its `ts_ms`."""
    reference = prices.select(pl.col("ts_ms").alias("price_ms"), "price").sort("price_ms")
    return (
        blocks.sort("ts_ms")
        .join_asof(reference, left_on="ts_ms", right_on="price_ms", strategy="backward")
        .drop_nulls("price")
        .select("block", "ts_ms", "price")
        .sort("block")
    )
