# propamm-analyzer

This is Phase 0 of Prop-AMM Público. It measures how much passive LPs lose (markouts and LVR), and then backtests an oracle-anchored quoting pool against real flow. The pool design is in [`../docs/spec.md`](../docs/spec.md). Phase 0 ended in a no-go; [`../docs/phase-0-results.md`](../docs/phase-0-results.md) gives the results.

## Commands

All data goes into `data/`, which git ignores. Raw downloads go in `data/raw/` and derived parquet files in `data/processed/`.

```bash
# 1-second Binance klines (from data.binance.vision, SHA-256 verified), for a day or a range
uv run propamm binance-klines ETHUSDC 2026-10-28
uv run propamm binance-klines ETHUSDC 2026-10-23 --to 2026-10-29

# Find a Uniswap v3-style pool and print its tokens and fee
uv run propamm find-v3-pool --token-a 0x4200000000000000000000000000000000000006 \
    --token-b 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913 --fee 500

# A pool's swaps in a UTC window
uv run propamm v3-swaps --pool <address> --start 2026-10-28T12:00 --end 2026-10-28T13:00

# Volume-weighted LP markouts at several horizons, with and without fees
uv run propamm markouts --swaps data/processed/swaps/<file>.parquet \
    --prices data/processed/prices/ETHUSDC-1s-2026-10-28.parquet

# Every pool of a pair (or of all candidate pairs) across Base venues, ranked by depth
uv run propamm venues wstETH WETH
uv run propamm venues --candidates --out data/processed/registry/base-pools.parquet

# Every swap of a pair across all Base venues, with per-venue volume (pilot pairs: cbETH,
# wstETH and weETH against WETH)
uv run propamm pair-swaps cbETH WETH --days 7 --out data/processed/swaps/base-cbETH-WETH-7d.parquet

# Every swap of a pair across a chain's venues in one HyperSync scan (token in .env), with
# each transaction's sender and the contract it called
uv run --env-file .env propamm hs-swaps wstETH WETH --chain ethereum \
    --start 2026-10-02T00:00 --end 2026-10-30T00:00 \
    --out data/processed/swaps/ethereum-wstETH-WETH-28d.parquet

# Daily volume of those files, and the part that routers and aggregators send
uv run propamm flow data/processed/swaps/ethereum-*-28d.parquet --usd-per-eth 2239

# wstETH/WETH LP markouts against the market mid and Lido's rate, split by venue, by routed or
# unrouted flow, by calm or burst days and by trade size
uv run --env-file .env propamm lst-markouts data/processed/swaps/ethereum-wstETH-WETH-28d.parquet

# Backtest the rate-anchored pool on that flow. A comma-separated list in any pool option runs a
# grid, one row per configuration; --start and --end split it into training and test windows
uv run --env-file .env propamm simulate data/processed/swaps/ethereum-wstETH-WETH-28d.parquet \
    --s-min 0.6 --offset 1.5 --k-inv 100 --cap 30 --routing split
uv run --env-file .env propamm simulate data/processed/swaps/ethereum-wstETH-WETH-28d.parquet \
    --s-min 0.4,0.6,0.9 --k-inv 0,100,500 --cap 10,30 --routing split --end 2026-10-16T00:00

# A busy pair in chunks of 6 hours of Base blocks, then the fast-oracle pool on it, with
# Binance's 1 s closes standing in for Pyth
uv run --env-file .env propamm hs-swaps WETH USDC --chain base --start 2026-10-23T00:00 \
    --end 2026-10-30T00:00 --chunk-blocks 10800 --out data/processed/swaps/base-WETH-USDC-7d.parquet
uv run propamm simulate-oracle data/processed/swaps/base-WETH-USDC-7d.parquet \
    --prices data/processed/prices/ETHUSDC-1s-2026-10-23_2026-10-29.parquet --s-min 1,2,3

# Monad: MON/USDC swaps, every block's time in ms, and Bybit's MON/USDC microprice (MONUSDT's
# book over USDCUSDT's), the paper's reference. Then the fast-oracle pool on that flow
uv run --env-file .env propamm hs-swaps WMON USDC --chain monad --start 2026-10-24 \
    --end 2026-10-31 --out data/processed/swaps/monad-MON-USDC-7d.parquet
uv run --env-file .env propamm hs-blocks --chain monad --start 2026-10-24 --end 2026-10-31
uv run propamm bybit-book MONUSDT 2026-10-24 --to 2026-10-30 --convert USDCUSDT
uv run propamm simulate-oracle data/processed/swaps/monad-MON-USDC-7d.parquet --chain monad \
    --blocks data/processed/blocks/monad-<first>-<last>.parquet --block-ms 300 \
    --prices data/processed/prices/MONUSDC-bybit-book-2026-10-24_2026-10-30.parquet \
    --gas-gwei 102 --push-gas 200000 --s-min 10,15,20

# A Chainlink feed's answers (exchange rates update about once a day)
uv run propamm chainlink "cbETH-ETH Exchange Rate" --days 7

# Pyth feed ids and market-hours schedules (the one Hermes endpoint that needs no key)
uv run propamm pyth-feeds eurc

# Pyth updates pushed on Base recently: confidence per feed, and fork-test vectors
uv run propamm pyth-pushed --blocks 200000 --vectors ../contracts/test/fork/vectors
```

