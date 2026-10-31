"""The analyzer's common schemas. Every venue-specific decoder produces these columns."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import polars as pl

# One row per swap. `d0` and `d1` are the pool's balance changes in token units (positive
# means the pool received). `fee_rate` is the pool's input fee as a fraction (0.0005 = 5 bps).
# `price_after` is the pool's mid after the swap (token1 per token0, in token units), for
# venues whose events log it (Uniswap v3 and v4 style), and null elsewhere.
SWAP_SCHEMA = pl.Schema(
    {
        "chain": pl.String,
        "venue": pl.String,
        "pool": pl.String,
        "block": pl.Int64,
        "tx_index": pl.Int64,
        "log_index": pl.Int64,
        "tx_hash": pl.String,
        "sender": pl.String,
        "ts_ms": pl.Int64,
        "d0": pl.Float64,
        "d1": pl.Float64,
        "fee_rate": pl.Float64,
        "price_after": pl.Float64,
    }
)

# Reference prices (token1 per token0), stamped at the moment the price became known.
PRICE_SCHEMA = pl.Schema({"ts_ms": pl.Int64, "price": pl.Float64})


def swaps_frame(records: Iterable[Mapping[str, Any]]) -> pl.DataFrame:
    """Build a swap frame in chain order, rejecting rows that do not match the schema."""
    return pl.DataFrame(list(records), schema=SWAP_SCHEMA).sort("block", "log_index")
