"""Contract addresses and feed ids the analyzer relies on. Each is checked before use."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Venue:
    name: str
    kind: str
    """How pools are found: `v3` (factory `getPool` by fee), `slipstream` (by tick spacing),
    `solidly` (by stable flag), `v4` (pool ids, read from the PoolManager), `curve` (Curve's
    MetaRegistry) or `fluid` (Fluid's DEX factory)."""
    address: str
    """The factory, the PoolManager for `v4`, or the MetaRegistry for `curve`."""
    params: tuple[int, ...] = ()
    """Fee tiers in pips for `v3`, tick spacings for `slipstream`."""
    swap_event: str = ""
    """Swap event layout when it is not the kind's own: PancakeSwap v3 adds two protocol-fee
    fields to the Uniswap v3 event, so its pools need `pancake-v3`."""
    start_block: int = 0
    """For `v4` with HyperSync: where the scan of `Initialize` logs starts, at or before the
    PoolManager's deployment."""


@dataclass(frozen=True)
class Chain:
    name: str
    chain_id: int
    public_rpc: str
    archive_rpc: str
    """Keyless endpoint that also serves historical state, for fork tests at pinned blocks."""
    uniswap_v3_factory: str
    pyth: str
    tokens: dict[str, str] = field(default_factory=dict)
    venues: tuple[Venue, ...] = ()
    wrapped_native: str = "WETH"
    """Symbol of the wrapped native token, which pools of the native token count as."""


BASE = Chain(
    name="base",
    chain_id=8453,
    # Free, keyless endpoint. Base's own mainnet.base.org rejects eth_getLogs ("request limit
    # reached"); https://base.drpc.org also works as a fallback.
    public_rpc="https://base-rpc.publicnode.com",
    # PublicNode wants a personal token for historical state; Base's own endpoint serves it.
    archive_rpc="https://mainnet.base.org",
    uniswap_v3_factory="0x33128a8fc17869897dce68ed026d694621f6fdfd",
    pyth="0x8250f4af4b972684f7b336503e2d6dfedeb1487a",
    # Symbols and decimals checked on-chain on 2026-10-30.
    tokens={
        "WETH": "0x4200000000000000000000000000000000000006",
        "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "wstETH": "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452",
        "cbETH": "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22",
        "weETH": "0x04c0599ae5a44757c0af6f9ec3b93da8976c150a",
        "wrsETH": "0xedfa23602d0ec14714057867a78d01e94176bea0",
        "ezETH": "0x2416092f143378750bb29b79ed961ab195cceea5",
        "EURC": "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42",
        "cbBTC": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
        "AERO": "0x940181a94a35a4569e4529a3cdfb74e38fd98631",
        "VIRTUAL": "0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b",
    },
    venues=(
        Venue(
            "uniswap-v3",
            "v3",
            "0x33128a8fc17869897dce68ed026d694621f6fdfd",
            (100, 500, 3000, 10000),
        ),
        # The PoolManager has code from block 25,350,988 (2025-01-21).
        Venue(
            "uniswap-v4", "v4", "0x498581ff718922c3f8e6a244956af099b2652b2b", start_block=25_350_000
        ),
        Venue(
            "aerodrome-slipstream",
            "slipstream",
            "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a",
            (1, 50, 100, 200, 2000),
        ),
        Venue("aerodrome-v2", "solidly", "0x420dd381b31aef6683db6b902084cb0ffece40da"),
        Venue(
            "pancakeswap-v3",
            "v3",
            "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865",
            (100, 500, 2500, 10000),
            swap_event="pancake-v3",
        ),
        Venue(
            "sushiswap-v3",
            "v3",
            "0xc35dadb65012ec5796536bd9864ed8773abc74c4",
            (100, 500, 3000, 10000),
        ),
    ),
)

ETHEREUM = Chain(
    name="ethereum",
    chain_id=1,
    # Free and keyless. PublicNode answers 403 without a token, and 1rpc.io keeps no history.
    public_rpc="https://rpc.mevblocker.io",
    archive_rpc="https://eth.drpc.org",
    uniswap_v3_factory="0x1f98431c8ad98523631ae4a59f267346ea31f984",
    pyth="0x4305fb66699c3b2702d4d05cf36551390a4c69c6",
    # Checked on-chain on 2026-10-30; all have 18 decimals.
    tokens={
        "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "wstETH": "0x7f39c581f595b53c5cb19bd0b3f8da6c935e2ca0",
        "stETH": "0xae7ab96520de3a18e5e111b5eaab095312d7fe84",
        "cbETH": "0xbe9895146f7af43049ca1c1ae358b0541ea49704",
        "weETH": "0xcd5fe23c85820f7b72d0926fc9b05b43e359b7ee",
        "rETH": "0xae78736cd615f374d3085123a210448e74fc6393",
    },
    # Balancer v2 and v3 were checked too: under 1 ETH of LST/WETH swaps in the week to
    # 2026-10-30, so they are left out.
    venues=(
        Venue(
            "uniswap-v3",
            "v3",
            "0x1f98431c8ad98523631ae4a59f267346ea31f984",
            (100, 500, 3000, 10000),
        ),
        # Deployed in January 2025, after this block.
        Venue(
            "uniswap-v4", "v4", "0x000000000004444c5dc75cb358380d2e3de08a90", start_block=21_600_000
        ),
        Venue("curve", "curve", "0xf98b45fa17de75fb1ad0e7afd971b0ca00e379fc"),
        Venue("fluid", "fluid", "0x91716c4eda1fb55e84bf8b4c7085f84285c19085"),
        Venue(
            "pancakeswap-v3",
            "v3",
            "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865",
            (100, 500, 2500, 10000),
            swap_event="pancake-v3",
        ),
        Venue(
            "sushiswap-v3",
            "v3",
            "0xbaceb8ec6b9355dfc0269c18bac9d6e2bdc29c4f",
            (100, 500, 3000, 10000),
        ),
    ),
)

MONAD = Chain(
    name="monad",
    chain_id=143,
    # The Monad Foundation's free, keyless endpoint.
    public_rpc="https://rpc.monad.xyz",
    archive_rpc="https://rpc.monad.xyz",
    # Not used: Uniswap v3's MON/USDC pools carry almost nothing (see the venues below).
    uniswap_v3_factory="",
    # From Pyth's list of EVM deployments; it has code (2026-10-31).
    pyth="0x2880ab155794e7179c9ee2e38200202908c17b43",
    # Symbols and decimals checked on-chain on 2026-10-31.
    tokens={
        "WMON": "0x3bd359c1119da7da1d913d1c4d2b7c461115433a",
        "USDC": "0x754704bc059f8c67012fed69bc8a327a5aafb603",
    },
    # Uniswap v4's native-MON/USDC 0.05 % pool held 99.9 % of the passive MON/USDC volume in
    # the day to 2026-10-31. The other pools (Uniswap v2 and v3 forks, PancakeSwap v3, LFJ
    # Liquidity Book) traded under $1k a day between them, so they are left out.
    venues=(
        # The PoolManager's first log is in block 29,255,895.
        Venue(
            "uniswap-v4", "v4", "0x188d586ddcf52439676ca21a244753fa19f9ea8e", start_block=29_250_000
        ),
    ),
    wrapped_native="WMON",
)

CHAINS = {chain.name: chain for chain in (BASE, ETHEREUM, MONAD)}

# Contracts that a user's transaction calls when a router, an aggregator or an intent protocol
# fills it. A swap whose transaction calls one of them counts as routed flow, the flow a new pool
# can win by quoting better. Bots and solvers calling their own contracts do not. Only addresses
# we are confident about are listed; everything else is reported unlabelled.
ROUTERS: dict[str, dict[str, str]] = {
    "base": {
        "0x2626664c2603336e57b271c5c0b26f421741e481": "Uniswap SwapRouter02",
        "0x6ff5693b99212da76ad316178a184ab56d299b43": "Uniswap UniversalRouter",
        "0x111111125421ca6dc452d289314280a0f8842a65": "1inch v6",
        "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap",
        "0x19ceead7105607cd444f5ad10dd51356436095a1": "Odos v2",
        "0x6a000f20005980200259b80c5102003040001068": "Velora (ParaSwap) v6",
        "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW Protocol",
        "0xbe6d8f0d05cc4be24d5167a3ef062215be6d18a5": "Slipstream SwapRouter",
        "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43": "Aerodrome Router",
    },
    "ethereum": {
        "0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "Uniswap UniversalRouter (v4)",
        "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap UniversalRouter",
        "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b": "Uniswap UniversalRouter (2022)",
        "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap SwapRouter02",
        "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap SwapRouter",
        "0x00000011f84b9aa48e5f8aa8b9897600006289be": "UniswapX V2 reactor",
        "0x6000da47483062a0d734ba3dc7576ce6a0b645c4": "UniswapX reactor",
        "0x111111125421ca6dc452d289314280a0f8842a65": "1inch v6",
        "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch v5",
        "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x Exchange Proxy",
        "0x0000000000001ff3684f28c67538d4d072c22734": "0x AllowanceHolder",
        "0x9008d19f58aabd9ed0d60971565aa8510560ab41": "CoW Protocol",
        "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap",
        "0xcf5540fffcdc3d510b18bfca6d2b9987b0772559": "Odos v2",
        "0x6a000f20005980200259b80c5102003040001068": "Velora (ParaSwap) v6",
        "0xdef171fe48cf0115b1d80b88dc8eab59176fee57": "ParaSwap v5",
        "0x881d40237659c251811cec9c364ef91dc08d300c": "MetaMask Swaps",
        "0x1231deb6f5749ef6ce6943a275a1d3e7486f4eae": "LI.FI",
        # Named by their verified source on Blockscout (2026-10-30). The UniversalRouter's
        # immutables point at Uniswap's PoolManager and position managers.
        "0x23617e59a5925b2a4bf75d73ff6711cd0b29de85": "Uniswap UniversalRouter (2026)",
        "0x666fedd4cdd4e890a5ad20e7b60975409435a64a": "0x Settler",
        "0x0cef320d0c5793ade37964e2661a033771e53cd5": "0x Settler",
        "0xe6e49e792dc5727a1dd3450d9834c7058c6d72ce": "0x Settler (intents)",
        "0x45312ea0eff7e09c83cbe249fa1d7598c4c8cd4e": "Curve Router v1.2",
    },
    "monad": {
        # Uniswap's from its deployment list, the others by their verified source on Sourcify
        # (2026-10-31).
        "0x0d97dc33264bfc1c226207428a79b26757fb9dc3": "Uniswap UniversalRouter",
        "0xfdf682f51fe81aa4898f0ae2163d8a55c127fbc7": "Uniswap UniversalRouter 2.1.1",
        "0xa6ce4f10d83dbddac17e68e1837ca9ce6a1b596e": "Uniswap UniversalRouter 2.1.2",
        "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap",
        "0x0729e2a4716d9f7dd17858d5d504426d228697f7": "Relay RelayRouter",
        "0x5567954cd026cebfe486062cb75848e0f1c12e58": "Relay ApprovalProxy",
        # Unverified, but its users call 0x Settler's `execute` (0x1fff991f), from 535 accounts.
        "0xfb78fcae443eb423b59b8c186518c5df94416344": "0x Settler",
    },
}

# Routers that reach only their own venue's pools (Uniswap's v2 and v3 routers, Aerodrome's and
# Curve's), so they cannot send an order to a new Uniswap v4 hook. Their flow counts as routed,
# but a new pool cannot win it.
VENUE_ROUTERS: dict[str, frozenset[str]] = {
    "base": frozenset(
        {
            "0x2626664c2603336e57b271c5c0b26f421741e481",
            "0xbe6d8f0d05cc4be24d5167a3ef062215be6d18a5",
            "0xcf77a3ba9a5ca399b7c97c74d54e5b1beb874e43",
        }
    ),
    "ethereum": frozenset(
        {
            "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",
            "0xef1c6e67703c7bd7107eed8303fbe6ec2554bf6b",
            "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
            "0xe592427a0aece92de3edee1f18e0157c05861564",
            "0x45312ea0eff7e09c83cbe249fa1d7598c4c8cd4e",
        }
    ),
    "monad": frozenset(),
}

# Chainlink feeds on Base for the pilot pairs, from Chainlink's reference data directory
# (2026-10-30). Exchange-rate feeds follow each token's staking contract and update about once a
# day; the market feed moves on 0.5 % deviations and serves as a safety band.
CHAINLINK_BASE: dict[str, str] = {
    "wstETH-ETH Exchange Rate": "0xa669e5272e60f78299f4824495ce01a3923f4380",
    "cbETH-ETH Exchange Rate": "0x868a501e68f3d1e89cfc0d22f6b22e8dabce5f04",
    "weETH / eETH Exchange Rate": "0x35e9d7001819ea3b39da906ae6b06a62cfe2c181",
    "WSTETH / ETH": "0x43a5c292a453a3bf3606fa856197f09d7b74251a",
}

# Pairs the analyzer screens first: staking tokens, a euro stablecoin and
# mid caps against their natural quote, with ETH/USDC as the reference market.
CANDIDATE_PAIRS: tuple[tuple[str, str], ...] = (
    ("wstETH", "WETH"),
    ("weETH", "WETH"),
    ("wrsETH", "WETH"),
    ("cbETH", "WETH"),
    ("ezETH", "WETH"),
    ("EURC", "USDC"),
    ("cbBTC", "USDC"),
    ("cbBTC", "WETH"),
    ("AERO", "USDC"),
    ("AERO", "WETH"),
    ("VIRTUAL", "WETH"),
    ("WETH", "USDC"),
)

# Pyth Core feed ids for the candidate pairs, from Hermes' /v2/price_feeds (2026-10-30). The ids
# are Pythnet's, so they are the same on every chain. `.RR` feeds are redemption rates: they track
# a staking token's exchange rate in its own contracts, not a market price.
PYTH_FEEDS: dict[str, str] = {
    "Crypto.ETH/USD": "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "Crypto.ETH/USDC": "0xf2f8317e3bd7d4afb5a613030cdcae7e4196c3cf46d440317b0c0fa18316e749",
    "Crypto.USDC/USD": "0xeaa020c61cc479712813461ce153894a96a6c00b21ed0cfc2798d1f9a9e9c94a",
    "Crypto.WSTETH/USD": "0x6df640f3b8963d8f8358f791f352b8364513f6ab1cca5ed3f1f7b5448980e784",
    "Crypto.WSTETH/STETH.RR": "0xf59ead01ed0faba85332a1e2feae8ddb14a1c94ebac259f1c982c92fc7ce333e",
    "Crypto.STETH/ETH": "0x3af6a3098c56f58ff47cc46dee4a5b1910e5c157f7f0b665952445867470d61f",
    "Crypto.STETH/ETH.RR": "0xcf40822c7635ddbde64c831982c65b3b37247f139e8e53078d1602e886badd2f",
    "Crypto.WEETH/USD": "0x9ee4e7c60b940440a261eb54b6d8149c23b580ed7da3139f7f08f4ea29dad395",
    "Crypto.WEETH/EETH.RR": "0x343558e79f587e098c321218ecb34d031ba709ab3e84133126f3c98511b91f64",
    "Crypto.CBETH/USD": "0x15ecddd26d49e1a8f1de9376ebebc03916ede873447c1255d2d5891b92ce5717",
    "Crypto.CBETH/ETH.RR": "0xed5d938ea5c648f66a3b7dd349376356c1d95135307137f4d1200eb36e9f5c12",
    "Crypto.RETH/ETH.RR": "0x612a00bae88eee6a4bc8bd4de6e578794a0e584ce784691561aba348e07cbede",
    "Crypto.RSETH/USD": "0x0caec284d34d836ca325cf7b3256c078c597bc052fbd3c0283d52b581d68d71f",
    "Crypto.EURC/USD": "0x76fa85158bf14ede77087fe3ae472f66213f6ea2f5b411cb2de472794990fa5c",
    "FX.EUR/USD": "0xa995d00bb36a63cef7fd2c287dc105fc8f3d93779f062f09551b0af3e81ec30b",
    "Metal.XAU/USD": "0x765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2",
    "Crypto.PAXG/USD": "0x273717b49430906f4b0c230e99aa1007f83758e3199edbc887c0d06c3e332494",
    "Crypto.XAUT/USD": "0x44465e17d2e9d390e70c999d5a11fda4f092847fcd2e3e5aa089d96c98a30e67",
    "Crypto.CBBTC/USD": "0x2817d7bfe5c64b8ea956e9a26f573ef64e72e4d7891f2d6af9bcc93f7aff9a97",
    "Crypto.AERO/USD": "0x9db37f4d5654aad3e37e2e14ffd8d53265fb3026d1d8f91146539eebaa2ef45f",
    "Crypto.VIRTUAL/USD": "0x8132e3eb1dac3e56939a16ff83848d194345f6688bff97eb1c8bd462d558802b",
    "Crypto.MON/USD": "0x31491744e2dbf6df7fcf4ac0820d18a609b49076d45066d3568424e62f686cd1",
}
