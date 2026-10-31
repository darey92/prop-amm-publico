"""Binance spot klines from data.binance.vision, the free public archive."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import httpx
import polars as pl

from propamm_analyzer.normalize import PRICE_SCHEMA

ARCHIVE_URL = "https://data.binance.vision/data/spot/daily/klines"

KLINE_COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
)

INTERVAL_MS = {"1s": 1_000, "1m": 60_000, "5m": 300_000, "1h": 3_600_000}


def archive_name(symbol: str, interval: str, day: date) -> str:
    return f"{symbol}-{interval}-{day.isoformat()}.zip"


def archive_url(symbol: str, interval: str, day: date) -> str:
    return f"{ARCHIVE_URL}/{symbol}/{interval}/{archive_name(symbol, interval, day)}"


def parse_checksum(text: str) -> str:
    """The SHA-256 digest in a `.CHECKSUM` file, whose format is `<sha256>  <filename>`."""
    fields = text.split()
    digest = fields[0].lower() if fields else ""
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError(f"not a SHA-256 checksum: {text[:80]!r}")
    return digest


def download_daily_klines(
    symbol: str, interval: str, day: date, raw_dir: Path, *, http: httpx.Client | None = None
) -> Path:
    """Download one daily archive into `raw_dir/<symbol>/<interval>/`, verifying its SHA-256.

    An archive already on disk with the right checksum is reused.
    """
    client = http or httpx.Client(timeout=120.0, follow_redirects=True)
    url = archive_url(symbol, interval, day)
    checksum = client.get(url + ".CHECKSUM")
    checksum.raise_for_status()
    expected = parse_checksum(checksum.text)

    target = raw_dir / symbol / interval / archive_name(symbol, interval, day)
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
        return target

    response = client.get(url)
    response.raise_for_status()
    actual = hashlib.sha256(response.content).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch for {url}: expected {expected}, got {actual}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    return target


def read_klines(path: Path, interval: str) -> pl.DataFrame:
    """Read an archive into reference prices: `ts_ms` (end of each kline) and `price` (close)."""
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        if len(members) != 1:
            raise ValueError(f"expected one CSV in {path}, found {members}")
        raw = archive.read(members[0])
    return parse_klines_csv(raw, interval)


def parse_klines_csv(raw: bytes, interval: str) -> pl.DataFrame:
    """Parse kline CSV bytes, with or without a header row.

    Binance spot archives switched from millisecond to microsecond timestamps on 2025-01-01,
    so both are accepted. Prices are stamped at the end of the kline, when the close is known.
    """
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval!r}; known: {sorted(INTERVAL_MS)}")
    frame = pl.read_csv(io.BytesIO(raw), has_header=not raw[:1].isdigit(), infer_schema_length=0)
    if frame.width != len(KLINE_COLUMNS):
        raise ValueError(f"expected {len(KLINE_COLUMNS)} kline columns, got {frame.width}")
    frame.columns = list(KLINE_COLUMNS)
    if frame.is_empty():
        return pl.DataFrame(schema=PRICE_SCHEMA)

    open_time = frame["open_time"].cast(pl.Int64)
    first = int(open_time[0])
    if first >= 10**15:
        open_ms = open_time // 1_000
    elif first >= 10**12:
        open_ms = open_time
    else:
        raise ValueError(f"unrecognised timestamp unit in open_time {first}")
    return pl.DataFrame(
        {"ts_ms": open_ms + INTERVAL_MS[interval], "price": frame["close"].cast(pl.Float64)},
        schema=PRICE_SCHEMA,
    ).sort("ts_ms")
