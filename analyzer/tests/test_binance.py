import io
import zipfile
from pathlib import Path

import pytest

from propamm_analyzer.ingest.binance import parse_checksum, read_klines

DAY_START_MS = 1_793_145_600_000  # 2026-10-28 00:00:00 UTC

HEADER = (
    "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
    "taker_buy_volume,taker_buy_quote_volume,ignore"
)


def _archive(tmp_path: Path, lines: list[str]) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ETHUSDC-1s-2026-10-28.csv", "\n".join(lines) + "\n")
    path = tmp_path / "ETHUSDC-1s-2026-10-28.zip"
    path.write_bytes(buffer.getvalue())
    return path


def _row(open_time: int, close: float) -> str:
    return f"{open_time},{close},{close},{close},{close},1.5,{open_time + 999},6000,12,0.7,2800,0"


def test_microsecond_timestamps_are_stamped_at_kline_end(tmp_path: Path) -> None:
    start_us = DAY_START_MS * 1_000
    path = _archive(tmp_path, [_row(start_us, 4000.5), _row(start_us + 1_000_000, 4001.25)])
    prices = read_klines(path, "1s")
    assert prices["ts_ms"].to_list() == [DAY_START_MS + 1_000, DAY_START_MS + 2_000]
    assert prices["price"].to_list() == [4000.5, 4001.25]


def test_millisecond_timestamps_with_a_header_row(tmp_path: Path) -> None:
    path = _archive(tmp_path, [HEADER, _row(DAY_START_MS, 3999.0)])
    prices = read_klines(path, "1s")
    assert prices["ts_ms"].to_list() == [DAY_START_MS + 1_000]
    assert prices["price"].to_list() == [3999.0]


def test_parse_checksum() -> None:
    digest = "ab" * 32
    assert parse_checksum(f"{digest}  ETHUSDC-1s-2026-10-28.zip\n") == digest
    with pytest.raises(ValueError):
        parse_checksum("not a checksum")
