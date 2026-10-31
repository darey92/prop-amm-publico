"""Minimal ABI helpers: Keccak-256, selectors, event topics and 32-byte word decoding."""

from __future__ import annotations

from Crypto.Hash import keccak

_ADDRESS_MASK = (1 << 160) - 1


def keccak256(data: bytes) -> bytes:
    """Ethereum's Keccak-256. This is not NIST SHA3-256, which is what hashlib provides."""
    return keccak.new(digest_bits=256, data=data).digest()


def selector(signature: str) -> str:
    """Function selector, e.g. `selector("token0()") == "0x0dfe1681"`."""
    return "0x" + keccak256(signature.encode()).hex()[:8]


def event_topic(signature: str) -> str:
    return "0x" + keccak256(signature.encode()).hex()


def split_words(data_hex: str) -> list[int]:
    """Split ABI-encoded data into unsigned 32-byte words."""
    raw = bytes.fromhex(data_hex.removeprefix("0x"))
    if len(raw) % 32:
        raise ValueError(f"ABI data is {len(raw)} bytes, not a multiple of 32")
    return [int.from_bytes(raw[i : i + 32], "big") for i in range(0, len(raw), 32)]


def to_signed(word: int, bits: int = 256) -> int:
    """Read the low `bits` bits of a word as a two's-complement integer."""
    word &= (1 << bits) - 1
    return word - (1 << bits) if word >> (bits - 1) else word


def decode_address(word: int) -> str:
    return "0x" + format(word & _ADDRESS_MASK, "040x")


def encode_address(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def encode_uint(value: int) -> str:
    if value < 0:
        raise ValueError("a uint cannot be negative")
    return format(value, "064x")
