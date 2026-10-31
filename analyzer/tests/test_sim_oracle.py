import math

import polars as pl
import pytest

from propamm_analyzer.sim.oracle import (
    block_times,
    keeper_pushes,
    reference_at_blocks,
    subsecond_block_times,
)

BP = 1e-4


def _prices(values: list[float], start_ms: int = 1_000) -> pl.DataFrame:
    """One price per second, stamped at the end of each second."""
    return pl.DataFrame(
        {"ts_ms": [start_ms + 1_000 * i for i in range(len(values))], "price": values}
    )


def test_block_times_fill_the_grid_and_reject_stamps_off_it() -> None:
    stamped = pl.DataFrame({"block": [10, 13], "ts_ms": [20_000, 26_000]})
    grid = block_times(9, 14, stamped, 2_000)
    assert grid.rows() == [(b, 20_000 + (b - 10) * 2_000) for b in range(9, 15)]
    with pytest.raises(ValueError, match="not 2000 ms apart"):
        block_times(9, 14, pl.DataFrame({"block": [10, 13], "ts_ms": [20_000, 27_000]}), 2_000)


def test_blocks_stamped_in_whole_seconds_are_spread_over_their_second() -> None:
    # Out of order: three blocks in second 100 and four in second 101.
    blocks = [16, 10, 11, 12, 13, 14, 15]
    headers = pl.DataFrame({"block": blocks, "ts": [101] + [100] * 3 + [101] * 3})
    assert subsecond_block_times(headers).rows() == [
        (10, 100_166),
        (11, 100_500),
        (12, 100_833),
        (13, 101_125),
        (14, 101_375),
        (15, 101_625),
        (16, 101_875),
    ]


def test_the_keeper_pushes_on_a_move_or_a_heartbeat_and_stamps_the_block_before() -> None:
    # Blocks every 2 s from t = 10 s; the price jumps 5 bp at t = 16 s.
    prices = _prices([100.0] * 15 + [100.05] * 30)
    blocks = pl.DataFrame(
        {"block": list(range(1, 16)), "ts_ms": [10_000 + 2_000 * i for i in range(15)]}
    )
    pushes = keeper_pushes(
        blocks, prices, latency_s=2.0, threshold=2 * BP, heartbeat_s=8.0, sigma0=1 * BP
    )
    # Block 1 (t = 10 s) sees the price of t = 8 s: the first push, stamped block 0. At block 4
    # (t = 16 s) that price is 8 s old: a heartbeat. Block 5 (t = 18 s) sees the jump. After
    # that, a heartbeat whenever the price on chain is 8 s old.
    assert pushes["block"].to_list() == [0, 3, 4, 7, 10, 13]
    assert pushes["price"].to_list() == [100.0, 100.0, 100.05, 100.05, 100.05, 100.05]
    assert pushes["publish_ms"].to_list() == [8_000, 14_000, 16_000, 22_000, 28_000, 34_000]


def test_each_push_updates_the_volatility_estimate() -> None:
    prices = _prices([100.0, 100.0, 100.1, 100.1])
    blocks = pl.DataFrame({"block": [1, 2], "ts_ms": [1_000, 3_000]})
    pushes = keeper_pushes(
        blocks,
        prices,
        latency_s=0.0,
        threshold=1 * BP,
        heartbeat_s=60.0,
        sigma0=1 * BP,
        sigma_weight=0.5,
    )
    change = 100.1 / 100.0 - 1
    expected = math.sqrt(0.5 * BP**2 + 0.5 * change**2 / 2.0)
    assert pushes["sigma"].to_list() == [pytest.approx(BP), pytest.approx(expected)]


def test_the_reference_is_the_last_price_at_each_block() -> None:
    blocks = pl.DataFrame({"block": [1, 2, 3], "ts_ms": [500, 2_000, 3_500]})
    reference = reference_at_blocks(blocks, _prices([100.0, 101.0, 102.0]))
    # No price is known at 0.5 s; at 2 s it is the one stamped at 2 s; at 3.5 s, at 3 s.
    assert reference.rows() == [(2, 2_000, 101.0), (3, 3_500, 102.0)]
