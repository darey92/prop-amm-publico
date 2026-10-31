import polars as pl

from propamm_analyzer.reference import pool_mids


def test_mids_come_from_each_blocks_last_swap_in_the_chosen_pools() -> None:
    swaps = pl.DataFrame(
        {
            "pool": ["deep", "deep", "thin", "deep", "fluid"],
            "block": [10, 10, 10, 11, 12],
            "log_index": [1, 5, 9, 2, 0],
            "ts_ms": [120_000, 120_000, 120_000, 132_000, 144_000],
            "price_after": [1.2470, 1.2474, 1.2600, 1.2472, None],
        }
    )
    mids = pool_mids(swaps, ["deep", "fluid"])
    # Block 10 keeps the deep pool's last mid; the thin pool and Fluid's missing mid are ignored.
    assert mids.rows() == [(10, 120_000, 1.2474), (11, 132_000, 1.2472)]
