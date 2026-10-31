"""Daily flow of a pair: the volume a new pool competes for, and the part that routers send.

A swap counts as routed when its transaction calls a known router, aggregator or intent protocol
(`registry.ROUTERS`). Bots and solvers that call their own contracts do not count: they trade
with a pool only when it pays them, so that flow cannot be won by quoting well.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import polars as pl


def with_quote(frame: pl.DataFrame, quote_tokens: Collection[str]) -> pl.DataFrame:
    """Add `quote`, each swap's size in the quote token: the pool-side amount of whichever of
    `token0` and `token1` is in `quote_tokens` (WETH and native ETH count as one)."""
    quote_is_0 = pl.col("token0").is_in(list(quote_tokens))
    return frame.with_columns(
        pl.when(quote_is_0).then(pl.col("d0")).otherwise(pl.col("d1")).abs().alias("quote")
    )


def daily(frame: pl.DataFrame, routers: Collection[str]) -> pl.DataFrame:
    """Per UTC day: swaps, quote volume, and the part whose transaction called a router."""
    routed = pl.col("tx_to").is_in(list(routers))
    return (
        frame.with_columns(pl.from_epoch("ts_ms", time_unit="ms").dt.date().alias("day"))
        .group_by("day")
        .agg(
            pl.len().alias("swaps"),
            pl.col("quote").sum().alias("volume"),
            pl.col("quote").filter(routed).sum().alias("routed"),
        )
        .sort("day")
    )


def entry_points(frame: pl.DataFrame, routers: Mapping[str, str]) -> pl.DataFrame:
    """Volume per contract that the transactions called (`tx_to`), largest first."""
    return (
        frame.group_by("tx_to")
        .agg(pl.len().alias("swaps"), pl.col("quote").sum().alias("volume"))
        .with_columns(
            (pl.col("volume") / pl.col("volume").sum()).alias("share"),
            pl.col("tx_to")
            .replace_strict(dict(routers), default=None, return_dtype=pl.String)
            .alias("label"),
        )
        .sort("volume", descending=True)
    )
