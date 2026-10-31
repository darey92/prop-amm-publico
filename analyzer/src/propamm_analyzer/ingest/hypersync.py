"""HyperSync: the logs that match a set of filters, with their blocks and transactions.

HyperSync (envio.dev) serves pre-indexed chain data over HTTP, so weeks of logs take seconds
where a free RPC endpoint manages hours. It needs a free API token, read from `ENVIO_API_TOKEN`
(see `.env.example`). Each answer covers as much of the range as the server scanned in one go,
and `next_block` says where to continue.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from propamm_analyzer.ingest.rpc import Log

URLS = {
    "ethereum": "https://eth.hypersync.xyz",
    "base": "https://base.hypersync.xyz",
    "monad": "https://monad.hypersync.xyz",
}
TOKEN_ENV = "ENVIO_API_TOKEN"

_LOG_FIELDS = [
    "block_number",
    "transaction_index",
    "log_index",
    "transaction_hash",
    "address",
    "data",
    "topic0",
    "topic1",
    "topic2",
    "topic3",
]
_RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class LogFilter:
    """Logs from any of `addresses` (any contract if empty) whose topics match position by
    position. Each position lists the values it accepts; an empty position accepts anything."""

    addresses: tuple[str, ...] = ()
    topics: tuple[tuple[str, ...], ...] = ()

    def to_json(self) -> dict[str, Any]:
        query: dict[str, Any] = {"topics": [list(position) for position in self.topics]}
        if self.addresses:
            query["address"] = list(self.addresses)
        return query


@dataclass
class LogScan:
    logs: list[Log] = field(default_factory=list)
    """In the JSON-RPC shape (`blockNumber`, `topics`, ...), so the RPC decoders apply as is."""
    timestamps: dict[int, int] = field(default_factory=dict)
    """Unix time of every block that holds a matching log."""
    transactions: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    """`from` and `to` of every transaction that holds a matching log (`to` is None for a
    contract creation)."""


def _int(value: int | str) -> int:
    """HyperSync sends most numbers as integers, but block timestamps as hex strings."""
    return value if isinstance(value, int) else int(value, 16)


def _rpc_log(log: dict[str, Any]) -> Log:
    return {
        "address": log["address"],
        "blockNumber": hex(_int(log["block_number"])),
        "transactionIndex": hex(_int(log["transaction_index"])),
        "logIndex": hex(_int(log["log_index"])),
        "transactionHash": log["transaction_hash"],
        "topics": [log[f"topic{i}"] for i in range(4) if log.get(f"topic{i}")],
        "data": log.get("data") or "0x",
    }


class HyperSyncClient:
    def __init__(
        self, url: str, token: str, *, http: httpx.Client | None = None, retries: int = 8
    ) -> None:
        if not token:
            raise ValueError(f"HyperSync needs an API token: set {TOKEN_ENV} (see .env.example)")
        self.url = url.rstrip("/")
        self.http = http or httpx.Client(timeout=120)
        self._headers = {"Authorization": f"Bearer {token}"}
        self.retries = retries

    @classmethod
    def for_chain(cls, chain: str) -> HyperSyncClient:
        return cls(URLS[chain], os.environ.get(TOKEN_ENV, ""))

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        for attempt in range(self.retries + 1):
            last = attempt == self.retries
            try:
                response = self.http.request(
                    method, self.url + path, json=body, headers=self._headers
                )
            except httpx.TransportError:
                if last:
                    raise
            else:
                if response.status_code not in _RETRY_STATUS or last:
                    response.raise_for_status()
                    return response.json()
            time.sleep(min(2**attempt, 30))
        raise AssertionError("unreachable")

    def height(self) -> int:
        """The last block HyperSync has indexed."""
        return _int(self._request("GET", "/height")["height"])

    def _pages(
        self, query: dict[str, Any], from_block: int, to_block: int
    ) -> Iterator[dict[str, Any]]:
        """The answers to `query` over [from_block, to_block], page by page."""
        start, end = from_block, to_block + 1  # HyperSync's to_block is exclusive
        while start < end:
            page = self._request("POST", "/query", {"from_block": start, "to_block": end, **query})
            yield page
            next_block = _int(page["next_block"])
            if next_block <= start:
                raise RuntimeError(
                    f"HyperSync made no progress at block {start}; it has indexed up to "
                    f"{page.get('archive_height')}"
                )
            start = next_block

    def logs(self, filters: Sequence[LogFilter], from_block: int, to_block: int) -> LogScan:
        """Every log in [from_block, to_block] that matches any of `filters`, in chain order."""
        scan = LogScan()
        query = {
            "logs": [log_filter.to_json() for log_filter in filters],
            "field_selection": {
                "log": _LOG_FIELDS,
                "transaction": ["hash", "from", "to"],
                "block": ["number", "timestamp"],
            },
        }
        for page in self._pages(query, from_block, to_block):
            for batch in page["data"]:
                scan.logs += (_rpc_log(log) for log in batch.get("logs", []))
                for block in batch.get("blocks", []):
                    scan.timestamps[_int(block["number"])] = _int(block["timestamp"])
                for tx in batch.get("transactions", []):
                    scan.transactions[tx["hash"]] = (tx["from"], tx.get("to"))
        return scan

    def block_times(self, from_block: int, to_block: int) -> dict[int, int]:
        """Unix time of every block in [from_block, to_block], whether it holds logs or not."""
        # Pages of headers alone hold under 2,000 blocks unless asked for more.
        query = {
            "include_all_blocks": True,
            "max_num_blocks": 100_000,
            "field_selection": {"block": ["number", "timestamp"]},
        }
        times = {}
        for page in self._pages(query, from_block, to_block):
            for batch in page["data"]:
                for block in batch.get("blocks", []):
                    times[_int(block["number"])] = _int(block["timestamp"])
        return times
