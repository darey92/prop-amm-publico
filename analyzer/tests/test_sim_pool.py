import random

import pytest

from propamm_analyzer.sim.pool import AnchoredPool, PoolParams

BP = 1e-4
RATE = 1.25


def _pool(reserve0: float = 40.0, reserve1: float = 50.0, **overrides: object) -> AnchoredPool:
    """A pool at RATE, balanced by default (40 wstETH are worth 50 WETH), with two steps on
    each side: 5 % of the reserve at s_min, then up to 10 % at 2 bp more."""
    settings: dict[str, object] = {"s_min": 1 * BP, "steps": ((0.05, 0.0), (0.10, 2 * BP))}
    pool = AnchoredPool(PoolParams(**(settings | overrides)), reserve0, reserve1)  # type: ignore[arg-type]
    pool.new_block(RATE)
    return pool


def test_a_balanced_pool_quotes_s_min_around_the_rate() -> None:
    pool = _pool()
    assert pool.q == pytest.approx(0.0)
    sold = pool.quote(True, 1.0)
    bought = pool.quote(False, 1.0)
    assert sold.complete and bought.complete
    assert sold.amount_out == pytest.approx(RATE * (1 - BP))
    assert bought.amount_out == pytest.approx(1 / (RATE * (1 + BP)))


def test_the_offset_discounts_the_mid() -> None:
    pool = _pool(offset=2 * BP)
    assert pool.mid == pytest.approx(RATE * (1 - 2 * BP))
    assert pool.quote(True, 1.0).amount_out == pytest.approx(RATE * (1 - 2 * BP) * (1 - BP))


def test_steps_follow_the_gross_outflow_and_the_last_one_caps_the_block() -> None:
    pool = _pool()
    bid0, bid1 = RATE * (1 - BP), RATE * (1 - 3 * BP)
    # The first step pays out 5 % of the 50 WETH snapshot; the rest of the input gets bid1.
    first_input = 2.5 / bid0
    quote = pool.quote(True, first_input + 1.0)
    assert quote.complete
    assert quote.amount_out == pytest.approx(2.5 + 1.0 * bid1)
    # Past 10 % of the snapshot the swap would exceed the cap.
    too_much = first_input + 2.5 / bid1 + 0.01
    capped = pool.quote(True, too_much)
    assert not capped.complete and capped.capped
    assert capped.amount_out == pytest.approx(5.0)
    with pytest.raises(ValueError, match="BlockCapExceeded"):
        pool.swap(True, too_much)


def test_each_side_climbs_its_own_steps_and_a_round_trip_loses() -> None:
    pool = _pool()
    start = pool.value(pool.mid)
    bought, _ = pool.swap(False, 2.0)  # WETH in, wstETH out: the ask side
    assert pool.outflow0 == pytest.approx(bought.amount_out) and pool.outflow1 == 0
    sold, _ = pool.swap(True, bought.amount_out)  # straight back: the bid side
    assert sold.amount_out < 2.0
    assert pool.value(pool.mid) > start


def test_the_skew_moves_only_the_side_that_adds_to_the_imbalance_when_clamped() -> None:
    long0 = _pool(reserve0=60.0, k_inv=20 * BP)  # 75 WETH of wstETH against 50 WETH
    assert long0.q == pytest.approx(0.2)
    skewed = RATE * (1 - 20 * BP * 0.2)
    assert long0.price(True, 0) == pytest.approx(skewed * (1 - BP))
    assert long0.price(False, 0) == pytest.approx(RATE * (1 + BP))
    free = _pool(reserve0=60.0, k_inv=20 * BP, clamp=False)
    assert free.price(False, 0) == pytest.approx(skewed * (1 + BP))


def test_q_max_closes_the_side_that_adds_to_the_imbalance() -> None:
    pool = _pool(reserve0=60.0, q_max=0.2)
    closed = pool.quote(True, 1.0)
    assert closed.amount_out == 0 and closed.capped
    assert pool.quote(False, 1.0).complete


def test_the_skew_comes_from_the_block_snapshot() -> None:
    pool = _pool(k_inv=20 * BP)
    pool.swap(True, 3.0)
    assert pool.q == pytest.approx(0.0)
    pool.new_block(RATE)
    assert pool.q > 0
    assert pool.outflow0 == pool.outflow1 == 0


def test_a_limit_stops_the_walk_at_the_first_worse_step() -> None:
    pool = _pool()
    quote = pool.quote(True, 100.0, limit=RATE * (1 - 2 * BP))
    assert not quote.complete and not quote.capped
    assert quote.amount_out == pytest.approx(2.5)


def test_swaps_never_lower_the_pools_value_at_the_mid() -> None:
    pool = _pool(k_inv=30 * BP)
    rng = random.Random(7)
    for _ in range(300):
        if rng.random() < 0.2:
            pool.new_block(RATE * (1 + rng.uniform(-5, 5) * BP))
        zero_for_one = rng.random() < 0.5
        quote = pool.quote(zero_for_one, rng.uniform(0.01, 3.0))
        if not quote.complete:
            continue
        before = pool.value(pool.mid)
        pool.swap(zero_for_one, quote.amount_in)
        assert pool.value(pool.mid) >= before


@pytest.mark.parametrize(
    "steps",
    [(), ((0.1, 1 * BP),), ((0.1, 0.0), (0.05, 1 * BP)), ((0.5, 0.0), (1.5, 0.0))],
)
def test_steps_must_grow_from_zero(steps: tuple[tuple[float, float], ...]) -> None:
    with pytest.raises(ValueError, match="steps"):
        PoolParams(s_min=BP, steps=steps)


def test_the_spread_widens_with_the_oracles_age_and_volatility() -> None:
    pool = _pool(s0=0.5 * BP, k_sigma=2.0, latency_s=1.0, max_age_s=30.0)
    pool.new_block(RATE, age_s=3.0, sigma=1 * BP)
    assert pool.spread == pytest.approx(0.5 * BP + 2.0 * BP * 2.0)
    assert pool.quote(True, 1.0).amount_out == pytest.approx(RATE * (1 - pool.spread))
    pool.new_block(RATE, age_s=0.0, sigma=0.0)
    assert pool.spread == BP  # never below s_min
    pool.new_block(RATE, age_s=31.0, sigma=1 * BP)
    closed = pool.quote(True, 1.0)
    assert pool.stale and closed.amount_out == 0 and closed.capped


def test_the_keeper_takes_its_share_of_the_spread_revenue() -> None:
    pool = _pool(keeper_share=0.2)
    quote, fee = pool.swap(True, 1.0)  # 1 wstETH in at RATE·(1 - 1 bp)
    revenue = 1.0 - quote.amount_out / RATE  # in wstETH, against the mid
    assert fee == pytest.approx(0.2 * revenue)
    assert pool.reserve0 == pytest.approx(41.0 - fee)
    assert pool.keeper_fees == pytest.approx(fee * RATE)
