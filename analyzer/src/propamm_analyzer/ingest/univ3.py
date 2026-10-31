"""Swaps from Uniswap v3-style concentrated-liquidity pools (Uniswap v3, Aerodrome Slipstream)."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import polars as pl

from propamm_analyzer.abi import (
    decode_address,
    encode_address,
    encode_uint,
    event_topic,
    split_words,
    to_signed,
)
from propamm_analyzer.ingest.rpc import BlockClock, Log, RpcClient
from propamm_analyzer.normalize import swaps_frame

SWAP_TOPIC = event_topic("Swap(address,address,int256,int256,uint160,uint128,int24)")


@dataclass(frozen=True)
class PoolInfo:
    address: str
    token0: str
    token1: str
    decimals0: int
    decimals1: int
    fee_pips: int
    """Swap fee in hundredths of a basis point: 500 = 0.05 %."""


def _call_word(rpc: RpcClient, to: str, signature: str, args: str = "") -> int:
    return rpc.call_function(to, signature, args)[0]


def find_pool(rpc: RpcClient, factory: str, token_a: str, token_b: str, fee_pips: int) -> str:
    """Pool address from a v3-style factory's `getPool`."""
    args = encode_address(token_a) + encode_address(token_b) + encode_uint(fee_pips)
    pool = decode_address(_call_word(rpc, factory, "getPool(address,address,uint24)", args))
    if int(pool, 16) == 0:
        raise LookupError(f"no pool for {token_a}/{token_b} with fee {fee_pips} at {factory}")
    return pool


def fetch_pool_info(rpc: RpcClient, address: str) -> PoolInfo:
    token0 = decode_address(_call_word(rpc, address, "token0()"))
    token1 = decode_address(_call_word(rpc, address, "token1()"))
    return PoolInfo(
        address=address.lower(),
        token0=token0,
        token1=token1,
        decimals0=_call_word(rpc, token0, "decimals()"),
        decimals1=_call_word(rpc, token1, "decimals()"),
        fee_pips=_call_word(rpc, address, "fee()"),
    )


def decode_swap(log: Log) -> dict[str, Any]:
    """Decode a v3 `Swap` log. Amounts are from the pool's side: positive = the pool received."""
    amount0, amount1, sqrt_price_x96, liquidity, tick = split_words(log["data"])
    return {
        "block": int(log["blockNumber"], 16),
        "tx_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
        "sender": decode_address(int(log["topics"][1], 16)),
        "amount0": to_signed(amount0),
        "amount1": to_signed(amount1),
        "sqrt_price_x96": sqrt_price_x96,
        "liquidity": liquidity,
        "tick": to_signed(tick, 24),
    }


def normalize_swaps(
    swaps: Iterable[dict[str, Any]],
    pool: PoolInfo,
    timestamp: Callable[[int], int],
    *,
    venue: str,
    chain: str,
) -> pl.DataFrame:
    """Convert decoded swaps to the analyzer's swap schema (token units, pool's perspective)."""
    scale0 = 10**pool.decimals0
    scale1 = 10**pool.decimals1
    return swaps_frame(
        {
            "chain": chain,
            "venue": venue,
            "pool": pool.address,
            "block": swap["block"],
            "tx_index": swap["tx_index"],
            "log_index": swap["log_index"],
            "tx_hash": swap["tx_hash"],
            "sender": swap["sender"],
            "ts_ms": timestamp(swap["block"]) * 1_000,
            "d0": swap["amount0"] / scale0,
            "d1": swap["amount1"] / scale1,
            "fee_rate": pool.fee_pips / 1_000_000,
        }
        for swap in swaps
    )


def fetch_swaps(
    rpc: RpcClient, pool: PoolInfo, from_block: int, to_block: int, *, venue: str, chain: str
) -> pl.DataFrame:
    """All swaps of `pool` in [from_block, to_block], normalised."""
    logs = rpc.iter_logs(pool.address, [SWAP_TOPIC], from_block, to_block)
    swaps = [decode_swap(log) for log in logs if not log.get("removed", False)]
    clock = BlockClock(rpc, from_block, to_block)
    return normalize_swaps(swaps, pool, clock.timestamp, venue=venue, chain=chain)
