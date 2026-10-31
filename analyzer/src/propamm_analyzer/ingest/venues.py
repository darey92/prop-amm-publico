"""Pools for a token pair across a chain's venues.

Factories and registries are asked directly (`getPool`, Curve's MetaRegistry, Fluid's DEX
factory). Uniswap v4 has no per-pool contracts. With HyperSync, the PoolManager's `Initialize`
logs list every pool of the pair, hooked ones included. Without it, the ids of hookless pools are
computed for common fee and tick-spacing pairs. Either way their state is read from the
PoolManager.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import polars as pl

from propamm_analyzer.abi import (
    decode_address,
    encode_address,
    encode_uint,
    event_topic,
    keccak256,
    split_words,
    to_signed,
)
from propamm_analyzer.ingest.hypersync import HyperSyncClient, LogFilter
from propamm_analyzer.ingest.rpc import RpcClient, RpcError
from propamm_analyzer.registry import Venue

NATIVE = "0x0000000000000000000000000000000000000000"
"""Uniswap v4's currency id for the chain's native token (ETH)."""
ETH_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
"""Curve's and Fluid's address for native ETH."""

# Hookless v4 pools probed per pair: the interface's four standard tiers, plus the low fees that
# correlated pairs use.
V4_HOOKLESS_KEYS: tuple[tuple[int, int], ...] = (
    (1, 1),
    (5, 1),
    (10, 1),
    (25, 1),
    (50, 1),
    (100, 1),
    (500, 10),
    (3000, 60),
    (10000, 200),
)
_V4_INITIALIZE = event_topic(
    "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
)
_V4_DYNAMIC_FEE = 0x800000
_V4_POOLS_SLOT = 6  # StateLibrary.POOLS_SLOT
_V4_LIQUIDITY_OFFSET = 3
_Q96 = 2**96
_UINT160 = (1 << 160) - 1
_UINT128 = (1 << 128) - 1
_ONE_BP_SQRT = math.sqrt(1.0001) - 1


@dataclass(frozen=True)
class FoundPool:
    venue: str
    pool: str
    """The pool contract, or the PoolId for Uniswap v4."""
    token0: str
    token1: str
    setting: str
    """What distinguishes the pool within its venue, e.g. `fee 500` or `tick spacing 100`."""
    fee_pips: int
    """0 for Fluid, whose fee is dynamic and not read yet."""
    liquidity: int
    """Active liquidity for concentrated-liquidity pools, sqrt(reserve0 * reserve1) for Solidly
    pools, and 0 where it is not read (Curve, Fluid)."""
    sqrt_price_x96: int
    """0 where it is not read (Curve, Fluid)."""
    stable: bool = False
    """A Solidly stable pool (x³y + xy³ = k), whose depth the formula below does not describe."""
    coins: tuple[int, int] | None = None
    """For Curve: the coin indices of token0 and token1, which can sit among other coins."""

    def depth_per_bp(self, token: str) -> float | None:
        """Raw units of `token` (token0 or token1) that move the price by 1 bp, at the current
        tick only: L·√P·(√1.0001 - 1) in token1, or L/√P·(√1.0001 - 1) in token0."""
        if self.stable or self.sqrt_price_x96 == 0:
            return None
        sqrt_price = self.sqrt_price_x96 / _Q96
        if token == self.token1:
            return self.liquidity * sqrt_price * _ONE_BP_SQRT
        if token == self.token0:
            return self.liquidity / sqrt_price * _ONE_BP_SQRT
        raise ValueError(f"{token} is not in the pool")


def _sorted(token_a: str, token_b: str) -> tuple[str, str]:
    a, b = token_a.lower(), token_b.lower()
    return (a, b) if int(a, 16) < int(b, 16) else (b, a)


def v4_pool_id(currency0: str, currency1: str, fee: int, tick_spacing: int, hooks: str) -> str:
    """`PoolId` = keccak256(abi.encode(PoolKey)). Currencies must be sorted."""
    if int(currency0, 16) >= int(currency1, 16):
        raise ValueError("currency0 must sort before currency1")
    encoded = (
        encode_address(currency0)
        + encode_address(currency1)
        + encode_uint(fee)
        + encode_uint(tick_spacing % (1 << 256))
        + encode_address(hooks)
    )
    return "0x" + keccak256(bytes.fromhex(encoded)).hex()


def _v3_style(rpc: RpcClient, venue: Venue, token0: str, token1: str) -> Iterator[FoundPool]:
    signature = (
        "getPool(address,address,int24)"
        if venue.kind == "slipstream"
        else ("getPool(address,address,uint24)")
    )
    for param in venue.params:
        args = encode_address(token0) + encode_address(token1) + encode_uint(param)
        pool = decode_address(rpc.call_function(venue.address, signature, args)[0])
        if int(pool, 16) == 0:
            continue
        sqrt_price_x96 = rpc.call_function(pool, "slot0()")[0]
        if sqrt_price_x96 == 0:
            continue
        # Slipstream fees depend on the pool, so they are read; v3 fees are the tier itself.
        fee = rpc.call_function(pool, "fee()")[0] if venue.kind == "slipstream" else param
        label = "tick spacing" if venue.kind == "slipstream" else "fee"
        yield FoundPool(
            venue=venue.name,
            pool=pool,
            token0=token0,
            token1=token1,
            setting=f"{label} {param}",
            fee_pips=fee,
            liquidity=rpc.call_function(pool, "liquidity()")[0],
            sqrt_price_x96=sqrt_price_x96,
        )


def _solidly(rpc: RpcClient, venue: Venue, token0: str, token1: str) -> Iterator[FoundPool]:
    for stable in (False, True):
        args = encode_address(token0) + encode_address(token1) + encode_uint(int(stable))
        pool = decode_address(
            rpc.call_function(venue.address, "getPool(address,address,bool)", args)[0]
        )
        if int(pool, 16) == 0:
            continue
        reserve0, reserve1 = rpc.call_function(pool, "getReserves()")[:2]
        if reserve0 == 0 or reserve1 == 0:
            continue
        # Aerodrome fees are in hundredths of a percent: 5 = 0.05 %.
        fee = rpc.call_function(
            venue.address, "getFee(address,bool)", encode_address(pool) + encode_uint(int(stable))
        )[0]
        yield FoundPool(
            venue=venue.name,
            pool=pool,
            token0=token0,
            token1=token1,
            setting="stable" if stable else "volatile",
            fee_pips=fee * 100,
            liquidity=math.isqrt(reserve0 * reserve1),
            sqrt_price_x96=math.isqrt(reserve1 * _Q96 * _Q96 // reserve0),
            stable=stable,
        )


def _topic(address: str) -> str:
    return "0x" + encode_address(address)


def _v4_keys(
    venue: Venue, pairs: Sequence[tuple[str, str]], hypersync: HyperSyncClient | None
) -> Iterator[tuple[str, str, str, int, int, str]]:
    """(pool id, currency0, currency1, fee, tick spacing, hooks) of each candidate pool."""
    if hypersync is None:
        for currency0, currency1 in pairs:
            for fee, tick_spacing in V4_HOOKLESS_KEYS:
                pool_id = v4_pool_id(currency0, currency1, fee, tick_spacing, NATIVE)
                yield pool_id, currency0, currency1, fee, tick_spacing, NATIVE
        return
    topics = (
        (_V4_INITIALIZE,),
        (),
        tuple(_topic(currency0) for currency0, _ in pairs),
        tuple(_topic(currency1) for _, currency1 in pairs),
    )
    scan = hypersync.logs(
        [LogFilter((venue.address,), topics)], venue.start_block, hypersync.height() - 1
    )
    for log in scan.logs:
        _, pool_id, currency0_topic, currency1_topic = log["topics"]
        currency0 = decode_address(int(currency0_topic, 16))
        currency1 = decode_address(int(currency1_topic, 16))
        if (currency0, currency1) not in pairs:  # the filter mixes the pairs' currencies
            continue
        fee, tick_spacing, hooks = split_words(log["data"])[:3]
        yield pool_id, currency0, currency1, fee, to_signed(tick_spacing, 24), decode_address(hooks)


def _v4(
    rpc: RpcClient,
    venue: Venue,
    token0: str,
    token1: str,
    wrapped_native: str | None,
    hypersync: HyperSyncClient | None,
    read_state: bool,
) -> Iterator[FoundPool]:
    pairs = [(token0, token1)]
    if wrapped_native is not None and wrapped_native.lower() in (token0, token1):
        other = token1 if token0 == wrapped_native.lower() else token0
        pairs.append(_sorted(NATIVE, other))
    for pool_id, currency0, currency1, fee, tick_spacing, hooks in _v4_keys(
        venue, pairs, hypersync
    ):
        fee_label = "dynamic fee" if fee == _V4_DYNAMIC_FEE else f"fee {fee}"
        native = " (native)" if currency0 == NATIVE else ""
        hooked = "" if hooks == NATIVE else f", hooks {hooks}"
        setting = f"{fee_label}, tick spacing {tick_spacing}{native}{hooked}"
        if not read_state:
            # Swap events carry their own fee, so a dynamic fee does not need reading.
            static_fee = 0 if fee == _V4_DYNAMIC_FEE else fee
            yield FoundPool(venue.name, pool_id, currency0, currency1, setting, static_fee, 0, 0)
            continue
        state_slot = int.from_bytes(
            keccak256(bytes.fromhex(pool_id[2:] + encode_uint(_V4_POOLS_SLOT)))
        )
        slot0 = rpc.call_function(venue.address, "extsload(bytes32)", encode_uint(state_slot))[0]
        sqrt_price_x96 = slot0 & _UINT160
        if sqrt_price_x96 == 0:
            continue
        liquidity_slot = encode_uint(state_slot + _V4_LIQUIDITY_OFFSET)
        liquidity = rpc.call_function(venue.address, "extsload(bytes32)", liquidity_slot)[0]
        yield FoundPool(
            venue=venue.name,
            pool=pool_id,
            token0=currency0,
            token1=currency1,
            setting=setting,
            fee_pips=(slot0 >> 208) & 0xFFFFFF,
            liquidity=liquidity & _UINT128,
            sqrt_price_x96=sqrt_price_x96,
        )


def _curve_fee(rpc: RpcClient, registry: str, pool_arg: str) -> int:
    """The pool's swap fee in pips. `get_fees()[0]` is in units of 1e-10, so 1e4 per pip."""
    try:
        return rpc.call_function(registry, "get_fees(address)", pool_arg)[0] // 10_000
    except (RpcError, LookupError):
        return 0


