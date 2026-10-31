import pytest

from propamm_analyzer.abi import (
    decode_address,
    encode_address,
    event_topic,
    selector,
    split_words,
    to_signed,
)


def test_keccak_matches_well_known_selectors() -> None:
    # hashlib's sha3_256 gives different digests; these values confirm Keccak-256.
    assert selector("transfer(address,uint256)") == "0xa9059cbb"
    assert selector("token0()") == "0x0dfe1681"
    assert selector("decimals()") == "0x313ce567"


def test_uniswap_v3_swap_topic() -> None:
    topic = event_topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
    assert topic == "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


def test_to_signed() -> None:
    assert to_signed((1 << 256) - 1) == -1
    assert to_signed(42) == 42
    assert to_signed((-3) % (1 << 256), 24) == -3  # an int24 sign-extended to 32 bytes


def test_split_words_rejects_partial_words() -> None:
    with pytest.raises(ValueError):
        split_words("0x" + "00" * 31)


def test_address_round_trip() -> None:
    address = "0x4200000000000000000000000000000000000006"
    assert decode_address(int(encode_address(address), 16)) == address
