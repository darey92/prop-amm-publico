"""Bybit spot order books from quote-saver.bycsi.com, Bybit's free public archive.

The paper this project reproduces marks Monad's trades against Bybit's MONUSDT book: the
microprice, the mid weighted by the size on the other side, converted to USDC through Bybit's
USDCUSDT book. Each daily archive holds one JSON message per line, as Bybit's `orderbook.200`
stream sends them: a `snapshot` of the top 200 levels of each side, then `delta`s that set a
level's size, where a size of 0 removes the level. Bybit publishes no checksums, so the only
check is the zip's own CRC, made as the archive is read.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import httpx
import polars as pl

ARCHIVE_URL = "https://quote-saver.bycsi.com/orderbook/spot"

BOOK_SCHEMA = pl.Schema(
    {
        "ts_ms": pl.Int64,
        "bid": pl.Float64,
        "bid_size": pl.Float64,
        "ask": pl.Float64,
        "ask_size": pl.Float64,
    }
)


def archive_name(symbol: str, day: date) -> str:
    return f"{day.isoformat()}_{symbol}_ob200.data.zip"


def download_daily_book(
    symbol: str, day: date, raw_dir: Path, *, http: httpx.Client | None = None
) -> Path:
    """Download one day's archive into `raw_dir/<symbol>/`. One already there is reused."""
    target = raw_dir / symbol / archive_name(symbol, day)
    if target.exists() and zipfile.is_zipfile(target):
        return target
    client = http or httpx.Client(timeout=300.0, follow_redirects=True)
    response = client.get(f"{ARCHIVE_URL}/{symbol}/{archive_name(symbol, day)}")
    response.raise_for_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(response.content)
    return target


def top_of_book(messages: Iterable[str | bytes]) -> pl.DataFrame:
    """The best bid and ask with their sizes after each message that changes any of them,
    stamped with the matching engine's time (`cts`). One-sided and crossed books are skipped."""
    bids: dict[float, float] = {}
    asks: dict[float, float] = {}
    rows: list[tuple[int, float, float, float, float]] = []
    last: tuple[float, float, float, float] | None = None
    for line in messages:
        if not line.strip():
            continue
        message = json.loads(line)
        data = message["data"]
        if message["type"] == "snapshot":
            bids.clear()
            asks.clear()
        for book, levels in ((bids, data["b"]), (asks, data["a"])):
            for price, size in levels:
                if float(size) == 0:
                    book.pop(float(price), None)
                else:
                    book[float(price)] = float(size)
        if not bids or not asks:
            continue
        bid, ask = max(bids), min(asks)
        if bid >= ask:
            continue
        top = (bid, bids[bid], ask, asks[ask])
        if top != last:
            rows.append((int(message.get("cts") or message["ts"]), *top))
            last = top
    return pl.DataFrame(rows, schema=BOOK_SCHEMA, orient="row")


def read_book(path: Path) -> pl.DataFrame:
    """The top of the book through one daily archive."""
    with zipfile.ZipFile(path) as archive:
        members = archive.namelist()
        if len(members) != 1:
            raise ValueError(f"expected one data file in {path}, found {members}")
        with archive.open(members[0]) as stream:
            return top_of_book(stream)


def microprice(book: pl.DataFrame) -> pl.DataFrame:
    """Reference prices from the top of the book: (bid * ask_size + ask * bid_size) /
    (bid_size + ask_size), the last one where several share a millisecond."""
    weighted = pl.col("bid") * pl.col("ask_size") + pl.col("ask") * pl.col("bid_size")
    return book.select(
        "ts_ms", (weighted / (pl.col("bid_size") + pl.col("ask_size"))).alias("price")
    ).unique("ts_ms", keep="last", maintain_order=True)


def in_quote(prices: pl.DataFrame, rate: pl.DataFrame) -> pl.DataFrame:
    """`prices` divided by the last `rate` known at each, such as MONUSDT by USDCUSDT for MON in
    USDC. Prices from before the first rate are dropped."""
    rates = rate.select(pl.col("ts_ms").alias("rate_ms"), pl.col("price").alias("rate"))
    return (
        prices.sort("ts_ms")
        .join_asof(rates.sort("rate_ms"), left_on="ts_ms", right_on="rate_ms", strategy="backward")
        .drop_nulls("rate")
        .select("ts_ms", (pl.col("price") / pl.col("rate")).alias("price"))
    )
