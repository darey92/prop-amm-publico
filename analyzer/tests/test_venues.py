import math
from collections.abc import Sequence

import pytest

from propamm_analyzer.abi import encode_address, encode_uint, event_topic, keccak256, selector
from propamm_analyzer.ingest.hypersync import HyperSyncClient, LogFilter, LogScan
from propamm_analyzer.ingest.rpc import Log, RpcClient
from propamm_analyzer.ingest.venues import (
    ETH_SENTINEL,
    NATIVE,
    FoundPool,
    find_pools,
    pools_frame,
    v4_pool_id,
)
from propamm_analyzer.registry import Venue

WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
POOL = "0x00000000000000000000000000000000000000aa"
FACTORY = "0x00000000000000000000000000000000000000f0"
MANAGER = "0x00000000000000000000000000000000000000f4"
SQRT_PRICE = math.isqrt(2_226 * 10**6 * 2**192 // 10**18)  # 2,226 USDC per WETH, as sqrtPriceX96


class FakeRpc(RpcClient):
    """Answers eth_call from a table of (address, calldata) -> words; anything else is zero."""

    def __init__(self, answers: dict[tuple[str, str], list[int]]) -> None:
        super().__init__("http://localhost.invalid")
        self.answers = answers

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        words = self.answers.get((to, data), [0])
        return "0x" + "".join(encode_uint(word) for word in words)


def _call(signature: str, args: str = "") -> str:
    return selector(signature) + args


def test_v4_pool_id_matches_the_live_base_eth_usdc_pool() -> None:
    # Initialized on Base's PoolManager: native ETH / USDC, 0.05 %, tick spacing 10, no hooks.
    expected = "0x96d4b53a38337a5733179751781178a2613306063c511b78cd02684739288c0a"
    assert v4_pool_id(NATIVE, USDC, 500, 10, NATIVE) == expected
    with pytest.raises(ValueError):
        v4_pool_id(USDC, NATIVE, 500, 10, NATIVE)


def test_find_pools_asks_each_kind_of_venue() -> None:
    pair = encode_address(WETH) + encode_address(USDC)
    pool_id = v4_pool_id(NATIVE, USDC, 500, 10, NATIVE)
    slot = int.from_bytes(keccak256(bytes.fromhex(pool_id[2:] + encode_uint(6))))
    lp_fee = 500
    answers = {
        (FACTORY, _call("getPool(address,address,uint24)", pair + encode_uint(500))): [
            int(POOL, 16)
        ],
        (POOL, _call("slot0()")): [SQRT_PRICE, 0, 0],
        (POOL, _call("liquidity()")): [10**18],
        (FACTORY, _call("getPool(address,address,bool)", pair + encode_uint(0))): [int(POOL, 16)],
        (POOL, _call("getReserves()")): [10**18, 2_226 * 10**6, 0],
        (FACTORY, _call("getFee(address,bool)", encode_address(POOL) + encode_uint(0))): [30],
        (MANAGER, _call("extsload(bytes32)", encode_uint(slot))): [SQRT_PRICE | lp_fee << 208],
        (MANAGER, _call("extsload(bytes32)", encode_uint(slot + 3))): [7 * 10**17],
    }
    venues = [
        Venue("v3-like", "v3", FACTORY, (100, 500)),
        Venue("solidly-like", "solidly", FACTORY),
        Venue("v4-like", "v4", MANAGER),
    ]
    found = find_pools(FakeRpc(answers), venues, USDC, WETH, wrapped_native=WETH)
    assert [(p.venue, p.setting, p.fee_pips) for p in found] == [
        ("v3-like", "fee 500", 500),
        ("solidly-like", "volatile", 3_000),
        ("v4-like", "fee 500, tick spacing 10 (native)", 500),
    ]
    assert found[0].token0 == WETH  # sorted by address
    assert found[1].liquidity == math.isqrt(10**18 * 2_226 * 10**6)
    assert (found[2].token0, found[2].liquidity) == (NATIVE, 7 * 10**17)


def test_depth_per_bp_matches_the_exact_curve() -> None:
    pool = FoundPool("v3", POOL, WETH, USDC, "fee 500", 500, 10**18, SQRT_PRICE)
    sqrt_price = SQRT_PRICE / 2**96
    # Exact amounts for a 1 bp move: token1 in to raise the price, token0 in to lower it.
    token1_in = 10**18 * sqrt_price * (math.sqrt(1.0001) - 1)
    token0_in = 10**18 * (1 / (sqrt_price / math.sqrt(1.0001)) - 1 / sqrt_price)
    assert pool.depth_per_bp(USDC) == pytest.approx(token1_in)
    assert pool.depth_per_bp(WETH) == pytest.approx(token0_in, rel=1e-4)
    stable = FoundPool("solidly", POOL, WETH, USDC, "stable", 500, 1, 1, stable=True)
    assert stable.depth_per_bp(USDC) is None


def test_frame_orients_prices_and_depth_by_the_quote_token() -> None:
    # A v4 pool with native ETH as currency0, read as USDC/WETH: the price is inverted.
    pool = FoundPool("v4", "0x01", NATIVE, USDC, "fee 500", 500, 10**18, SQRT_PRICE)
    decimals = {NATIVE: 18, WETH: 18, USDC: 6}
    eth_usdc = pools_frame([pool], "WETH/USDC", USDC, decimals, wrapped_native=WETH).row(
        0, named=True
    )
    usdc_eth = pools_frame([pool], "USDC/WETH", WETH, decimals, wrapped_native=WETH).row(
        0, named=True
    )
    assert eth_usdc["price"] == pytest.approx(2_226, rel=1e-3)
    assert usdc_eth["price"] == pytest.approx(1 / eth_usdc["price"])
    assert eth_usdc["depth_per_bp"] == pytest.approx(pool.depth_per_bp(USDC) / 10**6)  # type: ignore[operator]
    assert usdc_eth["depth_per_bp"] == pytest.approx(pool.depth_per_bp(NATIVE) / 10**18)  # type: ignore[operator]


# Ethereum mainnet addresses, for the venues that only exist there.
L1_WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
STETH = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
WSTETH = "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0"
REGISTRY = "0x00000000000000000000000000000000000000c1"
CURVE_POOL = "0x00000000000000000000000000000000000000c0"
OTHER = "0x00000000000000000000000000000000000000c2"
FLUID_FACTORY = "0x00000000000000000000000000000000000000f1"
DEX_1 = "0x00000000000000000000000000000000000000d1"
DEX_2 = "0x00000000000000000000000000000000000000d2"


def test_curve_pools_of_native_eth_count_for_weth() -> None:
    pool_arg = encode_address(CURVE_POOL)
    native_query = encode_address(STETH) + encode_address(ETH_SENTINEL)
    answers = {
        # Only the query with Curve's address for native ETH finds the pool.
        (REGISTRY, _call("find_pools_for_coins(address,address)", native_query)): [
            0x20,
            1,
            int(CURVE_POOL, 16),
        ],
        (REGISTRY, _call("get_n_coins(address)", pool_arg)): [3],
        (REGISTRY, _call("get_coins(address)", pool_arg)): [
            int(OTHER, 16),
            int(ETH_SENTINEL, 16),
            int(STETH, 16),
            *[0] * 5,
        ],
        (REGISTRY, _call("get_fees(address)", pool_arg)): [4_000_000, 5_000_000_000],
    }
    venues = [Venue("curve", "curve", REGISTRY)]
    found = find_pools(FakeRpc(answers), venues, STETH, L1_WETH, wrapped_native=L1_WETH)
    assert [(p.pool, p.token0, p.token1, p.coins, p.fee_pips, p.setting) for p in found] == [
        (CURVE_POOL, ETH_SENTINEL, STETH, (1, 2), 400, "coins 1,2 of 3")
    ]
    assert found[0].depth_per_bp(STETH) is None  # Curve's state is not read


def _constants(token0: str, token1: str) -> list[int]:
    return [*[0] * 9, int(token0, 16), int(token1, 16), *[0] * 5]


def test_fluid_dexes_keep_their_own_token_order() -> None:
    answers = {
        (FLUID_FACTORY, _call("totalDexes()")): [2],
        (FLUID_FACTORY, _call("getDexAddress(uint256)", encode_uint(1))): [int(DEX_1, 16)],
        (FLUID_FACTORY, _call("getDexAddress(uint256)", encode_uint(2))): [int(DEX_2, 16)],
        (DEX_1, _call("constantsView()")): _constants(WSTETH, ETH_SENTINEL),
        (DEX_2, _call("constantsView()")): _constants(ETH_SENTINEL, USDC),
    }
    venues = [Venue("fluid", "fluid", FLUID_FACTORY)]
    found = find_pools(FakeRpc(answers), venues, WSTETH, L1_WETH, wrapped_native=L1_WETH)
    assert [(p.pool, p.token0, p.token1, p.setting) for p in found] == [
        (DEX_1, WSTETH, ETH_SENTINEL, "dex 1")
    ]


class FakeHyperSync(HyperSyncClient):
    """Serves fixed logs and records what it was asked for."""

    def __init__(self, logs: list[Log]) -> None:
        super().__init__("https://hypersync.invalid", "token")
        self.served = LogScan(logs=logs)
        self.asked: list[tuple[list[LogFilter], int, int]] = []

    def height(self) -> int:
        return 1_000

    def logs(self, filters: Sequence[LogFilter], from_block: int, to_block: int) -> LogScan:
        self.asked.append((list(filters), from_block, to_block))
        return self.served


def _initialize(pool_id: str, currency0: str, currency1: str, fee: int, hooks: str) -> Log:
    topic = event_topic("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
    words = encode_uint(fee) + encode_uint(1) + encode_address(hooks) + encode_uint(SQRT_PRICE)
    return {
        "address": MANAGER,
        "topics": [
            topic,
            pool_id,
            "0x" + encode_address(currency0),
            "0x" + encode_address(currency1),
        ],
        "data": "0x" + words + encode_uint(0),
    }


def test_v4_pools_come_from_initialize_logs_when_hypersync_is_given() -> None:
    hooks = "0x00000000000000000000000000000000000088c0"
    hooked = v4_pool_id(NATIVE, USDC, 0x800000, 1, hooks)
    # The filter crosses the currencies of the pair's variants, so stray pools can come back.
    stray = v4_pool_id(NATIVE, WETH, 100, 1, NATIVE)
    client = FakeHyperSync(
        [
            _initialize(hooked, NATIVE, USDC, 0x800000, hooks),
            _initialize(stray, NATIVE, WETH, 100, NATIVE),
        ]
    )
    slot = int.from_bytes(keccak256(bytes.fromhex(hooked[2:] + encode_uint(6))))
    answers = {
        (MANAGER, _call("extsload(bytes32)", encode_uint(slot))): [SQRT_PRICE | 3_000 << 208],
        (MANAGER, _call("extsload(bytes32)", encode_uint(slot + 3))): [10**18],
    }
    venues = [Venue("v4", "v4", MANAGER, start_block=7)]
    found = find_pools(FakeRpc(answers), venues, USDC, WETH, wrapped_native=WETH, hypersync=client)
    assert [(p.pool, p.setting, p.fee_pips) for p in found] == [
        (hooked, f"dynamic fee, tick spacing 1 (native), hooks {hooks}", 3_000)
    ]
    ((filters, start, end),) = client.asked
    assert (start, end) == (7, 999)
    assert filters[0].topics[2] == ("0x" + encode_address(WETH), "0x" + encode_address(NATIVE))
