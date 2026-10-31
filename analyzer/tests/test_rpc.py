from collections.abc import Sequence
from itertools import pairwise

import pytest

from propamm_analyzer.ingest.rpc import BlockClock, Log, RpcClient, RpcError

UNUSED_URL = "http://localhost.invalid"


class RangeLimitedRpc(RpcClient):
    """Fake node that rejects eth_getLogs ranges wider than `max_range` blocks."""

    def __init__(self, max_range: int) -> None:
        super().__init__(UNUSED_URL)
        self.max_range = max_range
        self.served: list[tuple[int, int]] = []

    def get_logs(
        self, address: str, topics: Sequence[str | None], from_block: int, to_block: int
    ) -> list[Log]:
        if to_block - from_block + 1 > self.max_range:
            raise RpcError(-32005, "block range is too wide")
        self.served.append((from_block, to_block))
        return [{"blockNumber": hex(from_block)}]


class RevertingRpc(RpcClient):
    def get_logs(
        self, address: str, topics: Sequence[str | None], from_block: int, to_block: int
    ) -> list[Log]:
        raise RpcError(-32000, "execution reverted")


class ClockRpc(RpcClient):
    """Fake node with 2-second blocks, optionally overridden per block."""

    def __init__(self, overrides: dict[int, int] | None = None) -> None:
        super().__init__(UNUSED_URL)
        self.overrides = overrides or {}
        self.calls = 0

    def block_timestamp(self, number: int) -> int:
        self.calls += 1
        return self.overrides.get(number, 1_700_000_000 + 2 * number)


def test_iter_logs_halves_the_range_and_covers_every_block() -> None:
    rpc = RangeLimitedRpc(max_range=300)
    list(rpc.iter_logs("0x0", [], 0, 999, chunk=1_000))
    assert rpc.served[0][0] == 0
    assert rpc.served[-1][1] == 999
    for (_, end), (start, _) in pairwise(rpc.served):
        assert start == end + 1


def test_iter_logs_reraises_other_errors() -> None:
    with pytest.raises(RpcError):
        list(RevertingRpc(UNUSED_URL).iter_logs("0x0", [], 0, 10))


def test_first_block_at_or_after_needs_few_calls_on_a_steady_chain() -> None:
    rpc = ClockRpc()
    target = 1_700_000_000 + 2 * 53_000_000 + 1  # between blocks 53,000,000 and 53,000,001
    assert rpc.first_block_at_or_after(target, hi=60_000_000) == 53_000_001
    assert rpc.calls <= 6


def test_first_block_at_or_after_never_asks_for_old_blocks_when_the_target_is_recent() -> None:
    rpc = ClockRpc()
    asked: list[int] = []
    original = rpc.block_timestamp

    def recording(number: int) -> int:
        asked.append(number)
        return original(number)

    rpc.block_timestamp = recording  # type: ignore[method-assign]
    target = 1_700_000_000 + 2 * 59_990_000  # 10,000 blocks before the head
    assert rpc.first_block_at_or_after(target, hi=60_000_000) == 59_990_000
    assert min(asked) > 59_900_000


def test_first_block_at_or_after_handles_uneven_block_times() -> None:
    late = {n: 1_700_000_000 + 2 * n + 1 for n in range(1_501, 2_001)}
    rpc = ClockRpc(late)
    # Block 1,500 is at +3,000 s and block 1,501 at +3,003 s.
    assert rpc.first_block_at_or_after(1_700_000_000 + 3_001, lo=1_000, hi=2_000) == 1_501
    assert rpc.first_block_at_or_after(1_700_000_000, lo=0, hi=2_000) == 0


def test_block_clock_interpolates_constant_block_times() -> None:
    rpc = ClockRpc()
    clock = BlockClock(rpc, 1_000, 2_000)
    calls_after_sampling = rpc.calls
    assert clock.block_time == 2
    assert clock.timestamp(1_234) == 1_700_000_000 + 2 * 1_234
    assert rpc.calls == calls_after_sampling


def test_block_clock_fetches_blocks_when_block_times_vary() -> None:
    late = {n: 1_700_000_000 + 2 * n + 1 for n in range(1_501, 2_001)}
    clock = BlockClock(ClockRpc(late), 1_000, 2_000)
    assert clock.block_time is None
    assert clock.timestamp(1_600) == late[1_600]
