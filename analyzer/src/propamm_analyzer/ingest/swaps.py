"""Swaps from every kind of venue in the registry, normalised to the analyzer's swap schema.

Each venue family logs swaps differently:

- Uniswap v3, Aerodrome Slipstream, SushiSwap v3: `Swap(sender, recipient, amount0, amount1,
  sqrtPriceX96, liquidity, tick)`, amounts from the pool's side.
- PancakeSwap v3: the same plus two protocol-fee fields.
- Aerodrome v2 (Solidly): `Swap(sender, to, amount0In, amount1In, amount0Out, amount1Out)`.
- Uniswap v4: one `Swap(id, sender, amount0, amount1, sqrtPriceX96, liquidity, tick, fee)` per
  pool id on the PoolManager. The amounts are the swapper's deltas (negative = paid in), despite
  the interface comment, so they are negated here.
- Curve: `TokenExchange(buyer, sold_id, tokens_sold, bought_id, tokens_bought, ...)`. The ids are
  int128 in stableswap pools and uint256 in cryptoswap ones, which changes the topic but not the
  words. Pools can hold more than two coins, so a trade between other coins is skipped.
- Fluid DEX: `Swap(swap0to1, amountIn, amountOut, to)`, in the DEX's own token order. It logs no
  sender; the recipient stands in, which in practice is the router that called the DEX.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import Any

import polars as pl

from propamm_analyzer.abi import decode_address, event_topic, split_words, to_signed
from propamm_analyzer.ingest.hypersync import HyperSyncClient, LogFilter
from propamm_analyzer.ingest.rpc import Log, RpcClient
from propamm_analyzer.ingest.univ3 import SWAP_TOPIC as V3_SWAP_TOPIC
from propamm_analyzer.ingest.venues import FoundPool
from propamm_analyzer.normalize import swaps_frame
from propamm_analyzer.registry import Venue

SWAP_TOPICS = {
    "v3": V3_SWAP_TOPIC,
    "pancake-v3": event_topic(
        "Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)"
    ),
    "solidly": event_topic("Swap(address,address,uint256,uint256,uint256,uint256)"),
    "v4": event_topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"),
    "fluid": event_topic("Swap(bool,uint256,uint256,address)"),
}
CURVE_TOPICS = (
    event_topic("TokenExchange(address,int128,uint256,int128,uint256)"),  # stableswap, -ng
    event_topic("TokenExchange(address,uint256,uint256,uint256,uint256)"),  # cryptoswap
    # twocrypto-ng and tricrypto-ng add the fee and the packed price scale.
    event_topic("TokenExchange(address,uint256,uint256,uint256,uint256,uint256,uint256)"),
)
# Uniswap v4 pools per HyperSync log filter. Base has thousands of WETH/USDC pools, so the
# PoolManager's filter is split into several, which HyperSync ORs together.
_IDS_PER_FILTER = 1_000


def swap_event(venue: Venue) -> str:
    """The venue's swap event layout: its own override, else the one its kind implies."""
    if venue.swap_event:
        return venue.swap_event
    return {
        "v3": "v3",
        "slipstream": "v3",
        "solidly": "solidly",
        "v4": "v4",
        "curve": "curve",
        "fluid": "fluid",
    }[venue.kind]


def swap_topics(event: str) -> tuple[str, ...]:
    """The topics a layout's swaps can carry; Curve's three variants share one layout."""
    return CURVE_TOPICS if event == "curve" else (SWAP_TOPICS[event],)


def decode(event: str, log: Log, pool: FoundPool | None = None) -> dict[str, Any] | None:
    """Raw pool-side amounts (positive = the pool received), sender, and the per-swap fee and
    post-swap sqrtPriceX96 where the event logs them.

    Curve needs `pool` for its coin indices, and returns None for a trade between other coins.
    """
    words = split_words(log["data"])
    fee_pips: int | None = None
    sqrt_price_x96: int | None = None
    if event in ("v3", "pancake-v3"):
        amount0, amount1 = to_signed(words[0]), to_signed(words[1])
        sender = int(log["topics"][1], 16)
        sqrt_price_x96 = words[2]
    elif event == "solidly":
        amount0, amount1 = words[0] - words[2], words[1] - words[3]
        sender = int(log["topics"][1], 16)
    elif event == "v4":
        amount0, amount1 = -to_signed(words[0], 128), -to_signed(words[1], 128)
        sender = int(log["topics"][2], 16)
        sqrt_price_x96 = words[2]
        fee_pips = words[5]
    elif event == "curve":
        if pool is None or pool.coins is None:
            raise ValueError("Curve swaps need the pool's coin indices")
        sold_id, sold, bought_id, bought = words[:4]
        index0, index1 = pool.coins
        if (sold_id, bought_id) == (index0, index1):
            amount0, amount1 = sold, -bought
        elif (sold_id, bought_id) == (index1, index0):
            amount0, amount1 = -bought, sold
        else:
            return None
        sender = int(log["topics"][1], 16)
    elif event == "fluid":
        swap0to1, amount_in, amount_out = words[:3]
        amount0, amount1 = (amount_in, -amount_out) if swap0to1 else (-amount_out, amount_in)
        sender = words[3]
    else:
        raise ValueError(f"unknown swap event {event!r}")
    return {
        "block": int(log["blockNumber"], 16),
        "tx_index": int(log["transactionIndex"], 16),
        "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"],
        "sender": decode_address(sender),
        "amount0": amount0,
        "amount1": amount1,
        "fee_pips": fee_pips,
        "sqrt_price_x96": sqrt_price_x96,
    }


