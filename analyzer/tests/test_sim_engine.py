import polars as pl
import pytest

from propamm_analyzer.sim.engine import SimConfig, evaluate, organic_orders, simulate
from propamm_analyzer.sim.pool import PoolParams

BP = 1e-4
RATE = 1.25
ROUTER = "0xrouter"


def _swaps(rows: list[tuple[str, str, int, str, float, float]]) -> pl.DataFrame:
    """Oriented swaps as (tx_hash, tx_to, block, venue, d0, d1), pool-side."""
    return pl.DataFrame(
        {
            "tx_hash": [row[0] for row in rows],
            "tx_to": [row[1] for row in rows],
            "block": [row[2] for row in rows],
            "log_index": list(range(len(rows))),
            "ts_ms": [row[2] * 12_000 for row in rows],
            "venue": [row[3] for row in rows],
            "d0": [row[4] for row in rows],
            "d1": [row[5] for row in rows],
        }
    )


def _orders(rows: list[tuple[int, bool, float, float]]) -> pl.DataFrame:
    """Orders as (block, sells_base, amount_in, amount_out)."""
    return pl.DataFrame(
        {
            "order": list(range(len(rows))),
            "block": [row[0] for row in rows],
            "ts_ms": [row[0] * 12_000 for row in rows],
            "sells_base": [row[1] for row in rows],
            "amount_in": [row[2] for row in rows],
            "amount_out": [row[3] for row in rows],
            "quote": [row[3] if row[1] else row[2] for row in rows],
        }
    )


def _mids(prices: dict[int, float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "block": list(prices),
            "ts_ms": [block * 12_000 for block in prices],
            "price": list(prices.values()),
        }
    )


RATES = pl.DataFrame({"block": [1], "ts_ms": [12_000], "price": [RATE]})


def _config(**overrides: object) -> SimConfig:
    settings: dict[str, object] = {
        "pool": PoolParams(s_min=1 * BP, steps=((0.1, 0.0),)),
        "tvl": 100.0,
        "gas_gwei": 0.0,
    }
    return SimConfig(**(settings | overrides))  # type: ignore[arg-type]


def test_orders_group_each_routed_transaction_by_direction() -> None:
    swaps = _swaps(
        [
            ("0xa", ROUTER, 5, "fluid", 2.0, -2.49),
            ("0xa", ROUTER, 5, "uniswap-v3", 1.0, -1.245),
            ("0xb", ROUTER, 6, "uniswap-v3", -1.0, 1.26),
            ("0xc", "0xbot", 6, "uniswap-v3", 5.0, -6.2),
        ]
    )
    orders = organic_orders(swaps, {ROUTER})
    assert orders.select("block", "venue", "sells_base", "amount_in", "amount_out").rows() == [
        (5, "several", True, 3.0, pytest.approx(3.735)),
        (6, "uniswap-v3", False, 1.26, 1.0),
    ]
    assert orders["quote"].to_list() == [pytest.approx(3.735), 1.26]


def test_all_or_nothing_takes_orders_the_pool_prices_better_within_its_cap() -> None:
    bid = RATE * (1 - BP)
    orders = _orders(
        [
            (10, True, 1.0, bid - 0.0001),  # got less than the pool pays: won
            (10, True, 1.0, bid + 0.0001),  # got more: lost
            (10, True, 6.0, 6.0 * bid - 0.01),  # 7.5 WETH out is past the 5 WETH cap: lost
        ]
    )
    result = simulate(orders, _mids({10: RATE}), RATES, _config(arbitrage=False))
    assert result.fills["order"].to_list() == [0]
    fill = result.fills.row(0, named=True)
    assert (fill["block"], fill["ts_ms"], fill["flow"], fill["d0"]) == (10, 120_000, "organic", 1.0)
    assert fill["d1"] == pytest.approx(-bid)


def test_split_routing_takes_the_slice_its_cap_covers() -> None:
    bid = RATE * (1 - BP)
    orders = _orders([(10, True, 6.0, 6.0 * bid - 0.01)])
    result = simulate(orders, _mids({10: RATE}), RATES, _config(routing="split", arbitrage=False))
    (d0, d1) = result.fills.select("d0", "d1").row(0)
    assert d1 == pytest.approx(-5.0)  # 10 % of the 50 WETH reserve
    assert d0 == pytest.approx(5.0 / bid)


