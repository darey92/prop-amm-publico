"""Minimal EVM JSON-RPC client: retries, eth_getLogs range splitting and block timestamps."""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

from propamm_analyzer.abi import selector, split_words

Log = dict[str, Any]

# Fragments that providers use when an eth_getLogs block range or result set is too large.
_RANGE_HINTS = (
    "block range",
    "range too",
    "range is too",
    "more than",
    "too many results",
    "response size",
    "max range",
    "query timeout",
)
_RATE_LIMIT_HINTS = ("rate limit", "too many requests", "over capacity")
_INTERPOLATION_PROBES = 8


class RpcError(RuntimeError):
    """An error object returned by the node."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message

    @property
    def is_range_error(self) -> bool:
        message = self.message.lower()
        return any(hint in message for hint in _RANGE_HINTS)

    @property
    def is_rate_limited(self) -> bool:
        message = self.message.lower()
        return self.code == 429 or any(hint in message for hint in _RATE_LIMIT_HINTS)


class _RetryableError(Exception):
    pass


class RpcClient:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 6,
        http: httpx.Client | None = None,
    ) -> None:
        self.url = url
        self.max_retries = max_retries
        self._http = http or httpx.Client(timeout=timeout)
        self._request_id = 0

    def call(self, method: str, params: Sequence[Any]) -> Any:
        """Send one request. Transport errors, HTTP 429/5xx and rate limits are retried."""
        delay = 0.5
        for attempt in range(self.max_retries + 1):
            try:
                return self._call_once(method, params)
            except _RetryableError as exc:
                if attempt == self.max_retries:
                    raise RuntimeError(
                        f"{method} failed after {attempt + 1} attempts: {exc}"
                    ) from exc
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
        raise AssertionError("unreachable")

    def _call_once(self, method: str, params: Sequence[Any]) -> Any:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": list(params),
        }
        try:
            response = self._http.post(self.url, json=payload)
        except httpx.TransportError as exc:
            raise _RetryableError(repr(exc)) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise _RetryableError(f"HTTP {response.status_code}")
        response.raise_for_status()
        body = response.json()
        error = body.get("error")
        if error is None:
            return body["result"]
        rpc_error = RpcError(int(error.get("code", 0)), str(error.get("message", "")))
        if rpc_error.is_rate_limited:
            raise _RetryableError(str(rpc_error)) from rpc_error
        raise rpc_error

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_timestamp(self, number: int) -> int:
        block = self.call("eth_getBlockByNumber", [hex(number), False])
        if block is None:
            raise LookupError(f"block {number} not found")
        return int(block["timestamp"], 16)

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        return str(self.call("eth_call", [{"to": to, "data": data}, block]))

    def call_function(self, to: str, signature: str, args: str = "") -> list[int]:
        """Call a view function and return its result as 32-byte words.

        `args` is the hex ABI encoding of the arguments. An empty result (no contract at `to`)
        raises LookupError.
        """
        result = self.eth_call(to, selector(signature) + args)
        if result in ("0x", ""):
            raise LookupError(f"{signature} returned no data at {to} (not a contract?)")
        return split_words(result)

    def transaction(self, tx_hash: str) -> dict[str, Any]:
        tx = self.call("eth_getTransactionByHash", [tx_hash])
        if tx is None:
            raise LookupError(f"transaction {tx_hash} not found")
        return dict(tx)

    def get_logs(
        self, address: str, topics: Sequence[str | None], from_block: int, to_block: int
    ) -> list[Log]:
        query = {
            "address": address,
            "topics": list(topics),
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        return list(self.call("eth_getLogs", [query]))

    def iter_logs(
        self,
        address: str,
        topics: Sequence[str | None],
        from_block: int,
        to_block: int,
        *,
        chunk: int = 2_000,
        min_chunk: int = 1,
    ) -> Iterator[Log]:
        """Yield logs in block order, halving the range whenever the node says it is too large."""
        start, size = from_block, chunk
        while start <= to_block:
            end = min(start + size - 1, to_block)
            try:
                logs = self.get_logs(address, topics, start, end)
            except RpcError as exc:
                if not exc.is_range_error or size <= min_chunk:
                    raise
                size = max(size // 2, min_chunk)
                continue
            yield from logs
            start = end + 1

    def first_block_at_or_after(
        self, timestamp: int, *, lo: int | None = None, hi: int | None = None
    ) -> int:
        """The first block whose timestamp is >= `timestamp`.

        Interpolation search: on chains with a steady block time (Base: 2 s) it needs a few
        calls instead of ~25 for bisection, which keeps us under free RPC rate limits. After
        `_INTERPOLATION_PROBES` probes it falls back to bisection. Without `lo`, the lower
        bound is found by stepping back from `hi`, so recent targets never touch old blocks.
        """
        if hi is None:
            hi = self.block_number()
        hi_ts = self.block_timestamp(hi)
        if hi_ts < timestamp:
            raise ValueError(f"timestamp {timestamp} is after block {hi}")
        if lo is None:
            lo, lo_ts = self._block_before(timestamp, hi, hi_ts)
        else:
            lo_ts = self.block_timestamp(lo)
        if lo_ts >= timestamp:
            return lo
        # Invariant: ts(lo) < timestamp <= ts(hi).
        probes = 0
        while hi - lo > 1:
            if probes < _INTERPOLATION_PROBES:
                guess = lo + (timestamp - lo_ts) * (hi - lo) // (hi_ts - lo_ts)
                guess = min(max(guess, lo + 1), hi - 1)
            else:
                guess = (lo + hi) // 2
            probes += 1
            guess_ts = self.block_timestamp(guess)
            if guess_ts < timestamp:
                lo, lo_ts = guess, guess_ts
            else:
                hi, hi_ts = guess, guess_ts
        return hi

    def _block_before(self, timestamp: int, hi: int, hi_ts: int) -> tuple[int, int]:
        """A block older than `timestamp` (or block 0), stepping back from `hi`.

        Steps grow with the block time seen so far. Pruned providers such as PublicNode
        reject requests for ancient blocks, including genesis, so we avoid them.
        """
        step = 1_024
        while True:
            lo = max(hi - step, 0)
            lo_ts = self.block_timestamp(lo)
            if lo_ts < timestamp or lo == 0:
                return lo, lo_ts
            seconds_per_block = (hi_ts - lo_ts) / (hi - lo)
            blocks_needed = (hi_ts - timestamp) / seconds_per_block if seconds_per_block else 0
            step = max(step * 2, int(blocks_needed * 1.1) + 1)


class BlockClock:
    """Block number -> timestamp for a block range, with as few RPC calls as possible.

    OP Stack chains such as Base produce one block every 2 s, so a handful of samples pins
    down the whole range. If the samples do not fit a constant block time, every block that
    is asked for is fetched (and cached).
    """

    def __init__(self, rpc: RpcClient, from_block: int, to_block: int, *, samples: int = 5) -> None:
        if to_block < from_block:
            raise ValueError("empty block range")
        self._rpc = rpc
        self._range = (from_block, to_block)
        self._cache: dict[int, int] = {}
        span = to_block - from_block
        points = sorted({from_block + span * i // (samples - 1) for i in range(samples)})
        stamps = {n: self._fetch(n) for n in points}
        self._block_time: int | None = None
        elapsed = stamps[to_block] - stamps[from_block]
        if span > 0 and elapsed % span == 0:
            block_time = elapsed // span
            if all(
                stamps[from_block] + block_time * (n - from_block) == ts for n, ts in stamps.items()
            ):
                self._block_time = block_time

    @property
    def block_time(self) -> int | None:
        """Seconds per block if the whole range has a constant block time, else None."""
        return self._block_time

    def timestamp(self, number: int) -> int:
        start, end = self._range
        if self._block_time is not None and start <= number <= end:
            return self._cache[start] + self._block_time * (number - start)
        return self._fetch(number)

    def _fetch(self, number: int) -> int:
        if number not in self._cache:
            self._cache[number] = self._rpc.block_timestamp(number)
        return self._cache[number]