def _row(
    swap: dict[str, Any],
    pool: FoundPool,
    timestamp: int,
    decimals: dict[str, int],
    *,
    venue: str,
    chain: str,
) -> dict[str, Any]:
    sqrt_price_x96 = swap["sqrt_price_x96"]
    price_after = None
    if sqrt_price_x96:
        scale = 10 ** (decimals[pool.token0] - decimals[pool.token1])
        price_after = (sqrt_price_x96 / 2**96) ** 2 * scale
    return {
        "chain": chain,
        "venue": venue,
        "pool": pool.pool,
        "block": swap["block"],
        "tx_index": swap["tx_index"],
        "log_index": swap["log_index"],
        "tx_hash": swap["tx_hash"],
        "sender": swap["sender"],
        "ts_ms": timestamp * 1_000,
        "d0": swap["amount0"] / 10 ** decimals[pool.token0],
        "d1": swap["amount1"] / 10 ** decimals[pool.token1],
        "fee_rate": (swap["fee_pips"] or pool.fee_pips) / 1_000_000,
        "price_after": price_after,
    }


def fetch_swaps(
    rpc: RpcClient,
    venue: Venue,
    pool: FoundPool,
    from_block: int,
    to_block: int,
    *,
    timestamp: Callable[[int], int],
    decimals: dict[str, int],
    chain: str,
) -> pl.DataFrame:
    """All swaps of `pool` in [from_block, to_block], in the analyzer's swap schema."""
    event = swap_event(venue)
    rows = []
    for topic in swap_topics(event):
        if event == "v4":
            address, topics = venue.address, [topic, pool.pool]
        else:
            address, topics = pool.pool, [topic]
        # Most pools are sparse, so wide ranges save calls; iter_logs halves them if a node
        # objects.
        for log in rpc.iter_logs(address, topics, from_block, to_block, chunk=20_000):
            swap = None if log.get("removed", False) else decode(event, log, pool)
            if swap is not None:
                rows.append(
                    _row(
                        swap,
                        pool,
                        timestamp(swap["block"]),
                        decimals,
                        venue=venue.name,
                        chain=chain,
                    )
                )
    return swaps_frame(rows)


def fetch_swaps_hypersync(
    client: HyperSyncClient,
    venues: Sequence[Venue],
    pools: Sequence[FoundPool],
    from_block: int,
    to_block: int,
    *,
    decimals: dict[str, int],
    chain: str,
) -> pl.DataFrame:
    """Swaps of all `pools` in [from_block, to_block] in one HyperSync scan.

    The swap schema comes with four more columns: `tx_from` and `tx_to` (the account that sent
    the transaction and the contract it called) and the pool's `token0` and `token1`.
    """
    by_name = {venue.name: venue for venue in venues}
    groups: dict[str, list[FoundPool]] = defaultdict(list)
    for pool in pools:
        groups[pool.venue].append(pool)
    filters = []
    for name, group in groups.items():
        venue = by_name[name]
        topics = swap_topics(swap_event(venue))
        if swap_event(venue) == "v4":
            ids = [p.pool for p in group]
            for start in range(0, len(ids), _IDS_PER_FILTER):
                chunk = tuple(ids[start : start + _IDS_PER_FILTER])
                filters.append(LogFilter((venue.address,), (topics, chunk)))
        else:
            filters.append(LogFilter(tuple(p.pool for p in group), (topics,)))
    # v4 logs come from the PoolManager and name their pool in the first topic.
    route = {pool.pool: pool for pool in pools}
    scan = client.logs(filters, from_block, to_block)
    rows = []
    for log in scan.logs:
        found = route.get(log["address"])
        if found is None and len(log["topics"]) > 1:
            found = route.get(log["topics"][1])
        if found is None:
            continue
        swap = decode(swap_event(by_name[found.venue]), log, found)
        if swap is not None:
            timestamp = scan.timestamps[swap["block"]]
            rows.append(_row(swap, found, timestamp, decimals, venue=found.venue, chain=chain))
    hashes = sorted({row["tx_hash"] for row in rows})
    transactions = pl.DataFrame(
        {
            "tx_hash": hashes,
            "tx_from": [scan.transactions.get(h, ("", None))[0] or None for h in hashes],
            "tx_to": [scan.transactions.get(h, ("", None))[1] for h in hashes],
        },
        schema={"tx_hash": pl.String, "tx_from": pl.String, "tx_to": pl.String},
    )
    tokens = pl.DataFrame(
        {
            "pool": [p.pool for p in pools],
            "token0": [p.token0 for p in pools],
            "token1": [p.token1 for p in pools],
        },
        schema={"pool": pl.String, "token0": pl.String, "token1": pl.String},
    )
    return (
        swaps_frame(rows)
        .join(transactions, on="tx_hash", how="left")
        .join(tokens, on="pool", how="left")
        .sort("block", "log_index")
    )
