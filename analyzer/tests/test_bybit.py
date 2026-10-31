import io
import json
import zipfile
from datetime import date
from pathlib import Path

import httpx
import polars as pl
import pytest

from propamm_analyzer.ingest import bybit


def _message(kind: str, cts: int, bids: list[list[str]], asks: list[list[str]]) -> str:
    data = {"s": "MONUSDT", "b": bids, "a": asks, "u": 1, "seq": 1}
    return json.dumps(
        {"topic": "orderbook.200.MONUSDT", "ts": cts + 10, "type": kind, "cts": cts, "data": data}
    )


def test_deltas_move_the_book_and_only_changes_at_the_top_are_kept() -> None:
    messages = [
        _message("snapshot", 1_000, [["10", "1"], ["9", "5"]], [["11", "2"], ["12", "1"]]),
        _message("delta", 1_100, [["8", "3"]], []),  # below the top: no row
        _message("delta", 1_200, [["10", "0"]], []),  # the best bid goes: 9 is the best
        _message("delta", 1_300, [], [["11", "4"]]),  # the best ask's size changes
        _message("delta", 1_400, [["11.5", "1"]], []),  # crossed: skipped
        _message("delta", 1_500, [], [["11", "0"]]),  # uncrossed by the ask leaving
        "",
    ]
    assert bybit.top_of_book(messages).rows() == [
        (1_000, 10.0, 1.0, 11.0, 2.0),
        (1_200, 9.0, 5.0, 11.0, 2.0),
        (1_300, 9.0, 5.0, 11.0, 4.0),
        (1_500, 11.5, 1.0, 12.0, 1.0),
    ]


def test_a_snapshot_replaces_the_book() -> None:
    messages = [
        _message("snapshot", 1_000, [["10", "1"]], [["11", "1"]]),
        _message("snapshot", 2_000, [["20", "1"]], [["21", "1"]]),
    ]
    assert bybit.top_of_book(messages)["bid"].to_list() == [10.0, 20.0]


def test_the_microprice_leans_toward_the_side_that_will_trade_next() -> None:
    book = pl.DataFrame(
        {
            "ts_ms": [1, 2, 2],
            "bid": [10.0, 10.0, 10.0],
            "bid_size": [3.0, 1.0, 1.0],
            "ask": [11.0, 11.0, 11.0],
            "ask_size": [1.0, 1.0, 3.0],
        },
        schema=bybit.BOOK_SCHEMA,
    )
    # Three to buy and one to sell: the ask is likelier to be lifted. Of two rows in one
    # millisecond, the last stands.
    assert bybit.microprice(book).rows() == [(1, pytest.approx(10.75)), (2, pytest.approx(10.25))]


def test_prices_convert_at_the_last_rate_known() -> None:
    rate = pl.DataFrame({"ts_ms": [2, 5], "price": [1.0002, 0.9998]})
    prices = pl.DataFrame({"ts_ms": [1, 3, 6], "price": [10.0, 10.0, 10.0]})
    assert bybit.in_quote(prices, rate).rows() == [
        (3, pytest.approx(10 / 1.0002)),
        (6, pytest.approx(10 / 0.9998)),
    ]


def test_archives_are_downloaded_once_and_read(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        line = _message("snapshot", 1_000, [["10", "1"]], [["11", "1"]])
        archive.writestr("2026-10-29_MONUSDT_ob200.data", line + "\n")
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, content=payload.getvalue())

    http = httpx.Client(transport=httpx.MockTransport(handler))
    day = date(2026, 10, 29)
    path = bybit.download_daily_book("MONUSDT", day, tmp_path, http=http)
    assert bybit.download_daily_book("MONUSDT", day, tmp_path, http=http) == path
    assert urls == [
        "https://quote-saver.bycsi.com/orderbook/spot/MONUSDT/2026-10-29_MONUSDT_ob200.data.zip"
    ]
    assert bybit.read_book(path).rows() == [(1_000, 10.0, 1.0, 11.0, 1.0)]
