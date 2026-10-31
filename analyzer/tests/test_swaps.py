from collections.abc import Sequence
from typing import Any

import pytest

from propamm_analyzer.ingest.hypersync import HyperSyncClient, LogFilter, LogScan
from propamm_analyzer.ingest.rpc import Log, RpcClient
from propamm_analyzer.ingest.swaps import (
    CURVE_TOPICS,
    SWAP_TOPICS,
    decode,
    fetch_swaps,
    fetch_swaps_hypersync,
    swap_event,
)
from propamm_analyzer.ingest.venues import ETH_SENTINEL, NATIVE, FoundPool
from propamm_analyzer.registry import Venue

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
MANAGER = "0x00000000000000000000000000000000000000f4"
POOL_ID = "0x" + "ab" * 32
SENDER = 0xBEEF
STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
WSTETH = "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
CURVE_POOL = "0x00000000000000000000000000000000000000c0"
FLUID_DEX = "0x00000000000000000000000000000000000000d1"


def _word(value: int) -> str:
    return format(value % (1 << 256), "064x")


def _log(event: str, topics: list[str], words: Sequence[int]) -> dict[str, Any]:
    return {
        "blockNumber": hex(100),
        "transactionIndex": hex(3),
        "logIndex": hex(7),
        "transactionHash": "0xabc",
        "topics": [SWAP_TOPICS[event], *topics],
        "data": "0x" + "".join(_word(w) for w in words),
    }


SENDER_TOPIC = "0x" + _word(SENDER)


def test_each_layout_is_read_from_the_pools_side() -> None:
    # A user sells 1 ETH-like token0 for 2,000 USDC-like token1 in every venue family.
    v3 = _log("v3", [SENDER_TOPIC, SENDER_TOPIC], [10**18, -2_000 * 10**6, 2**96, 10**18, 5])
    pancake = _log(
        "pancake-v3", [SENDER_TOPIC, SENDER_TOPIC], [10**18, -2_000 * 10**6, 0, 0, 0, 0, 0]
    )
    solidly = _log("solidly", [SENDER_TOPIC, SENDER_TOPIC], [10**18, 0, 0, 2_000 * 10**6])
    # v4 logs the swapper's deltas: it paid token0 (negative) and received token1 (positive).
    v4 = _log("v4", [POOL_ID, SENDER_TOPIC], [-(10**18), 2_000 * 10**6, 2**96, 10**18, 5, 500])
    for event, log in (("v3", v3), ("pancake-v3", pancake), ("solidly", solidly), ("v4", v4)):
        swap = decode(event, log)
        assert swap is not None
        assert (swap["amount0"], swap["amount1"]) == (10**18, -2_000 * 10**6), event
        assert swap["sender"] == "0x" + format(SENDER, "040x"), event
    v4_swap, v3_swap = decode("v4", v4), decode("v3", v3)
    assert v4_swap is not None and v4_swap["fee_pips"] == 500
    assert v3_swap is not None and v3_swap["fee_pips"] is None


def _curve_log(topic: str, words: Sequence[int]) -> dict[str, Any]:
    log = _log("v3", [SENDER_TOPIC], words)
    log["topics"][0] = topic
    return log


def test_curve_exchanges_go_through_the_pools_coin_indices() -> None:
    # Coins [other, ETH, stETH]: the pair is coins 1 and 2, whatever the event variant.
    pool = FoundPool(
        "curve", CURVE_POOL, ETH_SENTINEL, STETH, "coins 1,2 of 3", 400, 0, 0, coins=(1, 2)
    )
    for topic in CURVE_TOPICS:
        sell = decode("curve", _curve_log(topic, [2, 2 * 10**18, 1, 1_999 * 10**15]), pool)
        assert sell is not None
        assert (sell["amount0"], sell["amount1"]) == (-1_999 * 10**15, 2 * 10**18)
    buy = decode("curve", _curve_log(CURVE_TOPICS[0], [1, 10**18, 2, 10**18 + 1]), pool)
    assert buy is not None and (buy["amount0"], buy["amount1"]) == (10**18, -(10**18 + 1))
    assert decode("curve", _curve_log(CURVE_TOPICS[0], [0, 5, 1, 5]), pool) is None
    with pytest.raises(ValueError):
        decode("curve", _curve_log(CURVE_TOPICS[0], [1, 1, 2, 1]))


def test_fluid_swaps_follow_the_direction_flag() -> None:
    pool = FoundPool("fluid", FLUID_DEX, WSTETH, ETH_SENTINEL, "dex 1", 0, 0, 0)
    sell = decode("fluid", _log("fluid", [], [1, 58 * 10**18, 72 * 10**18, SENDER]), pool)
    buy = decode("fluid", _log("fluid", [], [0, 10**17, 8 * 10**16, SENDER]), pool)
    assert sell is not None and (sell["amount0"], sell["amount1"]) == (58 * 10**18, -72 * 10**18)
    assert buy is not None and (buy["amount0"], buy["amount1"]) == (-8 * 10**16, 10**17)
    assert sell["sender"] == "0x" + format(SENDER, "040x")  # the recipient stands in


