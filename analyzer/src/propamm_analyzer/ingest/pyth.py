"""Pyth Core price updates: the accumulator wire format, and updates already pushed on-chain.

Hermes serves these binary updates, but since 2026 its price endpoints need an API key. Updates
that someone has pushed on-chain are public: their transactions carry the signed update data,
which is enough for fork tests and for a first look at confidence intervals.

Wire format (pyth-crosschain `pythnet_sdk/src/wire.rs`), all integers big-endian:

    "PNAU" | major u8 | minor u8 | trailing header (u8 length + bytes) | update type u8 (0)
    | VAA (u16 length + bytes) | u8 count of { message (u16 length + bytes), proof (u8 count
    of 20-byte nodes) }

A price message is `type u8 (0) | feed id [32] | price i64 | conf u64 | expo i32 |
publish time i64 | prev publish time i64 | EMA price i64 | EMA conf u64`.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import polars as pl

from propamm_analyzer.abi import event_topic
from propamm_analyzer.ingest.rpc import RpcClient

MAGIC = b"PNAU"
_VAA_PAYLOAD_MAGIC = b"AUWV"
_PRICE_MESSAGE = 0
_PRICE_FIELDS = struct.Struct(">qQiqqqQ")
_SIGNATURE_SIZE = 66  # guardian index + 65-byte signature
_VAA_BODY_HEADER = 4 + 4 + 2 + 32 + 8 + 1  # timestamp, nonce, chain, emitter, sequence, level

PRICE_FEED_UPDATE_TOPIC = event_topic("PriceFeedUpdate(bytes32,uint64,int64,uint64)")


@dataclass(frozen=True)
class PriceMessage:
    feed_id: str
    """0x-prefixed, lower-case hex."""
    price: int
    conf: int
    expo: int
    publish_time: int
    prev_publish_time: int
    ema_price: int
    ema_conf: int

    @property
    def conf_bps(self) -> float:
        """Confidence relative to the price, in basis points."""
        return self.conf / abs(self.price) * 1e4


@dataclass(frozen=True)
class AccumulatorUpdate:
    """One `PNAU` blob: a Wormhole VAA over a Merkle root, and messages with their proofs."""

    header: bytes
    """Everything before the VAA length: magic, version, trailing header and update type."""
    vaa: bytes
    raw_messages: tuple[bytes, ...]
    proofs: tuple[bytes, ...]

    @property
    def guardian_set_index(self) -> int:
        return int.from_bytes(self.vaa[1:5], "big")

    @property
    def signatures(self) -> int:
        return self.vaa[5]

    @property
    def slot(self) -> int:
        """The Pythnet slot whose Merkle root the VAA signs."""
        payload = self.vaa[6 + _SIGNATURE_SIZE * self.signatures + _VAA_BODY_HEADER :]
        if payload[:4] != _VAA_PAYLOAD_MAGIC:
            raise ValueError("the VAA payload is not a Pyth Merkle root")
        return int.from_bytes(payload[5:13], "big")

    @property
    def messages(self) -> tuple[PriceMessage, ...]:
        """The price messages. Other message types (TWAP) are skipped."""
        return tuple(
            _decode_price_message(raw) for raw in self.raw_messages if raw[0] == _PRICE_MESSAGE
        )

    def encode(self) -> bytes:
        parts = [self.header, len(self.vaa).to_bytes(2, "big"), self.vaa]
        parts.append(len(self.raw_messages).to_bytes(1, "big"))
        for raw, proof in zip(self.raw_messages, self.proofs, strict=True):
            parts += [
                len(raw).to_bytes(2, "big"),
                raw,
                (len(proof) // 20).to_bytes(1, "big"),
                proof,
            ]
        return b"".join(parts)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        if self.pos + size > len(self.data):
            raise ValueError(f"truncated update: wanted {size} bytes at offset {self.pos}")
        chunk = self.data[self.pos : self.pos + size]
        self.pos += size
        return chunk

    def uint(self, size: int) -> int:
        return int.from_bytes(self.take(size), "big")


def decode_update(blob: bytes) -> AccumulatorUpdate:
    """Parse a `PNAU` blob, rejecting anything malformed or with trailing bytes."""
    reader = _Reader(blob)
    if reader.take(4) != MAGIC:
        raise ValueError("not a Pyth accumulator update (no PNAU magic)")
    major = reader.uint(1)
    reader.take(1)  # minor version: new minors only add trailing header fields
    if major != 1:
        raise ValueError(f"unsupported update major version {major}")
    reader.take(reader.uint(1))
    if reader.uint(1) != 0:
        raise ValueError("only WormholeMerkle updates are supported")
    header = blob[: reader.pos]
    vaa = reader.take(reader.uint(2))
    raw_messages: list[bytes] = []
    proofs: list[bytes] = []
    for _ in range(reader.uint(1)):
        raw_messages.append(reader.take(reader.uint(2)))
        proofs.append(reader.take(20 * reader.uint(1)))
    if reader.pos != len(blob):
        raise ValueError(f"{len(blob) - reader.pos} trailing bytes after the last message")
    if any(not raw for raw in raw_messages):
        raise ValueError("empty message")
    return AccumulatorUpdate(header, vaa, tuple(raw_messages), tuple(proofs))


def _decode_price_message(raw: bytes) -> PriceMessage:
    if len(raw) < 33 + _PRICE_FIELDS.size:
        raise ValueError(f"price message is {len(raw)} bytes")
    price, conf, expo, publish_time, prev_publish_time, ema_price, ema_conf = (
        _PRICE_FIELDS.unpack_from(raw, 33)
    )
    return PriceMessage(
        feed_id="0x" + raw[1:33].hex(),
        price=price,
        conf=conf,
        expo=expo,
        publish_time=publish_time,
        prev_publish_time=prev_publish_time,
        ema_price=ema_price,
        ema_conf=ema_conf,
    )


def merge_updates(updates: Iterable[AccumulatorUpdate]) -> AccumulatorUpdate:
    """Combine updates signed by the same VAA (one Pythnet slot) into one blob.

    Every message carries its own proof against the VAA's Merkle root, so this is the blob
    Hermes returns when asked for several feeds of the same slot.
    """
    updates = list(updates)
    if not updates:
        raise ValueError("nothing to merge")
    first = updates[0]
    if any(u.vaa != first.vaa or u.header != first.header for u in updates[1:]):
        raise ValueError("updates from different VAAs cannot be merged")
    raw_messages = tuple(raw for u in updates for raw in u.raw_messages)
    proofs = tuple(proof for u in updates for proof in u.proofs)
    if len(raw_messages) > 255:
        raise ValueError("an update holds at most 255 messages")
    return AccumulatorUpdate(first.header, first.vaa, raw_messages, proofs)


def updates_in_calldata(data: bytes) -> list[bytes]:
    """The `PNAU` blobs inside transaction input.

    ABI encoding puts a 32-byte length right before each `bytes` value, at any nesting depth
    (a call inside a multicall or an ERC-4337 bundle), so every magic preceded by a plausible
    length is tried. Only blobs that decode are kept.
    """
    found: list[bytes] = []
    start = 0
    while (index := data.find(MAGIC, start)) != -1:
        start = index + 1
        if index < 32:
            continue
        length = int.from_bytes(data[index - 32 : index], "big")
        if length > len(data) - index:
            continue
        blob = data[index : index + length]
        try:
            decode_update(blob)
        except ValueError:
            continue
        if blob not in found:
            found.append(blob)
    return found


@dataclass(frozen=True)
class PushedUpdate:
    block: int
    tx_index: int
    tx_hash: str
    update: AccumulatorUpdate


def fetch_pushed_updates(
    rpc: RpcClient, pyth: str, from_block: int, to_block: int
) -> Iterator[PushedUpdate]:
    """Updates in the transactions that stored a fresh Pyth price in [from_block, to_block].

    The contract only emits `PriceFeedUpdate` when it stores a price, so updates that were only
    parsed (`parsePriceFeedUpdates*`) are not found.
    """
    logs = rpc.iter_logs(pyth, [PRICE_FEED_UPDATE_TOPIC], from_block, to_block)
    transactions = sorted(
        {
            (int(log["blockNumber"], 16), int(log["transactionIndex"], 16), log["transactionHash"])
            for log in logs
            if not log.get("removed", False)
        }
    )
    for block, tx_index, tx_hash in transactions:
        data = bytes.fromhex(rpc.transaction(tx_hash)["input"].removeprefix("0x"))
        for blob in updates_in_calldata(data):
            yield PushedUpdate(block, tx_index, tx_hash, decode_update(blob))


MESSAGE_SCHEMA = pl.Schema(
    {
        "block": pl.Int64,
        "tx_index": pl.Int64,
        "tx_hash": pl.String,
        "slot": pl.Int64,
        "messages": pl.Int64,
        "feed_id": pl.String,
        "price": pl.Int64,
        "conf": pl.Int64,
        "expo": pl.Int64,
        "publish_time": pl.Int64,
        "prev_publish_time": pl.Int64,
        "conf_bps": pl.Float64,
    }
)


def messages_frame(pushed: Iterable[PushedUpdate]) -> pl.DataFrame:
    """One row per price message."""
    return pl.DataFrame(
        [
            {
                "block": p.block,
                "tx_index": p.tx_index,
                "tx_hash": p.tx_hash,
                "slot": p.update.slot,
                "messages": len(p.update.raw_messages),
                "feed_id": m.feed_id,
                "price": m.price,
                "conf": m.conf,
                "expo": m.expo,
                "publish_time": m.publish_time,
                "prev_publish_time": m.prev_publish_time,
                "conf_bps": m.conf_bps,
            }
            for p in pushed
            for m in p.update.messages
        ],
        schema=MESSAGE_SCHEMA,
    )


def summarize_messages(frame: pl.DataFrame, names: dict[str, str] | None = None) -> pl.DataFrame:
    """Per feed: sample count, confidence quantiles and two timing shares.

    - `first_of_second`: `prev_publish_time < publish_time`. Only such an update can pass
      `parsePriceFeedUpdatesUnique`; a later update of the same second never can.
    - `gap_1s`: `publish_time - prev_publish_time == 1`, the feed also published the second before.
    """
    names = names or {}
    gap = pl.col("publish_time") - pl.col("prev_publish_time")
    return (
        frame.group_by("feed_id")
        .agg(
            pl.len().alias("samples"),
            pl.col("conf_bps").median().alias("conf_bps_p50"),
            pl.col("conf_bps").quantile(0.9).alias("conf_bps_p90"),
            (gap > 0).mean().alias("first_of_second"),
            (gap == 1).mean().alias("gap_1s"),
        )
        .with_columns(pl.col("feed_id").replace_strict(names, default=None).alias("symbol"))
        .select(
            "symbol",
            "feed_id",
            "samples",
            "conf_bps_p50",
            "conf_bps_p90",
            "first_of_second",
            "gap_1s",
        )
        .sort("samples", descending=True)
    )