def _curve(
    rpc: RpcClient, venue: Venue, token0: str, token1: str, wrapped_native: str | None
) -> Iterator[FoundPool]:
    """Pools that Curve's MetaRegistry lists for the pair. Pools of native ETH count for WETH."""
    queries = [(token0, token1)]
    weth = wrapped_native.lower() if wrapped_native is not None else None
    if weth in (token0, token1):
        queries.append((token1 if token0 == weth else token0, ETH_SENTINEL))
    seen: set[str] = set()
    for coin_a, coin_b in queries:
        args = encode_address(coin_a) + encode_address(coin_b)
        words = rpc.call_function(venue.address, "find_pools_for_coins(address,address)", args)
        count = words[1] if len(words) > 1 else 0  # an address[]: offset, length, items
        for word in words[2 : 2 + count]:
            pool = decode_address(word)
            if pool in seen:
                continue
            seen.add(pool)
            pool_arg = encode_address(pool)
            n_coins = rpc.call_function(venue.address, "get_n_coins(address)", pool_arg)[0]
            coin_words = rpc.call_function(venue.address, "get_coins(address)", pool_arg)
            coins = [decode_address(word) for word in coin_words[:n_coins]]
            indices = [i for i, coin in enumerate(coins) if coin in (coin_a, coin_b)]
            if len(indices) != 2:  # listed for its underlying coins only
                continue
            index0, index1 = indices
            yield FoundPool(
                venue=venue.name,
                pool=pool,
                token0=coins[index0],
                token1=coins[index1],
                setting=f"coins {index0},{index1} of {n_coins}",
                fee_pips=_curve_fee(rpc, venue.address, pool_arg),
                liquidity=0,
                sqrt_price_x96=0,
                coins=(index0, index1),
            )