**Pools.** `venues` asks each factory for the pair's pools: Uniswap v3, PancakeSwap v3, SushiSwap v3, Aerodrome Slipstream and Aerodrome v2. For Uniswap v4 it computes the ids of hookless pools for common fee and tick-spacing pairs, with WETH and with native ETH, and reads their state from the PoolManager. `hs-swaps` reads every v4 pool of the pair, hooked ones included, from the PoolManager's `Initialize` logs instead. Prices are quote per base, as the pair is named. `depth_per_bp` is the quote amount that moves the price by 1 bp at the current tick. It ranks pools; it is not a volume measure.

**Swaps.** `pair-swaps` reads every venue family's own `Swap` event: Uniswap v3, Slipstream and SushiSwap v3, PancakeSwap v3 (two extra fee fields), Aerodrome v2 (in and out amounts) and Uniswap v4 (one event per pool id on the PoolManager). Uniswap v4 logs the swapper's deltas, so they are negated to keep the pool's side. Its fee comes from each event; the other venues use the pool's fee read at discovery time.

**Ethereum and HyperSync.** `hs-swaps` reads the swaps of all of a pair's pools in one HyperSync scan, so weeks take minutes; finding the pools over RPC takes most of that time. On Ethereum it covers:
- Uniswap v3, PancakeSwap v3 and SushiSwap v3, through their factories.
- Uniswap v4: every pool of the pair, from the PoolManager's `Initialize` logs, hooked ones included.
- Curve: the pools that the MetaRegistry lists, native-ETH ones included. Stableswap and cryptoswap events are both decoded.
- Fluid DEX: the DEXes that its factory deployed.

Balancer v2 and v3 were checked and carried under 1 ETH of LST/WETH swaps in a week, so they are left out. Fluid's fee is not read yet, so its `fee_rate` is 0. Each swap also carries `tx_from` and `tx_to`, the account that sent the transaction and the contract it called, plus the pool's `token0` and `token1`.

**Flow.** `flow` sums each UTC day's volume on the ETH side. A swap counts as routed when its transaction called a known router, aggregator or intent protocol (`registry.ROUTERS`). That routed volume is what criterion 4 of the go/no-go measures. `flow` also lists the contracts that the transactions called, so unlabelled bots and solvers show up by size. A solver that calls its own contract counts as unrouted, so the routed share is a lower bound.

