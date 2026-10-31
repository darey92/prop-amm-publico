import struct

import pytest

from propamm_analyzer.ingest.pyth import (
    PushedUpdate,
    decode_update,
    merge_updates,
    messages_frame,
    summarize_messages,
    updates_in_calldata,
)


def _vaa(slot: int, signatures: int = 3) -> bytes:
    header = bytes([1]) + (4).to_bytes(4, "big") + bytes([signatures]) + b"\x01" * 66 * signatures
    body = (
        bytes(4)
        + bytes(4)
        + (26).to_bytes(2, "big")
        + b"\x02" * 32
        + (7).to_bytes(8, "big")
        + b"\x01"
    )
    payload = (
        b"AUWV" + b"\x00" + slot.to_bytes(8, "big") + (10_000).to_bytes(4, "big") + b"\x03" * 20
    )
    return header + body + payload


def _message(feed: int, price: int, conf: int, publish_time: int, prev_publish_time: int) -> bytes:
    fields = struct.pack(">qQiqqqQ", price, conf, -8, publish_time, prev_publish_time, price, conf)
    return b"\x00" + feed.to_bytes(32, "big") + fields


def _blob(vaa: bytes, messages: list[bytes], proof_nodes: int = 2) -> bytes:
    out = b"PNAU" + bytes([1, 0, 0, 0]) + len(vaa).to_bytes(2, "big") + vaa + bytes([len(messages)])
    for message in messages:
        proof = b"\x04" * 20 * proof_nodes
        out += len(message).to_bytes(2, "big") + message + bytes([proof_nodes]) + proof
    return out


def _abi_bytes(selector: bytes, value: bytes) -> bytes:
    """`selector` applied to a single `bytes` argument."""
    padded = value + bytes(-len(value) % 32)
    return selector + (32).to_bytes(32, "big") + len(value).to_bytes(32, "big") + padded


ETH = _message(0xE7, 2_350_97498720, 1_52501280, 1_793_293_877, 1_793_293_876)
SOL = _message(0x50, 100_36598962, 5_224128, 1_793_293_877, 1_793_293_870)


def test_decode_reads_the_vaa_and_messages_and_round_trips() -> None:
    blob = _blob(_vaa(slot=35_865_877_540), [ETH])
    update = decode_update(blob)
    assert (update.slot, update.signatures, update.guardian_set_index) == (35_865_877_540, 3, 4)
    (message,) = update.messages
    assert message.feed_id == "0x" + format(0xE7, "064x")
    assert (message.price, message.expo) == (2_350_97498720, -8)
    assert (message.publish_time, message.prev_publish_time) == (1_793_293_877, 1_793_293_876)
    assert message.conf_bps == pytest.approx(6.4867, abs=1e-4)
    assert update.encode() == blob


@pytest.mark.parametrize("damage", ["truncated", "trailing", "magic"])
def test_decode_rejects_malformed_updates(damage: str) -> None:
    blob = _blob(_vaa(slot=1), [ETH])
    broken = {"truncated": blob[:-1], "trailing": blob + b"\x00", "magic": b"PNAX" + blob[4:]}
    with pytest.raises(ValueError):
        decode_update(broken[damage])


def test_merge_combines_messages_signed_by_the_same_vaa() -> None:
    vaa = _vaa(slot=9)
    merged = merge_updates([decode_update(_blob(vaa, [ETH])), decode_update(_blob(vaa, [SOL]))])
    assert merged.encode() == _blob(vaa, [ETH, SOL])
    assert [m.price for m in decode_update(merged.encode()).messages] == [
        2_350_97498720,
        100_36598962,
    ]
    with pytest.raises(ValueError):
        merge_updates([decode_update(_blob(vaa, [ETH])), decode_update(_blob(_vaa(10), [SOL]))])


def test_updates_are_found_at_any_abi_nesting_depth() -> None:
    first = _blob(_vaa(slot=1), [ETH])
    second = _blob(_vaa(slot=2), [SOL])
    inner = _abi_bytes(b"\xaa\xbb\xcc\xdd", first)
    # An outer call (an ERC-4337 bundle, say) whose argument is the inner call, plus a direct one.
    calldata = _abi_bytes(b"\x11\x22\x33\x44", inner) + _abi_bytes(b"\x55\x66\x77\x88", second)
    noise = (40).to_bytes(32, "big") + b"PNAU" + bytes(36)  # magic with a length, but not an update
    assert updates_in_calldata(calldata + noise) == [first, second]


def test_summary_reports_confidence_and_timing_per_feed() -> None:
    later_in_second = _message(0xE7, 2_351_00000000, 1_50000000, 1_793_293_877, 1_793_293_877)
    pushed = [
        PushedUpdate(1, 0, "0x01", decode_update(_blob(_vaa(slot=1), [ETH, SOL]))),
        PushedUpdate(2, 0, "0x02", decode_update(_blob(_vaa(slot=2), [later_in_second]))),
    ]
    summary = summarize_messages(messages_frame(pushed), {"0x" + format(0xE7, "064x"): "ETH"})
    eth = summary.filter(summary["symbol"] == "ETH").row(0, named=True)
    sol = summary.filter(summary["symbol"].is_null()).row(0, named=True)
    assert (eth["samples"], eth["first_of_second"], eth["gap_1s"]) == (2, 0.5, 0.5)
    assert (sol["samples"], sol["first_of_second"], sol["gap_1s"]) == (1, 1.0, 0.0)