@functools.cache
def _fluid_dexes(rpc: RpcClient, factory: str) -> tuple[tuple[int, str, str, str], ...]:
    """(dex id, address, token0, token1) of every DEX that the factory has deployed."""
    total = rpc.call_function(factory, "totalDexes()")[0]
    dexes = []
    for dex_id in range(1, total + 1):
        args = encode_uint(dex_id)
        dex = decode_address(rpc.call_function(factory, "getDexAddress(uint256)", args)[0])
        # A static struct: dexId, liquidity, factory, five implementation addresses and the
        # deployer, then token0 and token1.
        constants = rpc.call_function(dex, "constantsView()")
        dexes.append((dex_id, dex, decode_address(constants[9]), decode_address(constants[10])))
    return tuple(dexes)


def _fluid(
    rpc: RpcClient, venue: Venue, token0: str, token1: str, wrapped_native: str | None
) -> Iterator[FoundPool]:
    """Fluid DEXes of the pair. They keep their own token order, which their swap events use."""
    weth = wrapped_native.lower() if wrapped_native is not None else None

    def alias(token: str) -> str:
        return weth if weth is not None and token == ETH_SENTINEL else token

    for dex_id, dex, dex_token0, dex_token1 in _fluid_dexes(rpc, venue.address):
        if _sorted(alias(dex_token0), alias(dex_token1)) != (token0, token1):
            continue
        yield FoundPool(
            venue=venue.name,
            pool=dex,
            token0=dex_token0,
            token1=dex_token1,
            setting=f"dex {dex_id}",
            fee_pips=0,
            liquidity=0,
            sqrt_price_x96=0,
        )


