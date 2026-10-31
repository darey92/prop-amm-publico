"""Lido's share rate: stETH per wstETH, which is wstETH's redemption rate.

Each daily oracle report makes the stETH contract emit `TokenRebased` with its totals after the
report. `postTotalEther * 1e18 // postTotalShares` is exactly what `wstETH.stEthPerToken()`
returns from that block on (checked against the contract at four reports on 2026-10-30).
"""

from __future__ import annotations

import polars as pl

from propamm_analyzer.abi import event_topic, split_words
from propamm_analyzer.ingest.hypersync import HyperSyncClient, LogFilter
from propamm_analyzer.ingest.rpc import Log

TOKEN_REBASED = event_topic("TokenRebased(uint256,uint256,uint256,uint256,uint256,uint256,uint256)")
RATE_SCHEMA = pl.Schema({"block": pl.Int64, "ts_ms": pl.Int64, "price": pl.Float64})


def report_rate(log: Log) -> int:
    """The rate after a report, scaled by 1e18, as `stEthPerToken()` gives it."""
    _, _, _, post_total_shares, post_total_ether, _ = split_words(log["data"])
    return post_total_ether * 10**18 // post_total_shares


def rate_history(
    client: HyperSyncClient, steth: str, from_block: int, to_block: int
) -> pl.DataFrame:
    """One row per oracle report in [from_block, to_block], with the rate as a float."""
    scan = client.logs([LogFilter((steth,), ((TOKEN_REBASED,),))], from_block, to_block)
    rows = []
    for log in scan.logs:
        block = int(log["blockNumber"], 16)
        rows.append(
            {
                "block": block,
                "ts_ms": scan.timestamps[block] * 1_000,
                "price": report_rate(log) / 10**18,
            }
        )
    return pl.DataFrame(rows, schema=RATE_SCHEMA)
