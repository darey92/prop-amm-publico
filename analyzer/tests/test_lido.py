from collections.abc import Sequence

from propamm_analyzer.abi import encode_uint
from propamm_analyzer.ingest.hypersync import HyperSyncClient, LogFilter, LogScan
from propamm_analyzer.ingest.lido import TOKEN_REBASED, rate_history, report_rate
from propamm_analyzer.ingest.rpc import Log

STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
RATE = 1_247_995_805_450_453_143  # stEthPerToken() after the report at block 26,305,541


def _rebase_log(block: int, post_shares: int, post_ether: int) -> Log:
    # timeElapsed, pre-report shares and ether, post-report shares and ether, fee shares
    words = [86_400, post_shares // 2, post_ether // 2, post_shares, post_ether, 10**15]
    return {
        "address": STETH,
        "blockNumber": hex(block),
        "topics": [TOKEN_REBASED, "0x" + encode_uint(1_793_362_943)],
        "data": "0x" + "".join(encode_uint(w) for w in words),
    }


def test_the_rate_is_ether_per_share_rounded_down() -> None:
    shares = 10**27
    assert report_rate(_rebase_log(1, shares, RATE * 10**9)) == RATE
    assert report_rate(_rebase_log(1, 3, 4)) == 10**18 * 4 // 3


class FakeHyperSync(HyperSyncClient):
    def __init__(self, scan: LogScan) -> None:
        super().__init__("https://hypersync.invalid", "token")
        self.scan = scan

    def logs(self, filters: Sequence[LogFilter], from_block: int, to_block: int) -> LogScan:
        assert list(filters) == [LogFilter((STETH,), ((TOKEN_REBASED,),))]
        return self.scan


def test_history_has_one_row_per_report() -> None:
    scan = LogScan(
        logs=[_rebase_log(26_305_541, 10**27, RATE * 10**9)], timestamps={26_305_541: 1_793_362_943}
    )
    history = rate_history(FakeHyperSync(scan), STETH, 26_300_000, 26_310_000)
    assert history.rows() == [(26_305_541, 1_793_362_943_000, RATE / 10**18)]
