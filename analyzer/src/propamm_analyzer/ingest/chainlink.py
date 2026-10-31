"""Chainlink feeds read from chain: the feed's description and the history of its answers.

A feed is read through its proxy, which points at the current aggregator. The aggregator emits
`AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)` on every new
round. Only the current aggregator is read, so answers from before the proxy last switched
aggregators are missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import polars as pl

from propamm_analyzer.abi import decode_address, event_topic, selector, split_words, to_signed
from propamm_analyzer.ingest.rpc import Log, RpcClient

ANSWER_UPDATED_TOPIC = event_topic("AnswerUpdated(int256,uint256,uint256)")

ANSWER_SCHEMA = pl.Schema(
    {
        "block": pl.Int64,
        "round_id": pl.String,
        "updated_at": pl.Int64,
        "answer": pl.Float64,
    }
)


@dataclass(frozen=True)
class Feed:
    proxy: str
    aggregator: str
    description: str
    decimals: int


def _call_string(rpc: RpcClient, to: str, signature: str) -> str:
    """A view function that returns one ABI-encoded `string`."""
    data = bytes.fromhex(rpc.eth_call(to, selector(signature)).removeprefix("0x"))
    length = int.from_bytes(data[32:64], "big")
    return data[64 : 64 + length].decode()


def feed(rpc: RpcClient, proxy: str) -> Feed:
    proxy = proxy.lower()
    return Feed(
        proxy=proxy,
        aggregator=decode_address(rpc.call_function(proxy, "aggregator()")[0]),
        description=_call_string(rpc, proxy, "description()"),
        decimals=rpc.call_function(proxy, "decimals()")[0],
    )


def decode_answer(log: Log, decimals: int) -> dict[str, Any]:
    return {
        "block": int(log["blockNumber"], 16),
        "round_id": str(int(log["topics"][2], 16)),
        "updated_at": split_words(log["data"])[0],
        "answer": to_signed(int(log["topics"][1], 16)) / 10**decimals,
    }


def answer_history(rpc: RpcClient, feed: Feed, from_block: int, to_block: int) -> pl.DataFrame:
    """Every answer the current aggregator published in [from_block, to_block], in order."""
    logs = rpc.iter_logs(
        feed.aggregator, [ANSWER_UPDATED_TOPIC], from_block, to_block, chunk=50_000
    )
    rows = [decode_answer(log, feed.decimals) for log in logs if not log.get("removed", False)]
    return pl.DataFrame(rows, schema=ANSWER_SCHEMA).sort("block")
