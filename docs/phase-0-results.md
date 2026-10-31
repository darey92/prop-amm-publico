# Phase 0 results: no-go

*2026-10-31. This memo closes Phase 0 of Prop-AMM Público, three weeks before its go/no-go date of 2026-11-20.*

## Verdict

**No-go.** Phase 0 asked whether an open, oracle-anchored Uniswap v4 pool could beat passive pools for LPs. It had to do so on flow that routers would really send it, with an oracle that a project with no budget can afford. Three markets were tested, and the answer was no in each.

| Market | Anchor | Window | Result |
|---|---|---|---|
| wstETH/WETH on Ethereum | Lido's rate, read on-chain | 28 days, 2026-10-02 to 10-29 | The pool keeps about +0.7–1.0 bp at 60 s, short of the +1.5 bp bar. |
| WETH/USDC on Base | A fast oracle, with Binance's 1 s closes standing in for Pyth | 7 days, 2026-10-23 to 10-29 | On price-sensitive flow it loses once the oracle lags by 1 s. |
| MON/USDC on Monad | A fast oracle, with Bybit's microprice standing in for Pyth | 7 days, 2026-10-24 to 10-30 | It loses once the oracle lags by 0.6 s, outside one noise-level corner. |

The hook and the rest of the plan's code were therefore not built. The repository stays public as research: the analyzer, the measurements and the feasibility tests.

## The six criteria

The plan's rule was that one pair on one chain must meet all six. Markouts are volume-weighted, in bp, at 60 s unless noted, and positive means the LPs gained. The simulated pool holds $250k.

| Criterion | wstETH/WETH, Ethereum | WETH/USDC, Base | MON/USDC, Monad |
|---|---|---|---|
| **1.** The analyzer reproduces the paper's passive-LP markouts: same sign, within ±0.3 bp | No figure to compare: the paper does not cover Ethereum. A test day's swaps matched `eth_getLogs` exactly. | **Fails.** −1.34 bp at 2 s, against the paper's −0.62. The sign matches. | **Fails.** +0.22 bp at 2 s, against −2.06. The run-up matches: +6.50 bp 5 s before the trades, against +7.76. |
| **2.** The pool's LPs keep ≥ +0.5 bp, and ≥ 1 bp more than the best passive pool | **Fails.** The best passive pool keeps +0.50, so the bar is +1.5. No configuration reaches it on more than about 7 WETH a day of organic flow. | **Fails.** Negative with 1 s of lag or more. With no lag, which no oracle gives, +1.1–2.2 bp. | **Fails.** Negative with 0.6 s of lag or more, outside one corner. |
| **3.** Still ≥ 0 with twice the lag, an adversarial keeper and the low estimate of flow | **Fails.** All-or-nothing routing, the low estimate of flow, gives −0.85 bp. | **Fails.** Already negative at the base lag. | **Fails.** Already negative at the base lag. |
| **4.** A median of ≥ $1 M a day of routed flow, and an LP APR ≥ 5 % | Flow **passes** with $2.70 M a day. APR **fails**: 1.1 % for the moderate configuration, and 0.4–4.0 % out of sample. | **Fails.** $0.92 M a day, leaving out routers that reach only their own venue's pools. About 2.5 % APR with no lag. | **Fails.** $0.32 M a day from named routers, or $0.95 M counting 19 unnamed contracts. With no lag, at most 3.7 % on orders that paid 10 bp or less. |
| **5.** Keeper fees cover ≥ 1.5× the keeper's gas | No keeper needed. | **Fails.** 0.1–0.25× with no lag. | **Fails.** At most 1.47× with no lag. Fees reach 1.5× only where arbitrage pays them, which is where the LPs lose. |
| **6.** An affordable feed, updated at least every second, with median confidence ≤ ⅓ of `s_min` | No feed needed. | **Fails.** Pyth's ETH/USD confidence (median 9.5 bp) needs `s_min` ≥ 28.5 bp, and Pyth's price API now costs $500 a month. | **Fails.** Pyth's MON/USD confidence (median 3.96 bp) needs `s_min` ≥ 11.9 bp, ten times what routed orders pay. |