def test_venues_map_to_their_swap_layouts() -> None:
    assert swap_event(Venue("slipstream", "slipstream", "0x01")) == "v3"
    assert swap_event(Venue("pancake", "v3", "0x01", swap_event="pancake-v3")) == "pancake-v3"
    assert swap_event(Venue("v4", "v4", "0x01")) == "v4"
    assert swap_event(Venue("curve", "curve", "0x01")) == "curve"
    with pytest.raises(ValueError):
        decode("curve", _log("v3", [SENDER_TOPIC, SENDER_TOPIC], [0, 0, 0, 0, 0]))


class LogRpc(RpcClient):
    """Fake node that serves one list of logs for one (address, topics) filter."""

    def __init__(self, address: str, topics: list[str], logs: list[Log]) -> None:
        super().__init__("http://localhost.invalid")
        self.filter = (address, topics)
        self.logs = logs

    def get_logs(
        self, address: str, topics: Sequence[str | None], from_block: int, to_block: int
    ) -> list[Log]:
        return self.logs if (address, list(topics)) == self.filter else []


def test_v4_swaps_are_read_from_the_pool_manager_with_the_logged_fee() -> None:
    pool = FoundPool("uniswap-v4", POOL_ID, NATIVE, USDC, "fee 500", 500, 1, 2**96)
    venue = Venue("uniswap-v4", "v4", MANAGER)
    log = _log("v4", [POOL_ID, SENDER_TOPIC], [-(10**18), 2_000 * 10**6, 2**96, 10**18, 5, 3_000])
    rpc = LogRpc(MANAGER, [SWAP_TOPICS["v4"], POOL_ID], [log])
    frame = fetch_swaps(
        rpc,
        venue,
        pool,
        0,
        200,
        timestamp=lambda block: 1_700_000_000 + 2 * block,
        decimals={NATIVE: 18, USDC: 6},
        chain="base",
    )
    row = frame.row(0, named=True)
    assert (row["d0"], row["d1"]) == (1.0, -2_000.0)
    assert row["fee_rate"] == 0.003  # from the event, not the pool's static fee
    assert row["price_after"] == 1e12  # sqrtPriceX96 = 2**96, scaled by 10**(18 - 6)
    assert row["ts_ms"] == (1_700_000_000 + 200) * 1_000


class FakeHyperSync(HyperSyncClient):
    """Serves one fixed scan and records the filters it was asked for."""

    def __init__(self, scan: LogScan) -> None:
        super().__init__("https://hypersync.invalid", "token")
        self.scan = scan
        self.filters: list[LogFilter] = []

    def logs(self, filters: Sequence[LogFilter], from_block: int, to_block: int) -> LogScan:
        self.filters = list(filters)
        return self.scan


def test_hypersync_swaps_carry_their_transaction_and_tokens() -> None:
    v4_pool = FoundPool("uniswap-v4", POOL_ID, NATIVE, USDC, "fee 500", 500, 1, 2**96)
    fluid_pool = FoundPool("fluid", FLUID_DEX, WSTETH, ETH_SENTINEL, "dex 1", 0, 0, 0)
    venues = [Venue("uniswap-v4", "v4", MANAGER), Venue("fluid", "fluid", "0x01")]
    v4_log = _log("v4", [POOL_ID, SENDER_TOPIC], [-(10**18), 2_000 * 10**6, 2**96, 1, 5, 500])
    v4_log["address"] = MANAGER  # the PoolManager logs it; the pool is in the first topic
    fluid_log = _log("fluid", [], [1, 10**18, 2 * 10**18, SENDER])
    fluid_log.update(address=FLUID_DEX, blockNumber=hex(101), transactionHash="0xdef")
    client = FakeHyperSync(
        LogScan(
            logs=[v4_log, fluid_log],
            timestamps={100: 1_700_000_000, 101: 1_700_000_012},
            transactions={"0xabc": ("0xuser", "0xrouter"), "0xdef": ("0xbot", "0xbotcontract")},
        )
    )
    frame = fetch_swaps_hypersync(
        client,
        venues,
        [v4_pool, fluid_pool],
        0,
        200,
        decimals={NATIVE: 18, USDC: 6, WSTETH: 18, ETH_SENTINEL: 18},
        chain="ethereum",
    )
    assert client.filters == [
        LogFilter((MANAGER,), ((SWAP_TOPICS["v4"],), (POOL_ID,))),
        LogFilter((FLUID_DEX,), ((SWAP_TOPICS["fluid"],),)),
    ]
    rows = frame.rows(named=True)
    assert [(r["venue"], r["tx_from"], r["tx_to"], r["token0"]) for r in rows] == [
        ("uniswap-v4", "0xuser", "0xrouter", NATIVE),
        ("fluid", "0xbot", "0xbotcontract", WSTETH),
    ]
    assert (rows[0]["d0"], rows[0]["d1"]) == (1.0, -2_000.0)
    assert (rows[0]["price_after"], rows[1]["price_after"]) == (1e12, None)  # Fluid logs none
    assert (rows[1]["d0"], rows[1]["d1"], rows[1]["ts_ms"]) == (1.0, -2.0, 1_700_000_012_000)