def find_pools(
    rpc: RpcClient,
    venues: Sequence[Venue],
    token_a: str,
    token_b: str,
    *,
    wrapped_native: str | None = None,
    hypersync: HyperSyncClient | None = None,
    read_state: bool = True,
) -> list[FoundPool]:
    """Every initialized pool for the pair. With `wrapped_native` (WETH), pools that hold native
    ETH instead are found too. With `hypersync`, Uniswap v4 pools come from the PoolManager's
    `Initialize` logs, hooked ones included; without it only common hookless pools are tried.

    Without `read_state`, Uniswap v4 pools come without their liquidity and price, which saves
    two RPC calls per pool. On Base, thousands of hooked v4 pools trade WETH/USDC."""
    token0, token1 = _sorted(token_a, token_b)
    found: list[FoundPool] = []
    for venue in venues:
        if venue.kind in ("v3", "slipstream"):
            found += _v3_style(rpc, venue, token0, token1)
        elif venue.kind == "solidly":
            found += _solidly(rpc, venue, token0, token1)
        elif venue.kind == "v4":
            found += _v4(rpc, venue, token0, token1, wrapped_native, hypersync, read_state)
        elif venue.kind == "curve":
            found += _curve(rpc, venue, token0, token1, wrapped_native)
        elif venue.kind == "fluid":
            found += _fluid(rpc, venue, token0, token1, wrapped_native)
        else:
            raise ValueError(f"unknown venue kind {venue.kind!r}")
    return found


POOL_SCHEMA = pl.Schema(
    {
        "pair": pl.String,
        "venue": pl.String,
        "setting": pl.String,
        "fee_bps": pl.Float64,
        "pool": pl.String,
        "price": pl.Float64,
        "depth_per_bp": pl.Float64,
        "token0": pl.String,
        "token1": pl.String,
        "liquidity": pl.String,
    }
)


def pools_frame(
    pools: Sequence[FoundPool],
    pair: str,
    quote: str,
    decimals: dict[str, int],
    *,
    wrapped_native: str | None = None,
) -> pl.DataFrame:
    """One row per pool in token units, oriented as the pair is named.

    `price` is quote per base and `depth_per_bp` is in the quote token. Both are null where the
    pool's state is not read (Curve, Fluid), and the depth for stable pools too. Native ETH counts
    as `wrapped_native`. `liquidity` is a string because it can exceed 64 bits.
    """
    quote = quote.lower()
    rows = []
    for p in pools:
        aliases = {p.token0: p.token0, p.token1: p.token1}
        if wrapped_native is not None:
            for native in (NATIVE, ETH_SENTINEL):
                if native in aliases:
                    aliases[native] = wrapped_native.lower()
        quote_is_token1 = aliases[p.token1] == quote
        quote_token = p.token1 if quote_is_token1 else p.token0
        price = None
        if p.sqrt_price_x96:
            raw = (p.sqrt_price_x96 / _Q96) ** 2 * 10 ** (decimals[p.token0] - decimals[p.token1])
            price = raw if quote_is_token1 else 1 / raw
        depth = p.depth_per_bp(quote_token)
        rows.append(
            {
                "pair": pair,
                "venue": p.venue,
                "setting": p.setting,
                "fee_bps": p.fee_pips / 100,
                "pool": p.pool,
                "price": price,
                "depth_per_bp": None if depth is None else depth / 10 ** decimals[quote_token],
                "token0": p.token0,
                "token1": p.token1,
                "liquidity": str(p.liquidity),
            }
        )
    return pl.DataFrame(rows, schema=POOL_SCHEMA).sort(
        "depth_per_bp", descending=True, nulls_last=True
    )
