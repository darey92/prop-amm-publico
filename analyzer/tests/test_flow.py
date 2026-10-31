from typing import Any

import polars as pl
import pytest

from propamm_analyzer import flow
from propamm_analyzer.ingest.venues import ETH_SENTINEL, NATIVE

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WSTETH = "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
ROUTER = "0x" + "11" * 20
BOT = "0x" + "22" * 20
DAY_MS = 86_400_000


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    schema = {
        "ts_ms": pl.Int64,
        "d0": pl.Float64,
        "d1": pl.Float64,
        "token0": pl.String,
        "token1": pl.String,
        "tx_to": pl.String,
    }
    return pl.DataFrame(rows, schema=schema)


def test_volume_is_read_on_the_eth_side_and_split_by_router() -> None:
    swaps = _frame(
        [
            # wstETH/WETH, WETH as token1: 10 ETH through the router on day 0.
            {"ts_ms": 0, "d0": 8.0, "d1": -10.0, "token0": WSTETH, "token1": WETH, "tx_to": ROUTER},
            # Fluid, native ETH as token1: 4 ETH from a bot, still on day 0.
            {
                "ts_ms": DAY_MS - 1,
                "d0": -3.2,
                "d1": 4.0,
                "token0": WSTETH,
                "token1": ETH_SENTINEL,
                "tx_to": BOT,
            },
            # Uniswap v4, native ETH as token0: 6 ETH through the router on day 1.
            {
                "ts_ms": DAY_MS,
                "d0": 6.0,
                "d1": -4.8,
                "token0": NATIVE,
                "token1": WSTETH,
                "tx_to": ROUTER,
            },
        ]
    )
    frame = flow.with_quote(swaps, {WETH, NATIVE, ETH_SENTINEL})
    assert frame["quote"].to_list() == [10.0, 4.0, 6.0]
    per_day = flow.daily(frame, {ROUTER: "router"})
    assert per_day.select("swaps", "volume", "routed").rows() == [(2, 14.0, 10.0), (1, 6.0, 6.0)]
    assert per_day["day"].cast(pl.String).to_list() == ["1970-01-01", "1970-01-02"]
    points = flow.entry_points(frame, {ROUTER: "router"})
    assert points.select("tx_to", "label", "volume").rows() == [
        (ROUTER, "router", 16.0),
        (BOT, None, 4.0),
    ]
    assert points["share"].to_list() == pytest.approx([0.8, 0.2])
