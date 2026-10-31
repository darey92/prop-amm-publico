import polars as pl
import pytest

from propamm_analyzer.markouts import (
    attach_reference_prices,
    lp_markouts,
    orient,
    summarize,
    summarize_segments,
)

PRICES = pl.DataFrame(
    {"ts_ms": [1_000, 2_000, 3_000, 4_000], "price": [1000.0, 1000.0, 990.0, 980.0]}
)


def _swaps(rows: list[tuple[int, float, float]], fee_rate: float = 0.0) -> pl.DataFrame:
    """Swaps as (ts_ms, d0, d1), from the pool's perspective."""
    return pl.DataFrame(
        {
            "venue": ["v"] * len(rows),
            "pool": ["p"] * len(rows),
            "ts_ms": [ts for ts, _, _ in rows],
            "d0": [d0 for _, d0, _ in rows],
            "d1": [d1 for _, _, d1 in rows],
            "fee_rate": [fee_rate] * len(rows),
        }
    )


def test_prices_are_joined_backward() -> None:
    priced = attach_reference_prices(_swaps([(1_500, 1.0, -1000.0)]), PRICES, [0, 2])
    # At 1.5 s the last known price is the one stamped at 1.0 s; at 3.5 s it is 990.
    assert priced["p_0s"].to_list() == [1000.0]
    assert priced["p_2s"].to_list() == [990.0]


def test_stale_prices_count_as_missing() -> None:
    priced = attach_reference_prices(
        _swaps([(1_000, 1.0, -1000.0)]), PRICES, [10], max_staleness_ms=5_000
    )
    # 11 s is 7 s after the last price (4 s).
    assert priced["p_10s"].to_list() == [None]


def test_markout_sign() -> None:
    # The pool bought 1 token0 for 1000 token1, then the price fell 1 %: the LPs lost 100 bps.
    priced = attach_reference_prices(_swaps([(1_000, 1.0, -1000.0)]), PRICES, [0, 2])
    marked = lp_markouts(priced, [0, 2])
    assert marked["markout_0s_bps"][0] == pytest.approx(0.0)
    assert marked["markout_2s_bps"][0] == pytest.approx(-100.0)


def test_excluding_fees_reduces_the_input_side() -> None:
    priced = attach_reference_prices(_swaps([(1_000, 1.0, -1000.0)], fee_rate=0.003), PRICES, [0])
    marked = lp_markouts(priced, [0], exclude_fees=True)
    assert marked["markout_0s_bps"][0] == pytest.approx(-30.0)


def test_summary_is_volume_weighted() -> None:
    swaps = _swaps([(1_000, 1.0, -1000.0), (1_000, -3.0, 3000.0)])
    summary = summarize(attach_reference_prices(swaps, PRICES, [2]), [2])
    # Swap 1: 1 * 990 - 1000 = -10. Swap 2: -3 * 990 + 3000 = +30. Volume: 4000.
    assert summary["markout_2s_bps"][0] == pytest.approx((30 - 10) / 4000 * 1e4)
    assert summary["swaps"][0] == 2


def test_orient_puts_the_quote_token_on_side_one() -> None:
    swaps = pl.DataFrame(
        {
            "token0": ["wsteth", "eth"],
            "token1": ["eth", "wsteth"],
            "d0": [1.0, -1.25],
            "d1": [-1.25, 1.0],
            "price_after": [1.25, 0.8],
        }
    )
    oriented = orient(swaps, {"eth"})
    # The second pool holds native ETH as token0: its deltas, tokens and mid are flipped.
    assert oriented.rows() == [
        ("wsteth", "eth", 1.0, -1.25, 1.25),
        ("wsteth", "eth", 1.0, -1.25, 1.25),
    ]


def test_segments_overlap_and_keep_their_order() -> None:
    swaps = _swaps([(1_000, 1.0, -1000.0), (1_000, -3.0, 3000.0)])
    priced = attach_reference_prices(swaps, PRICES, [2])
    segments = [("sells", pl.col("d0") < 0), ("all", pl.lit(True)), ("buys", pl.col("d0") > 0)]
    table = summarize_segments(priced, segments, [2])
    assert table["segment"].to_list() == ["sells", "all", "buys"]
    assert table["markout_2s_bps"].to_list() == pytest.approx([100.0, 50.0, -100.0])
