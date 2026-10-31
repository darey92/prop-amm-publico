import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from propamm_analyzer.ingest import hypersync
from propamm_analyzer.ingest.hypersync import HyperSyncClient, LogFilter

POOL = "0x00000000000000000000000000000000000000aa"
TOPIC = "0x" + "11" * 32
SENDER_TOPIC = "0x" + "22" * 32


def _log(block: int, index: int, tx: str) -> dict[str, Any]:
    return {
        "block_number": block,
        "transaction_index": 0,
        "log_index": index,
        "transaction_hash": tx,
        "address": POOL,
        "data": "0x" + "00" * 32,
        "topic0": TOPIC,
        "topic1": SENDER_TOPIC,
        "topic2": None,
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> HyperSyncClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HyperSyncClient("https://hypersync.invalid", "secret", http=http)


def test_pages_are_followed_and_logs_come_back_in_rpc_shape() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        body = json.loads(request.content)
        bodies.append(body)
        if body["from_block"] == 100:
            batch = {
                "logs": [_log(100, 3, "0xa")],
                "blocks": [{"number": 100, "timestamp": "0x10"}],
                "transactions": [{"hash": "0xa", "from": "0xf1", "to": "0xr1"}],
            }
            return httpx.Response(200, json={"data": [batch], "next_block": 150})
        batch = {
            "logs": [_log(170, 0, "0xb")],
            "blocks": [{"number": 170, "timestamp": 32}],
            "transactions": [{"hash": "0xb", "from": "0xf2", "to": None}],
        }
        return httpx.Response(200, json={"data": [batch], "next_block": 201})

    scan = _client(handler).logs([LogFilter((POOL,), ((TOPIC,), ()))], 100, 200)
    # HyperSync's to_block is exclusive, so the inclusive 200 goes out as 201.
    assert [(body["from_block"], body["to_block"]) for body in bodies] == [(100, 201), (150, 201)]
    assert bodies[0]["logs"] == [{"address": [POOL], "topics": [[TOPIC], []]}]
    assert [log["blockNumber"] for log in scan.logs] == ["0x64", "0xaa"]
    assert scan.logs[0]["logIndex"] == "0x3"
    assert scan.logs[0]["topics"] == [TOPIC, SENDER_TOPIC]  # the null topic2 is dropped
    assert scan.timestamps == {100: 16, 170: 32}
    assert scan.transactions == {"0xa": ("0xf1", "0xr1"), "0xb": ("0xf2", None)}


def test_busy_answers_are_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    answers = iter(
        [httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"height": 42})]
    )
    assert _client(lambda request: next(answers)).height() == 42


def test_other_errors_and_stalls_are_raised() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        _client(lambda request: httpx.Response(401, json={"error": "malformed"})).height()
    stalled = {"data": [], "next_block": 100, "archive_height": 100}
    with pytest.raises(RuntimeError, match="no progress"):
        _client(lambda request: httpx.Response(200, json=stalled)).logs([], 100, 200)


def test_a_missing_token_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(hypersync.TOKEN_ENV, raising=False)
    with pytest.raises(ValueError, match="ENVIO_API_TOKEN"):
        HyperSyncClient.for_chain("ethereum")


def test_block_times_cover_every_header_across_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["include_all_blocks"] is True
        assert body["max_num_blocks"] == 100_000
        assert body["field_selection"] == {"block": ["number", "timestamp"]}
        if body["from_block"] == 10:
            blocks = [{"number": 10, "timestamp": "0x64"}, {"number": 11, "timestamp": 100}]
            return httpx.Response(200, json={"data": [{"blocks": blocks}], "next_block": 12})
        batch = {"blocks": [{"number": 12, "timestamp": 101}]}
        return httpx.Response(200, json={"data": [batch], "next_block": 13})

    assert _client(handler).block_times(10, 12) == {10: 100, 11: 100, 12: 101}