def test_the_arbitrageur_buys_until_the_skew_closes_the_gap() -> None:
    # The market jumps 30 bp above the pool's ask. Each block the arbitrageur takes the 10 %
    # cap, the skew lifts the ask at the next snapshot, and it stops once the ask is too high.
    # The clamp keeps the bid at the anchor, so the skew cannot overshoot the market.
    mid = RATE * (1 + 30 * BP)
    params = PoolParams(s_min=1 * BP, k_inv=100 * BP, steps=((0.1, 0.0),))
    mids = _mids({10: RATE, 20: mid, 40: mid})
    result = simulate(_orders([]), mids, RATES, _config(pool=params))
    arb = result.fills.filter(pl.col("flow") == "arb")
    blocks = arb["block"].to_list()
    assert blocks[0] == 20 and blocks == list(range(20, 20 + len(blocks))) and len(blocks) > 1
    assert (arb["d0"] < 0).all()  # the pool sold wstETH
    last = result.states.row(-1, named=True)
    skewed = RATE * (1 - 100 * BP * last["q"])
    assert skewed * (1 + BP) > mid * (1 - BP)


def test_pnl_is_every_fills_markout_at_the_last_mid() -> None:
    bid = RATE * (1 - BP)
    orders = _orders([(10, True, 1.0, bid - 0.001), (11, False, 2.0, 1.5)])
    mids = _mids({10: RATE, 11: RATE * (1 + 2 * BP), 12: RATE * (1 + 5 * BP)})
    result = simulate(orders, mids, RATES, _config(arbitrage=False))
    table, stats = evaluate(result, orders, mids, [0])
    end_mid = RATE * (1 + 5 * BP)
    fills = result.fills
    assert fills.height == 2
    expected = float(fills["d0"].sum()) * end_mid + float(fills["d1"].sum())
    assert stats["pnl"] == pytest.approx(expected)
    assert stats["organic_fills"] == 2 and stats["arb_fills"] == 0
    assert table["segment"].to_list() == ["all fills", "organic", "organic, calm days"]


def test_rates_must_start_before_the_first_block() -> None:
    late = pl.DataFrame({"block": [50], "ts_ms": [600_000], "price": [RATE]})
    with pytest.raises(ValueError, match="before the first block"):
        simulate(_orders([]), _mids({10: RATE}), late, _config())


def test_the_pool_quotes_the_last_push_and_its_age_widens_the_spread() -> None:
    # Blocks 2 s apart. A push before block 10 prices it at RATE; the reference then jumps
    # 6 bp at block 12, which the stale oracle still prices at RATE.
    params = PoolParams(s_min=1 * BP, k_sigma=1.0, latency_s=0.0, steps=((0.1, 0.0),))
    anchors = pl.DataFrame(
        {"block": [9], "price": [RATE], "publish_ms": [18_000], "sigma": [1 * BP]}
    )
    reference = pl.DataFrame(
        {"block": [10, 12], "ts_ms": [20_000, 24_000], "price": [RATE, RATE * (1 + 6 * BP)]}
    )
    config = _config(pool=params, block_ms=2_000)
    result = simulate(_orders([]), reference, anchors, config)
    arb = result.fills.filter(pl.col("flow") == "arb")
    assert arb["block"].to_list()[0] == 12
    first = result.states.filter(pl.col("block") == 12).row(0, named=True)
    # The price on chain is 6 s old at block 12: a spread of 1 bp times √6.
    assert first["spread"] == pytest.approx(BP * 6**0.5)


def test_gas_in_the_base_token_is_valued_at_the_reference() -> None:
    # Selling 1 ETH for 2,000 USDC: the pool pays 0.2 USDC more than the order got, which
    # beats 40,000 gas at 1 gwei worth 0.08 USDC, but not at 10 gwei (0.8 USDC).
    bid = 2_000 * (1 - BP)
    orders = _orders([(10, True, 1.0, bid - 0.2)])
    reference = pl.DataFrame({"block": [10], "ts_ms": [120_000], "price": [2_000.0]})
    anchors = pl.DataFrame({"block": [1], "ts_ms": [12_000], "price": [2_000.0]})
    for gwei, fills in ((1.0, 1), (10.0, 0)):
        config = _config(tvl=200_000.0, gas_gwei=gwei, gas_in_base=True, arbitrage=False)
        assert simulate(orders, reference, anchors, config).fills.height == fills