**Staking-token markouts.** No exchange quotes wstETH/ETH with depth, so `lst-markouts` builds its references on-chain:
- The market mid is the one that the deepest pool logging it (Uniswap v3's 1 bp pool) records after each swap, held until its next swap. A thin pool's mid can stray far from it.
- Lido's rate comes from the stETH contract's `TokenRebased` events, one per daily oracle report. It equals `wstETH.stEthPerToken()`.

A burst day is one with more than three times the median daily volume.

**Simulator.** `simulate` runs the pool of `docs/spec.md` §4 and §5 (`sim/pool.py`) through the swap data, block by block (`sim/engine.py`):
- **Anchor.** The mid is Lido's rate times `1 - offset`. `--anchor market` and `--anchor foresight` use the market mid itself, one block late or with foresight. They are upper bounds that no oracle reaches.
- **Organic orders.** Each routed transaction becomes one order per direction. A router sends it to the pool when the pool's price beats what the order really got, net of gas. With `--routing all` the pool takes the whole order within its cap. With `--routing split` it takes the slice that its steps price better than the order's average.
- **Arbitrage.** An arbitrageur trades at the top of each block, knowing the block's closing mid, and unwinds at that mid ± `--arb-cost`. That is the worst case for LPs.
- **Simplifications.** The pool is small next to the market, so the market's price path is taken as given. When the pool takes an order that moved the market mid in the data, the arbitrageur still sees that move. Both err against the pool.

The command prints the fills' markouts by flow and the volume won. It also gives two P&L figures:
- **Spread kept at each horizon**, with its APR: what the fills are worth after that horizon's adverse selection. This is the figure the go/no-go uses, at 60 s.
- **P&L against holding** the starting reserves, at the window's last mid. This also counts what the inventory made or lost afterwards, which one large dip can dominate.

**Fast-oracle simulator.** `simulate-oracle` runs the same pool on a volatile pair, with the oracle of §3 simulated from a CEX price series (`sim/oracle.py`):
- **Oracle.** A keeper sees the reference `--latency` seconds late. That delay is the pool's whole information lag behind the arbitrageur. The keeper pushes a price when it has moved `--threshold` times `s_min`, or when the price on chain is `--heartbeat` seconds old. Each push needs a newer publish time and updates the volatility estimate of §3.5.
- **Spread.** The spread is `max(s_min, s0 + k_sigma·sigma·√(age + latency))`, and swaps revert past `--max-age`.
- **Keeper.** The keeper takes `--keeper-share` of the spread revenue. Its gas is `--push-gas` per push at `--gas-gwei`, and the command reports how many times its fees cover that gas (criterion 5).
- **Arbitrage.** The arbitrageur knows the reference at the end of each block, and unwinds there at ± `--arb-cost`.
- **Eligible flow.** By default, routers that reach only their own venue's pools are left out (`registry.VENUE_ROUTERS`; `--all-routers` keeps them). `--max-paid` keeps only orders that paid at most that many bp over the reference a block earlier. Orders that overpaid went where a router seeking the best price would not send them, so a better quote would not win them either.
- **Cost.** Only blocks that have an order, follow a fill, or show a reference beyond the clamps run in full, so a week of 2 s blocks takes seconds.

On Base, thousands of hooked Uniswap v4 pools trade WETH/USDC. `hs-swaps` therefore takes their keys from the `Initialize` logs without reading their state, and `--chunk-blocks` keeps a busy pair's decoded logs from filling memory.

**Monad.** Blocks come about 300 ms apart, but their headers are stamped in whole seconds. `hs-blocks` reads every header in a window through HyperSync and spreads the three or four blocks of each second evenly over it, which is within about 150 ms of their real cadence. `markouts --blocks` and `simulate-oracle --blocks` take those times instead of the headers' seconds. Uniswap v4's native-MON/USDC 0.05 % pool carries almost all of the passive MON/USDC flow, so it is the only venue registered. Monad is charged on the gas limit, so `--push-gas` is a push's limit.

**Bybit.** `bybit-book` rebuilds Bybit's spot order book from its free daily archive (snapshots and deltas of the top 200 levels), keeps the top of the book after each change, and takes the microprice: `(bid * ask_size + ask * bid_size) / (bid_size + ask_size)`. `--convert USDCUSDT` divides by USDC's own microprice, which gives MON in USDC as the paper uses it. Bybit publishes no checksums, so only the zip's CRC is checked. `simulate-oracle --extra-routers` counts more entry points as routed, for the unlabelled contracts that many accounts call.

**Pyth.** Since Pyth's 2026 Core upgrade, Hermes needs an API key for every price endpoint, and keys come with paid plans (see `docs/spec.md` §12). `pyth-pushed` works without one. It decodes the signed updates that other users pushed to the Pyth contract on Base, taken from their transaction calldata. That sample is biased to the moments someone pushed, and only covers feeds that someone pushes.

**RPC endpoint.** Pass `--rpc` or set the `BASE_RPC_URL` environment variable. The default is PublicNode's free endpoint, and `https://base.drpc.org` also works. Base's own `mainnet.base.org` rejects `eth_getLogs`. Free endpoints are rate-limited and only good for windows of hours. For weeks or months of data, `hs-swaps` reads the logs through HyperSync with the token from `.env`. On Ethereum the default RPC is MEV Blocker's free endpoint, and `https://eth.drpc.org` also serves historical state.

**Secrets.** Tokens and keyed RPC URLs go in `analyzer/.env`, which git ignores. Copy [`.env.example`](.env.example), fill it in, and pass `--env-file .env` to `uv run`, as in `uv run --env-file .env propamm venues wstETH WETH`. `ENVIO_API_TOKEN` holds the HyperSync token (free, from envio.dev under API tokens). `BASE_RPC_URL` is optional.

## Conventions

- `d0`, `d1`: change in the **pool's** balances, in token units. Positive means the pool received tokens.
- `ts_ms`: block timestamp in milliseconds. Base blocks are 2 s apart, and position within a block is not modelled. On Monad, `hs-blocks` gives each block its own millisecond.
- Reference prices are stamped at the **end** of each kline and joined backward, so a markout never uses a future price.
- Markout at horizon `h`, in bps: `(d0 * P(t + h) + d1) / |d1| * 1e4`. Positive means the LPs gained. With fees excluded, the input side is reduced by the pool fee. The negative of that markout at `h = 0` is the LVR proxy.
