from typing import Any

from propamm_analyzer.ingest.univ3 import SWAP_TOPIC, PoolInfo, decode_swap, normalize_swaps

POOL = PoolInfo(
    address="0x00000000000000000000000000000000000000aa",
    token0="0x4200000000000000000000000000000000000006",
    token1="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    decimals0=18,
    decimals1=6,
    fee_pips=500,
)


def _word(value: int) -> str:
    return format(value % (1 << 256), "064x")


def _log(amount0: int, amount1: int, tick: int) -> dict[str, Any]:
    return {
        "blockNumber": hex(100),
        "transactionIndex": hex(3),
        "logIndex": hex(7),
        "transactionHash": "0xabc",
        "topics": [SWAP_TOPIC, "0x" + _word(0xBEEF), "0x" + _word(0xCAFE)],
        "data": "0x" + "".join(_word(v) for v in (amount0, amount1, 2**96, 10**18, tick)),
    }


def test_decode_swap_reads_signed_amounts() -> None:
    swap = decode_swap(_log(-5 * 10**17, 1_300_000_000, -200_000))
    assert swap["amount0"] == -5 * 10**17
    assert swap["amount1"] == 1_300_000_000
    assert swap["tick"] == -200_000
    assert swap["sender"] == "0x" + format(0xBEEF, "040x")
    assert (swap["block"], swap["tx_index"], swap["log_index"]) == (100, 3, 7)


def test_normalize_uses_token_units_and_the_pool_perspective() -> None:
    # The pool paid 0.5 WETH and received 1,300 USDC: a user bought WETH.
    swap = decode_swap(_log(-5 * 10**17, 1_300_000_000, 0))
    frame = normalize_swaps(
        [swap], POOL, lambda block: 1_700_000_000 + 2 * block, venue="uniswap-v3", chain="base"
    )
    row = frame.row(0, named=True)
    assert row["d0"] == -0.5
    assert row["d1"] == 1_300.0
    assert row["fee_rate"] == 0.0005
    assert row["ts_ms"] == (1_700_000_000 + 200) * 1_000