The paper is [arXiv 2609.38056](https://arxiv.org/html/2609.38056v1). [`spec.md`](spec.md) §12 has every measurement and the model behind each backtest.

## Why it fails

**Where the anchor is exact, there is little to gain. Where there is more to gain, the anchor comes late.** An oracle-anchored pool earns its edge from the LVR (loss-versus-rebalancing) it avoids.
- wstETH/WETH has an exact, free anchor in Lido's rate, but the pair barely moves. Uniswap v3's 1 bp pool already keeps +0.50 bp at 60 s.
- WETH/USDC and MON/USDC move more, but the pool's mid moves only when a keeper pushes an oracle update. Arbitrageurs see the market first. With 1 s of lag on Base, or 0.6 s on Monad, they take more than the spread earns.

**The incumbents already charge routed flow about 1 bp.** All costs included, routed orders paid a median of 1.0 bp over the market on Ethereum, and 1.1–1.2 bp over Bybit's microprice on Monad. On Base, the routed orders that paid much more went to costly pools through routers that do not seek the best price, so a better quote would not win them.

**Prop AMMs update faster and more cheaply than an open pool can.** Their operator rewrites its quotes privately, for about 27k gas on Base. An open pool waits for a keeper to push a signed Pyth update, which costs 170–200k gas.

**The affordable oracles are too noisy or too coarse.**
- Pyth's confidence forces spreads of 12–28.5 bp under criterion 6, and its price API went paid in 2026.
- Chainlink's market feeds on Base move only on deviations of 0.1–0.5 %. RedStone and Stork need keys or charge per update, and API3's free data comes 60 s late.
- A paid feed would have to pay for itself. The rule in spec §12 turns one on only when keeper fees cover 1.5× its cost for four weeks, which for Pyth Starter alone is $25 a day. At the simulated keeper share of 15 % of the spread, the pool would need $1.6 M a day of fills at 1 bp, or $140k a day at 12 bp. The most organic flow any Monad configuration won at 12 bp was $39k a day, at a loss.

**Price-seeking flow is thin on the volatile pairs.** Named routers sent a median of $1.07 M a day on Base and $0.32 M a day on Monad. Bots and solvers send most of the rest. On Monad, the flow of the unnamed contracts costs LPs 1.1 bp at 60 s.

## What would change the verdict

The idea is worth another look if one of these changes:
- **Oracle updates reach the pool as fast and as cheaply as prop AMMs update their quotes:** within the block in which the market moves, ahead of its arbitrage, for about 27k gas.
- **An affordable feed reports a median confidence of about ⅓ bp,** a third of what routed orders pay.
- **A volatile pair gets ≥ $1 M a day of routed flow that seeks the best price,** while its incumbents charge several bp.

**Plan B was not tested.** It keeps a passive curve and sets the fee from the oracle (`BaseOverrideFee`), so it reads the same late oracle. What it could save LPs is what passive pools lose. That is little on Ethereum, where Uniswap v3's 1 bp pool keeps +0.50 bp at 60 s, and on Monad this week (−0.42 bp at 60 s). Base's passive pools lost the most (−1.34 bp at 2 s), so a test of Plan B would start there.

## Findings for other builders

- **Pyth.** Since its 2026 Core upgrade, Hermes needs a paid API key for every price. The contract still verifies any update, whoever fetched it. On Base, `parsePriceFeedUpdatesUnique` costs 113,170 gas cold for one feed and enforces one update per window. `MockPyth` 4.3.1 does not check that, so tests of the rule need a fork (`contracts/test/fork/`).
- **Uniswap v4.** A hook that fills every swap itself, as a full custom curve does, bypasses the v4 protocol fee, because `Pool.swap` returns before computing it. Base has thousands of hooked WETH/USDC pools, and reading their `Initialize` logs takes about 20 minutes on HyperSync.
- **Inventory skew needs a clamp.** If the skew is steep relative to what a block can take, the quote overshoots the market and arbitrageurs trade it back and forth every block. In the backtest they drained 99 % of the pool (spec §4 and §12).
- **Monad stamps its blocks in whole seconds,** though they come about 300 ms apart, so sub-second work needs interpolated times (`hs-blocks`). HyperSync's pages of headers hold under 2,000 blocks unless `max_num_blocks` asks for more.
- **Incumbent pools price routed flow tightly.** Routed orders paid about 1 bp over the market in the pools tested. The flow that overpays comes from routers that do not seek the best price, and a better quote does not reach it.

## What the repository keeps

- [`analyzer/`](../analyzer/): the Python tools behind every figure here, run as `uv run propamm <command>`. They need only free data.
  - **Swaps.** `hs-swaps` reads a pair's swaps across a chain's venues in one HyperSync scan, with each transaction's sender and entry point. It covers Ethereum, Base and Monad. `venues`, `pair-swaps` and `v3-swaps` do the same on Base over RPC.
  - **References.** `binance-klines` (1 s klines, SHA-256 checked), `bybit-book` (Bybit's order book rebuilt from its archive, as a microprice), `chainlink`, `pyth-feeds` and `pyth-pushed` (the Pyth updates others pushed on-chain, with their confidence). `hs-blocks` gives millisecond block times.
  - **Measures.** `flow` (daily volume and the routed share), `markouts` and `lst-markouts` (LP markouts and the LVR proxy).
  - **Backtests.** `simulate` runs the rate-anchored pool, and `simulate-oracle` the fast-oracle one, with a keeper, oracle lag, arbitrage and routing.
- [`contracts/`](../contracts/): a Foundry project with smoke tests of the v4 custom-curve architecture, and fork tests of Pyth on Base.
- [`spec.md`](spec.md): the pool's design, and every Phase 0 measurement in §12.

Nothing else in the plan was built: no hook, factory, oracle adapter, keeper, SDK or dashboard. Nothing is deployed or audited.

## Limitations

- **Short windows.** 28 days on Ethereum and 7 on Base and Monad. ETH fell 5.7 % and MON 8.4 % in their weeks, and every window includes the stress day of 2026-10-29.
- **Stand-in oracles.** Binance's 1 s closes and Bybit's microprice stand in for Pyth, and the oracle's lag is a parameter.
- **No validated markouts.** Criterion 1 failed on Base and Monad, so our passive-LP markouts do not match the paper's. The windows differ (a week, against ten months to a year), and so do the venues. The verdict rests on what was measured directly: the prices routed orders paid, Pyth's confidence on-chain, and backtests on the real flow.
- **Routed flow is a lower bound.** Solvers' own contracts, Safes and smart accounts count as unrouted. The largest unnamed callers stay unidentified: Base's Blockscout sits behind a bot check, and Monad's `0xd32e…` is unverified.
- **The simulated arbitrageur is the LPs' worst case.** It knows each block's closing price and unwinds at ±1 bp. Split routing, in turn, is the high estimate of the flow the pool would win.
- **Monad's block times are interpolated** within each second, to within about 150 ms.

## Reproducing the results

Every source is free: HyperSync (with a free token), data.binance.vision, Bybit's order-book archive and public RPC endpoints. The three markets take about 200 MB of data. [`analyzer/README.md`](../analyzer/README.md) gives the commands, and spec §12 the parameters of each run.
