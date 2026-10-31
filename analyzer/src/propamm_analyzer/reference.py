"""Reference prices for markouts where no exchange quotes the pair, as for staking tokens.

Their price is made on-chain, so the market reference is the mid that the pools themselves log
after each swap. The latest pool to trade sets it, and it holds until the next trade.
"""

from __future__ import annotations

from collections.abc import Collection

import polars as pl


def pool_mids(swaps: pl.DataFrame, pools: Collection[str]) -> pl.DataFrame:
    """The mid after each block's last swap in any of `pools`, as `block`, `ts_ms` and `price`.

    `swaps` must be oriented (`markouts.orient`) so that `price_after` is quote per base.
    """
    return (
        swaps.filter(pl.col("pool").is_in(list(pools)) & pl.col("price_after").is_not_null())
        .sort("block", "log_index")
        .group_by("block", maintain_order=True)
        .last()
        .select("block", "ts_ms", pl.col("price_after").alias("price"))
    )
