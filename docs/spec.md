# Prop-AMM Público: pool specification (draft v0)

**Status:** Phase 0 draft, 2026-10-30. It was never implemented: Phase 0 ended on 2026-10-31 with a no-go ([`phase-0-results.md`](phase-0-results.md)). Mentions of later weeks (W4, W5) describe work that was planned and not done.

`analyzer/src/propamm_analyzer/sim/pool.py` is the executable version of §4 and §5 for the rate-anchored variant, in floating point, and the backtests run it. Its integer version comes with the contracts (W4), and the Solidity code must match that one bit for bit through shared test vectors. If this document and the reference implementation disagree, fix one of them before writing more code.

## 1. What the pool is

A Uniswap v4 pool whose hook owns all the liquidity and prices every swap itself. It is a custom curve: `beforeSwap` returns a `BeforeSwapDelta`.

- Passive LPs deposit both tokens and receive ERC-20 shares.
- Prices are anchored to an oracle mid that anyone can update.
- The pool behaves like a proprietary AMM: oracle-anchored quotes, dynamic spread, inventory skew, layered depth.
- Unlike a prop AMM, liquidity and updates are open to everyone, and nobody can change the rules after deployment.

Design rules that follow from the project's legal and security constraints:

- **Non-custodial and immutable.** No owner, admin keys, pause, proxy or upgrade. Every parameter is fixed in the constructor.
- **Withdrawals always work.** They never depend on the oracle and can never be blocked. The only exception is the short anti-JIT lock on freshly minted shares.
- **Share accounting never reads the oracle.** Deposits and withdrawals are strictly proportional.
- **Rounding always favours the pool.**
- **No fee for the developers.** The spread goes to LPs, minus the keeper share. Uniswap's v4 protocol fee is honoured if it is switched on.

## 2. Notation

| Symbol | Meaning |
|---|---|
| token0, token1 | Pool currencies in v4 order (currency0 < currency1) |
| raw units | Integer base units of a token; `d0`, `d1` are the decimals |
| `m` | Oracle mid: raw token1 per raw token0, stored as Q128.128 (`midX128`) |
| WAD | Fixed point with 1e18 = 1.0, used for spreads, fractions, decays and rates |
| `r0`, `r1` | Internal reserves, raw units |
| `R0`, `R1` | Reserves snapshotted at the first swap of the current block |
| `S` | Total share supply |
| `t` | `block.timestamp`, in seconds |

`⌈x⌉` and `⌊x⌋` round the exact rational result up and down. Implementations use `FullMath.mulDivRoundingUp` and `FullMath.mulDiv`.

## 3. Oracle updates: `poke(bytes[] updateData)`

> This section describes the fast-oracle variant (Pyth). The pilot uses the rate-anchored variant decided in §12, whose specification comes in W4.

Anyone can call `poke`, paying the Pyth update fee in `msg.value` (currently 0 on Pyth Core). Any excess is refunded.

1. **Window.** `minPublish = max(lastPublishTime + 1, t − W_back)` and `maxPublish = t + W_fwd`. Initially `W_back = W_fwd = 2 s`.
2. **Parse.** Call `pyth.parsePriceFeedUpdatesUnique(updateData, feedIds, minPublish, maxPublish)`.
   - For each feed, this accepts only the first update published at or after `minPublish`.
   - So for a given pool state and block there is exactly one acceptable update per feed.
   - Keepers choose when to update, never which price to use. This removes the "worst valid update" attack discussed on ethresear.ch.
3. **Price conversion.** Feed prices are `pᵢ·10^eᵢ`, with confidences `cᵢ·10^eᵢ`.
   - Composite (token0/USD and token1/USD feeds): `m = (P0 / P1) · 10^(d1 − d0)`, with relative confidence `c_rel = c0/p0 + c1/p1`.
   - Direct feed (token0/token1, optionally inverted): `m = P · 10^(d1 − d0)` or its inverse, with `c_rel = c/p`.
   - `lastPublishTime = max(publishTime0, publishTime1)`, so the next window is strictly newer for every feed.
   - `publishTime = min(publishTime0, publishTime1)` is the conservative value used for age.
4. **Optional sanity band.** If a Chainlink reference feed is configured and fresh (`t − updatedAt ≤ sanityMaxAge`), revert when `|m − m_ref| / m_ref > sanityMaxDev`. A stale reference is skipped and never blocks.
5. **Estimators.** These use the previous mid `m₋` and `dt = publishTime − publishTime₋ > 0`.
   - Return: `ρ = (m − m₋) / m₋`.
   - Volatility (per-second variance): `σ̂² ← (1 − λ_σ)·σ̂² + λ_σ·ρ²/dt`.
   - Toxicity: if the net token0 flow since the last update `F0 ≠ 0`, sample `τ = max(0, −sign(F0)·ρ)` and set `tox ← (1 − λ_tox)·tox + λ_tox·τ`. Then reset `F0`.
   - `F0 > 0` means users sold token0 to the pool. If the price then fell (`ρ < 0`), the sample is positive: that flow was toxic.
6. **Keeper accounting.** Move the pending keeper fees to `keeperOwed[currentKeeper]`, then set `currentKeeper = msg.sender`.
7. **Store and emit.** Store `midX128`, `c_rel` and `publishTime`, then emit `OracleUpdated`.

The estimators are updated in `poke`, so keepers pay that gas and swaps only read stored values.

## 4. Quoting

At the first swap of a block, snapshot `R0 = r0` and `R1 = r1`, and reset the per-block gross outflows `G0 = G1 = 0`. Swaps revert if the pool is empty.

Let `age = max(0, t − publishTime)`.

**Guards.** The swap reverts if any of these holds:

| Condition | Error |
|---|---|
| `age > maxAge` | `StaleOracle` |
| `c_rel > maxConf` | `ConfidenceTooWide` |
| Market closed and `marketMode = Halt` (see §8) | `MarketClosed` |

**Spread:**

    s = max(s_min, s0 + k_c·c_rel + k_σ·σ̂·√(age + L) + k_tox·tox + s_stale + s_closed)

- `s_stale = k_stale·(age − freshAge)/(maxAge − freshAge)` when `age > freshAge`, otherwise 0.
- `s_closed = closedSpreadAdd` when the market is closed and `marketMode = Widen`, otherwise 0.

**Inventory skew.** This is the value imbalance at the mid, using the block snapshot:

    q  = (R0·m − R1) / (R0·m + R1)          ∈ [−1, 1]
    m* = m · (1 − k_inv · q)

A pool that is long token0 (`q > 0`) lowers its quotes so that it sells token0 and buys token1.

**Layered prices** for steps `j = 0..3`, with `δ0 = 0 ≤ δ1 ≤ δ2 ≤ δ3`:

    ask_j = max(m*·(1 + s + δ_j), m·(1 + s_min))   rounded up
    bid_j = min(m*·(1 − s − δ_j), m·(1 − s_min))   rounded down, and must be > 0

Whatever the skew, these clamps keep the pool from quoting inside `±s_min` of the oracle mid.

**Step capacities** use fractions `0 = β0 < β1 < β2 < β3 < β4` of the block snapshot. `β4` is the per-block cap; it is multiplied by `closedCapFactor` when the market is closed and widening.

- Ask side (the pool pays token0): step `j` covers `G0 ∈ [β_j·R0, β_{j+1}·R0)`.
- Bid side (the pool pays token1): step `j` covers `G1 ∈ [β_j·R1, β_{j+1}·R1)`.

Steps advance with gross outflow, not net. So a round trip inside one block pays the spread twice and climbs the steps. In a sandwich, the front-run pushes the victim into a higher step, but the extra price paid goes to the LPs, not to the attacker.

## 5. Swap execution

v4 conventions:

- `zeroForOne = true` means the user sells token0: the pool buys at bids and pays token1.
- `amountSpecified < 0` means exact input.

All four cases walk the steps from the current `G` position. The price is constant inside a step. In each step `j`, `cap` is the remaining capacity in output-token units.

| Case | Loop body | Rounding |
|---|---|---|
| Buy token0, exact out `A0` | `take = min(rem, cap)`; `in1 += ⌈take·ask_j⌉`; `G0 += take` | input up |
| Buy token0, exact in `A1` | If `rem ≥ ⌈cap·ask_j⌉`, fill the step and continue. Otherwise `out0 += ⌊rem/ask_j⌋` and stop; the dust stays in the pool. | output down |
| Sell token0, exact in `A0` | If `rem ≥ ⌈cap/bid_j⌉`, fill the step and continue. Otherwise `out1 += ⌊rem·bid_j⌋` and stop. | output down |
| Sell token0, exact out `A1` | `take = min(rem, cap)`; `in0 += ⌈take/bid_j⌉`; `G1 += take` | input up |

Additional rules:

- Running past `β4` reverts with `BlockCapExceeded`.
- The specified amount must be at least `minTrade0` or `minTrade1`.
- The quote view runs exactly the same code as the swap. There are no branches on the caller, `tx.origin` or gas price.

## 6. Fees and reserves

**Spread revenue** is measured in input-token units against the unskewed mid. The clamps keep it non-negative:

- Buy token0: `rev1 = in1 − ⌈out0·m⌉`.
- Sell token0: `rev0 = in0 − ⌈out1/m⌉`.

**Keeper share.** `kFee = ⌊φ·rev⌋` goes into the pending keeper fees. At the next `poke` it is credited to the keeper whose update was live, which costs one storage write per block instead of one per swap.

**Uniswap protocol fee.** If the v4 protocol fee for the swap direction is `p` pips, the swapper also pays `⌈amountIn·p/1e6⌉`. `p` is read from the pool's own `slot0.protocolFee`, which Uniswap's fee controller sets (§12). It is forwarded the same way Uniswap's own custom-accounting hooks do it. This is to be confirmed in W4, and that code must be reimplemented rather than copied, because it is AGPL.

**Reserves.** Input reserve `+= in − kFee`, output reserve `−= out`. `G` and `F0` are updated.

The inventory sits in the PoolManager as the hook's ERC-6909 claims, so swaps move no ERC-20 tokens. Reserves are tracked internally in one slot and never read from balances, so donations change nothing.

## 7. Liquidity

Shares are an ERC-20 implemented by the hook.

**Deposit** `(amount0Max, amount1Max, minShares, to)`:

- **First deposit** (`S = 0`):
  - Require `a0 ≥ minFirst0` and `a1 ≥ minFirst1`.
  - `shares = ⌊√(a0·a1)⌋ − 1000`, and 1000 extra shares are minted to a dead address.
- **Later deposits:**
  - `shares = min(⌊a0·S/r0⌋, ⌊a1·S/r1⌋)`.
  - Then `a0 = ⌈shares·r0/S⌉` and `a1 = ⌈shares·r1/S⌉`.
- **Caps:** `r0 + a0 ≤ cap0` and `r1 + a1 ≤ cap1`.
- **Lock:** `unlockBlock[to] = block.number + lockBlocks`. Freshly minted shares cannot be transferred or burned until then, which stops flash and just-in-time deposits.

**Withdraw** `(shares, min0, min1, to)`:

- `a0 = ⌊shares·r0/S⌋` and `a1 = ⌊shares·r1/S⌋`.
- It never reads the oracle and can never be paused.

## 8. Market hours

This applies to references whose market closes, such as FX at weekends or equities. `marketMask` is a 168-bit weekly mask, one bit per UTC hour, where hour 0 is Monday 00:00 UTC:

    hourOfWeek = (⌊t / 3600⌋ + 72) mod 168      // 1970-01-01 was a Thursday

A set bit means the market is closed.

- `marketMode = Halt` reverts swaps while the market is closed.
- `marketMode = Widen` adds `closedSpreadAdd` to the spread and multiplies the caps by `closedCapFactor`.

Pairs with 24/7 references use an empty mask. Daylight-saving shifts are covered by padding the mask.

## 9. Parameters

All parameters are constructor arguments. They are range-checked, and their hash is emitted at deployment. Initial values are set per pair from the Phase 0 backtests.

| Group | Parameter | Unit | Constraint / note |
|---|---|---|---|
| Tokens | `token0`, `token1` | address | Decimals read at construction |
| Oracle | `pyth`; `feedId0` + `feedId1`, or `feedId` + `invert` | — | Composite or direct |
| | `W_back`, `W_fwd` | s | Update window (2, 2) |
| | `freshAge`, `maxAge` | s | `0 < freshAge < maxAge` |
| | `maxConf` | WAD | Maximum relative confidence |
| | `sanityFeed`, `sanityMaxAge`, `sanityMaxDev` | address, s, WAD | Optional Chainlink band |
| Spread | `s_min`, `s0`, `k_c`, `k_σ`, `L`, `k_tox`, `k_stale` | WAD (`L` in s) | `s_min > 0`; worst-case `s + δ3 < 1` |
| Estimators | `λ_σ`, `λ_tox` | WAD | EWMA weights in (0, 1) |
| Skew | `k_inv` | WAD | `k_inv < 1`, so `m* > 0` |
| Depth | `δ1..δ3`, `β1..β4` | WAD | Increasing; `β4 ≤ 1` is the per-block cap |
| Trades | `minTrade0`, `minTrade1` | raw | |
| Keeper | `φ` | WAD | 10–20 % initially |
| LPs | `cap0`, `cap1`, `minFirst0`, `minFirst1`, `lockBlocks` | raw / blocks | |
| Market | `marketMask`, `marketMode`, `closedSpreadAdd`, `closedCapFactor` | bits / enum / WAD | |

## 10. Events

- `OracleUpdated(address keeper, uint256 midX128, uint256 confWad, uint64 publishTime)`.
- `HookSwap(...)`, following Uniswap's convention for custom-accounting hooks so indexers see the real amounts.
- `Deposit`, `Withdraw` and `KeeperFeesClaimed`.
- `ParamsHash(bytes32)`, emitted at construction.

## 11. Invariants

These are the properties the Foundry invariant suite must test. Halmos must also prove the ones marked.

| Id | Property |
|---|---|
| I1 | **Claims solvency.** For each token, the hook's ERC-6909 claims equal its reserve plus keeper fees (pending and owed), plus donations. |
| I2 | No profitable round trip within a block at a fixed mid. |
| I3 | *(Halmos)* A swap never lowers pool value at the mid: `Δr0·m + Δr1 ≥ 0`. |
| I4 | *(Halmos)* `exactOut(exactIn(a)) ≤ a` and `exactIn(exactOut(b)) ≥ b`. |
| I5 | Share value at a fixed mid never decreases. A deposit followed by a withdraw returns no more than was deposited. |
| I6 | Only the unique, strictly newer oracle update is accepted. |
| I7 | Past `maxAge`, swaps revert while withdrawals keep working. |
| I8 | Per-block gross outflows never exceed `β4·R` on either side. |
| I9 | *(Halmos)* Every executed price is at least `m·(1 + s_min)` for buys and at most `m·(1 − s_min)` for sells. |
| I10 | Keeper credits never exceed `φ` times the spread revenue. |
| I11 | The quote view equals the executed swap. |
| I12 | The forwarded protocol fee equals the rounded-up amount due. |
| I13 | Swap deltas have the right signs in all four swap types. |

## 12. Open questions (resolve in W1–W4)

- **Oracle access and cost (decided 2026-10-30).** *Checked 2026-10-30:* since Pyth's Core upgrade in the summer of 2026, Hermes and Benchmarks answer 401 on every price endpoint without an API key. Keys come only with a paid data plan. Only the feed list (`/v2/price_feeds`) is open.
  - **Plans.** Starter costs $500/month and covers crypto and redemption rates, with 1-second updates. FX and metals are only in Pro bundles, from $2,500/month. No free API tier is listed.
  - **Who is affected.** Keepers must fetch updates, so this breaks the €0 budget for them, and for measuring criterion 6 in Phase 0. On-chain verification is unchanged and does not care who fetched an update.
  - **Options.** Pay for Starter, perhaps through a grant. Point the adapter at a pull oracle with free access. Or pilot only pairs whose fair value is on-chain, such as redemption rates.
  - **Decision.** The pilot moves to staking-token pairs whose fair value is on-chain.
    - On Base: cbETH/WETH, wstETH/WETH and weETH/WETH, anchored to Chainlink's exchange-rate feeds.
    - On Ethereum L1: the same design reads each token's own rate (`wstETH.stEthPerToken()`, `cbETH.exchangeRate()`, `weETH.getRate()`), so it needs no oracle at all.
    - Pyth-based pairs wait for the rule for paid data below. The §3 `poke` design stays for them.
  - **Alternatives checked 2026-10-30.** No fast pull oracle remains free and keyless:
    - RedStone's SDK 1.0 (2026-08-26) dropped the public gateways. It needs an `x-api-key` for `oracle-gateway.a.redstone.finance`, and the price is not published.
    - Stork gives keys on request and charges a fee per update.
    - Switchboard On-Demand has no Base deployment.
    - API3 publishes its signed data for free, but 60 s late.
  - **Chainlink on Base stays free to read, but its market feeds are coarse.** They only move on 0.1–0.5 % deviations: 0.1 % for EUR/USD and EURC/USD, 0.15 % for ETH/USD, 0.5 % for STETH/ETH and WSTETH/ETH. That is too coarse for 1 bp spreads.
  - **Chainlink's exchange-rate feeds on Base track the staking contracts.** There are feeds for wstETH–stETH, wstETH–ETH, cbETH–ETH, weETH/eETH and wrsETH–ETH. The wstETH–stETH feed updates on any change.
  - **A free design to test first.** For staking-token pairs, set the mid to the Chainlink exchange rate × (1 − `d`). The discount `d` is learned from the pool's own flow through the inventory skew, and the market feeds serve as a safety band. Swaps read the feed directly, so the price needs neither `poke` nor a keeper.
  - **Flow check on Base.** Seven days to block ~53,284,000, all venues and all senders (`propamm pair-swaps`), at $2,226 per ETH:
    - cbETH/WETH: $567k/day on average, ranging from 121 to 370 ETH a day. Aerodrome Slipstream (tick spacing 1) has 56 % and Uniswap v4 (0.5 bp) 28 %.
    - wstETH/WETH: $89k/day, 79 % of it in Uniswap v3's 1 bp pool.
    - weETH/WETH: $18k/day, almost all in one day of a few large trades.
    - wrsETH/WETH and ezETH/WETH had under $3k on the one day measured.
    - Trades are small: the median is 0.01 ETH, and 14–18 % of the volume comes in trades of 10 ETH or more.
    - Unlabelled contracts send most of the flow. One 731-byte contract made 14,298 of cbETH's 26,889 swaps, which is how an arbitrage bot looks. The routers we can name (Uniswap, 1inch, KyberSwap, Odos, Velora, CoW, Aerodrome) sent only 2–7 %.
    - **Base alone cannot meet criterion 4.** All three pairs together come to about $670k/day, below the $1M/day of router flow that a single pair needs, even if we captured every trade. Base remains the cheap place to test.
  - **Flow check on Ethereum L1.** 28 days, 2026-10-02 to 10-29 (blocks 26,100,914–26,301,844), read through HyperSync with `propamm hs-swaps` and summed with `propamm flow`, at $2,239 per ETH.
    - The check covers every venue the analyzer knows: Uniswap v3 and v4 (hooked pools included), Curve, Fluid, PancakeSwap v3 and SushiSwap v3.
    - For one day, its Uniswap v3 and Fluid swaps matched `eth_getLogs` exactly, both in count and in volume.
    - wstETH/WETH: $10.3M/day on average (median $6.65M), of which routed $4.40M (median $2.70M). Fluid has 60 % and Uniswap v3's 1 bp pool 38 %.
    - stETH/ETH: $6.44M/day (median $2.95M), of which routed $3.44M (median $1.81M). All of it is on Curve.
    - weETH/WETH: $2.11M/day (median $1.19M), of which routed $1.27M (median $0.67M). Curve has 51 %, Fluid 30 % and Uniswap v3 19 %.
    - rETH/WETH: $1.08M/day (median $0.59M), of which routed $0.51M (median $0.34M). Uniswap v3 has 59 % and v4 39 %.
    - cbETH/WETH: $0.07M/day (median $0.02M).
    - **Routed** means that the transaction called a known router, aggregator or intent protocol (`registry.ROUTERS`); KyberSwap, CoW, 1inch, 0x and Velora lead. That was 43–60 % of the volume. It is a lower bound, because solvers' own contracts, Safe multisigs and 4337 or 7702 accounts count as unrouted.
    - **wstETH/WETH meets criterion 4**, with a routed median of $2.70M/day. stETH/ETH would too, but rebasing stETH cannot sit in a v4 pool, so that flow is reachable only through routes that wrap it. weETH, rETH and cbETH fall short.
    - **Fluid is the main venue** for wstETH/ETH and the second for weETH/ETH.
    - **Volume comes in bursts.** 2026-10-29, the day of the 0.55 % dip, carried 28 % of wstETH's 28-day volume on its own, and 36 % of stETH's. Trades of 100 ETH or more make up 64 % of wstETH's volume, while the median trade is 0.22 ETH.
    - **What the flow is worth to LPs.** With $250k of TVL, a 5 % APR needs about $34/day. At 1 bp over the mid that takes $342k/day of volume, 13 % of wstETH's median routed flow, before adverse selection.
    - **An earlier one-day check was misleading.** It covered blocks 26,297,792–26,304,992 and counted only Uniswap and Curve. It also fell on the stress day, so its $131M/day was not representative.
    - A 150k-gas swap cost about $0.08 at 0.24 gwei on 2026-10-30.
    - The token contracts give the rate directly: `stEthPerToken()` read 1.2474808414, the same as Chainlink's Base feed. `cbETH.exchangeRate()` and `weETH.getRate()` were already a step newer than their Base copies.
    - Rebasing stETH cannot be held in a v4 pool, so the L1 target is wstETH/WETH. Stress is visible in the data: Chainlink's WSTETH/ETH market feed on Base dropped 0.55 % and recovered within 6 minutes on 2026-10-29, and that day carried over a quarter of the month's wstETH volume. Measuring how often the market strays from the rate by more than the spread is a W2 task.
  - **LP markouts on Ethereum L1**, wstETH/WETH over the same 28 days (`propamm lst-markouts`). The figures are volume-weighted bps, with fees unless noted, and positive means the LPs gained.
    - **References.** No exchange quotes the pair with depth; its price is made on-chain.
      - The market reference is the mid that Uniswap v3's 1 bp pool logs after each swap, held until its next swap.
      - Thin pools are left out of the reference. Uniswap v4's 1 bp pool sat within 0.3 bp of the v3 pool half the time, but strayed by up to 137 bp.
      - The second reference is Lido's rate, from `TokenRebased`, which our pool would anchor to. It rose about 0.6 bp a day, and 4.1 bp at the 10-30 report.
    - **Passive pools at 60 s:** Uniswap v3 1 bp +0.50, Fluid +0.06 and Uniswap v4 1 bp −4.5. All flow together comes to +0.14.
    - **What criterion 2 asks of us.** The best passive pool earns +0.50 bp, so our pool needs at least +1.5 bp at 60 s.
    - **Who sends the flow matters most** (at 60 s):
      - Routed flow +0.75, and +0.96 on calm days.
      - Unrouted flow −0.32.
      - Routed trades under 100 ETH on calm days +1.22, close to the bar.
      - Trades of 100 ETH or more, 64 % of the volume, −0.08.
    - **Burst days lose.** The three days with over 3× the median volume (10-02, 10-18, 10-29) came to −0.60 at 60 s, against +0.72 on calm days.
    - **Without fees**, the LVR proxy, Uniswap v3 1 bp comes to −0.50 at 60 s. Routed flow on calm days stays positive at +0.62, so it does not anticipate the price. Fluid's fee is not read yet, so its fee-free figures still include it.
    - **Against Lido's rate**, all flow comes to +0.69 bp.
      - Most of it comes from burst days (+1.21), when the pools bought wstETH on net below the rate. That gain is real only to an LP who can wait out Lido's withdrawal queue.
      - On calm days the pools sold wstETH on net, close to the rate: +0.29 with fees, −0.12 without.
    - **What follows for the design:**
      - Aim at routed flow under about 100 ETH, and keep large and burst-day flow small with per-block caps.
      - That flow already leaves passive LPs +1.2 bp, so quoting about 1 bp over the mid looks feasible.
      - The simulator (W2) must show whether skew and caps reach +1.5 bp on the flow we would actually win.
  - **Simulator results on Ethereum L1**, wstETH/WETH over the same 28 days (`propamm simulate`), with $250k of TVL.
    - **Model.**
      - The pool prices swaps as §4 and §5 say, with the mid at Lido's rate × (1 − `offset`).
      - Routed orders are replayed one per transaction and direction: 2,437 of them, 55,030 WETH.
      - A router sends an order to the pool when the pool's price beats what the order really got, net of gas. With "all-or-nothing" the pool takes the whole order. With "split" it takes the slice that its steps price better than the order's average.
      - An arbitrageur trades at the top of every block. It already knows the block's closing mid, and unwinds at that mid ± 1 bp.
      - Gas barely matters: the base fee had a median of 0.065 gwei over the window, with p90 0.15.
    - **The market sits 1–2 bp below Lido's rate.**
      - The median gap is −1.6 bp. It is in [−2, 0) bp 78 % of the time, and in [−5, −2) bp 19 %.
      - It moves about 0.4 bp a day, and dipped to −148 bp on 10-29.
      - The market accrues the rate continuously, while the rate steps once a day by 0.6 bp, so the gap saw-tooths by about 0.5 bp a day. Extrapolating the rate between reports does not make it steadier.
    - **The price to beat is about 1 bp.**
      - On calm days, routed orders paid a median of 1.0 bp against the v3 mid just before them, all costs included. Fluid's paid 0.9 bp.
      - Only 2.8 % of routed volume paid 1.5 bp or more. Even trades of 100 ETH or more paid only about 1.1 bp, because Uniswap v3's pool is deep.
    - **A moderate configuration**: `s_min` 0.6 bp, `offset` 1.5 bp, `k_inv` 100 bp, a 30 % per-block cap and split routing.
      - It wins 43 WETH a day of organic flow, 2.2 % of the routed volume, at +1.0 bp at 60 s.
      - Arbitrage adds 2.9 WETH a day, at −3.3 bp at 60 s. All fills together come to +0.74 bp.
      - The spread kept after 60 s of adverse selection comes to a 1.1 % APR.
      - The P&L against holding comes to a 3.5 % APR. Most of the difference comes from wstETH bought during the 10-29 dip, which recovered within minutes, so it is luck in one event.
    - **The grid** (672 configurations of `s_min`, `offset`, `k_inv` and the cap).
      - No configuration reaches +1.5 bp at 60 s with more than about 7 WETH a day of organic flow.
      - The best ones were chosen on the first 14 days by the APR kept at 60 s, and then run on the last 14. There they kept 0.4–4.0 %.
    - **Upper bound.** Take the market mid itself as the anchor, with foresight of each block's closing mid, which no oracle can provide.
      - With the same spread and cap, the pool would win 94 WETH a day at +0.92 bp, a 2.9 % APR at 60 s.
      - With the mid one block late, arbitrage costs the pool 30–50 bp of TVL over the 28 days. Lido's slow rate is a better anchor than a fast but late market price.
    - **Robustness** of the moderate configuration:
      - All-or-nothing routing, the lower flow bound: −0.85 bp at 60 s.
      - Gas at 1 gwei: a 0.6 % APR at 60 s.
      - $1M of TVL: −0.34 bp. The organic flow is limited by demand, while the arbitrage grows with the pool.
      - Steps of 10, 30 and 60 % at +0, +0.5 and +1.5 bp: +0.52 bp, a 1.2 % APR at 60 s.
    - **The clamp of §4 is necessary.** Without it, a skew that is steep relative to a block's capacity overshoots the market. Arbitrageurs then trade it back and forth every block. With `k_inv` of 20–50 bp and a 30 % cap, they drained 99 % of the pool.
    - **Verdict for wstETH/WETH: criteria 2, 3 and 4 are not met.**
      - The incumbents already quote about 1 bp, all costs included, and the pair barely moves. So passive LPs lose little (Uniswap v3's 1 bp pool keeps +0.5 bp), and the flow pays too little spread to earn +1.5 bp.
      - An oracle-anchored pool earns its edge from the LVR it avoids, and that LVR is small here.
      - Volatile pairs are where the LVR is. Their backtest needs no paid feed: Binance's free 1 s klines can stand in for the oracle, as the plan's W2 first intended. Only production needs a paid feed.
  - **Fast-oracle simulator on Base**, WETH/USDC over 7 days, 2026-10-23 to 10-29 (`propamm simulate-oracle`). Binance's 1 s ETHUSDC closes stand in for Pyth, and the pool has $250k of TVL.
    - **Data.**
      - 1.39 M swaps across 1,870 pools: Uniswap v3 and v4, PancakeSwap v3, Aerodrome Slipstream and v2, and SushiSwap v3. Together they traded $165 M a day.
      - Base has thousands of hooked v4 WETH/USDC pools. Scanning their `Initialize` logs alone takes about 20 minutes on HyperSync.
      - ETH fell 5.7 % over the week.
    - **Reproduction (criterion 1): the sign matches, the size does not.**
      - All passive pools together keep −1.34 bp at 2 s against Binance, or −1.20 with every trade stamped at its block's start. The paper found −0.62 bp over a year.
      - Uniswap v3's 30 bp pool carries 52 % of the volume and keeps −2.3 bp. Its mid wanders up to ±30 bp from Binance, so it trades only when arbitrageurs correct it.
      - The other pools together keep −0.35 bp.
    - **Routed flow: below criterion 4.**
      - Known routers sent a median of $1.07 M a day, 0.7 % of the volume. Unlabelled bots and solvers send most of the rest.
      - Some routers reach only their own venue's pools (`registry.VENUE_ROUTERS`: Uniswap's v2 and v3 routers, Aerodrome's). Without them, the routed flow is $0.92 M a day, below the $1 M bar.
      - Base's Blockscout sits behind a bot check, so the unlabelled contracts could not be named.
    - **Model.**
      - A keeper sees the reference `latency` late. It pushes when the price moves by `threshold` × `s_min`, and at least every 10 s.
      - The spread is `max(s_min, k_σ·σ̂·√(age + latency))`.
      - The arbitrageur knows the reference at the end of each block, and unwinds at ±1 bp.
      - A push costs 170k gas at 0.006 gwei (Base's median base fee plus tip), about $0.002.
    - **The apparent profit comes from flow that a better quote cannot win.**
      - With `s_min` 12 bp, `k_σ` 1 and 1 s of latency, the pool won $50–60k a day of routed flow at +9–11 bp at 60 s. That is an 8–11 % APR.
      - But 99 % of those orders had paid 12 bp or more elsewhere, and a third had paid over 30 bp. Most were orders of about $200 that Uniswap's router sent to the 30 bp pool or to costly v4 pools, while pools at 1–5 bp were available.
      - A router that seeks the best price would not have sent them there, so it would not send them to us either.
    - **On price-sensitive flow the pool loses money** (orders that paid at most 5 bp, or at most 10 bp, over the market):
      - With 1 or 2 s of latency, every configuration loses, from −0.3 % to −2.1 % APR at 60 s. Arbitrage takes $20–45k a day at −3 to −6 bp.
      - Even with no latency at all, which no oracle gives, the pool keeps only +1.1–2.2 bp at 60 s, about a 2.5 % APR. The keeper's fees then cover only 0.1–0.25× its gas.
    - **Inventory needs a strong skew in a trend.** One-sided retail flow left the pool long ETH as ETH fell. With `k_inv` at 10 bp that cost 65 bp of TVL in the week; at 200 bp, `q` stayed within ±0.1.
    - **Verdict for Base WETH/USDC: criteria 2–6 fail on price-sensitive flow.**
      - Criterion 6 fails too: Pyth's ETH/USD confidence (median 9.5 bp) would need an `s_min` of at least 28.5 bp.
      - Prop AMMs win this flow with sub-second updates that cost 27k gas. An open pool fed by Pyth through a keeper is slower, and pays 6× more gas per update.
      - The routed flow that a better quote can win is under $1 M a day.
  - **Fast-oracle check on Monad**, MON/USDC over 7 days, 2026-10-24 to 10-30 (`propamm hs-swaps`, `hs-blocks`, `bybit-book` and `simulate-oracle`). The reference is the paper's: Bybit's MONUSDT microprice, in USDC through Bybit's USDCUSDT book, rebuilt from Bybit's free order-book archive. The pool has $250k of TVL.
    - **Data.**
      - 165,853 swaps, $2.50 M a day. Uniswap v4's native-MON/USDC 0.05 % pool carries almost all of it. Its other MON/USDC pools traded about $2.4k a day, and in a one-day check the other venues (Uniswap v2 and v3 forks, PancakeSwap v3, LFJ Liquidity Book) traded under $1k.
      - Blocks come 302 ms apart, but their headers are stamped in whole seconds. `hs-blocks` spreads the blocks of each second over it, to within about 150 ms. The markouts barely depend on that: at 2 s they move by 0.03 bp between stamping every trade at the start and at the end of its second.
      - MON fell 8.4 % over the week, most of it on 10-29. Its 1 s volatility was 1.9 bp per √s, 2.5× ETH's on Base the week before.
      - Bybit's MONUSDT book is usually one tick wide, about 4.7 bp.
    - **Reproduction (criterion 1): the lead-up matches, the loss does not.**
      - The passive pools keep +0.22 bp at 2 s and −0.42 bp at 60 s. The paper found −2.06 bp at 2 s, from Monad's launch to August 2026.
      - Before the trades the two agree: +6.50 bp 5 s before, against the paper's +7.76. Quiet flow (the reference moved under 1 bp from 5 s before to 1 s after) is 8 % of the volume, as in the paper. It keeps +3.2 bp at 2 s, against the paper's +8.6, and moving flow −0.04, against −2.99.
      - The windows differ (one week against ten months), and so do the venues (one v4 pool now, 115 pools in the paper). Why passive LPs lost so much less this week is not pinned down.
    - **Routed flow: below criterion 4.**
      - Named routers sent a median of $0.32 M a day, 11 % of the volume, at +2.5 bp at 60 s. They are Uniswap's Universal Routers, KyberSwap, 0x Settler and Relay (`registry.ROUTERS`). 0x Settler is unverified on Monad and was named by its `execute` selector.
      - 19 unnamed contracts that 20 or more accounts call sent another $0.94 M a day, at −1.1 bp at 60 s. The largest, `0xd32e…` with 334 accounts, costs LPs 2.2 bp at 60 s, which looks like a trading service for informed users rather than a router.
      - Counting all of them, the median is $0.95 M a day, still under the bar.
    - **The price to beat is about 1 bp.** Named routers' orders under $1k, 94 % of their volume, paid a median of 1.1–1.2 bp over Bybit's microprice 300 ms before their block, all costs included.
    - **Pyth on Monad.** Others pushed 3,992 MON/USD updates a day there, with a median confidence of 3.96 bp (p90 8.2). Criterion 6 then needs an `s_min` of at least 11.9 bp, ten times the price to beat. A push costs about $0.0004: 200k gas at 102 gwei, in MON, charged on the gas limit.
    - **Simulator.** The grids use `k_inv` 200 bp, a 5 % cap, split routing and a push at a quarter of `s_min` or every 10 s. They cover `s_min` from 5 to 30 bp, `k_σ` 0 or 1 and latencies of 0–2 s, on three flow sets: named routers; those plus the unnamed callers; and of those, only orders that paid at most 10 bp.
      - With 0.6 s of latency or more, the pool loses at 60 s everywhere but one corner. At `s_min` 30 bp and 1 s it keeps +0.8–1.8 % APR, almost all of it from arbitrage fills, since it wins only $100–400 a day of organic flow. That is noise, not an edge.
      - At the 12 bp that Pyth's confidence allows, with 1 s of latency and the unnamed callers counted, the pool wins $33k a day of organic flow at +10.6 bp at 60 s. Arbitrage meanwhile takes $834k a day at −7.4 bp, a −82 % APR at 60 s.
      - Only with no latency at all, which no oracle gives, does the pool keep a profit: 2.5–2.9 % APR on named routers' flow, 3–10 % with the unnamed callers, and at most 3.7 % on orders that paid 10 bp or less.
      - The keeper covers 1.5× its gas only when arbitrage pays it.
      - The P&L against holding reaches +1.3 % of TVL in some wide-spread configurations, from being short MON while it fell. That is the trend, not an edge.
    - **Verdict for Monad MON/USDC: criteria 1–4 fail, criterion 5 passes only on arbitrage, and criterion 6 forces a spread ten times the price to beat.** The incumbent pool already fills routed orders about 1 bp from Bybit, and MON moves 2.5× as fast as ETH, so each second of oracle lag costs more than on Base.
  - **Rule for paid data.** Turn on a paid feed only when the pool's measured keeper revenue, or a grant, covers 1.5× its cost for four weeks. Pyth Starter plus `poke` gas comes to roughly $900/month. [`phase-0-results.md`](phase-0-results.md) works out the flow that needs: for Starter alone, $1.6 M a day of fills at 1 bp, or $140k a day at 12 bp.
- **Pyth on Base: behaviour and gas.** *Measured 2026-10-30 on a Base fork, with updates that were really pushed on-chain (`contracts/test/fork/`):*
  - **Gas.** `parsePriceFeedUpdatesUnique` costs 113,170 gas cold for one feed (a 631-byte update) and 130,965 for two (959 bytes). Calldata adds about 9.9k and 14.7k gas, and the transaction itself 21k. The plan's 120k budget for a whole `poke` cannot be met; 170–200k is realistic, which feeds into keeper economics (criterion 5).
  - **Uniqueness.** The deployed contract enforces the rule §3 relies on. It rejects an update when the window reaches back to the update's previous publish time, and when the update is older than the window. It stores nothing, and the update fee is 0.
  - **Versions.** Base and Base Sepolia run Pyth 1.4.6, with an update fee of 0 wei and 10 wei and a 60 s valid period. `MockPyth` 4.3.1 does not check uniqueness, so I6 is tested on the fork.
  - **Feeds.** Every candidate has a feed except ezETH. These include redemption rates (`WSTETH/STETH.RR`, `STETH/ETH.RR`, `WEETH/EETH.RR`, `CBETH/ETH.RR`, `RETH/ETH.RR`) and a market `STETH/ETH`, so a wstETH/WETH mid could be RR × market instead of a ratio of two USD feeds.
  - **Schedules.** `FX.EUR/USD` and `Metal.XAU/USD` follow market hours (§8). `EURC/USD`, `PAXG/USD` and `XAUT/USD` trade 24/7.
  - **Confidence.** We sampled 459 prices from 269 updates pushed on Base in 200k blocks; the sample is biased to the moments someone pushed. Median confidence was 9.5 bps for ETH/USD (p90 12.3), 8.6 for BTC/USD, 2.6–9.9 for other crypto and 6–16 for tokenized stocks. A composite adds both confidences, so with criterion 6 (median conf ≤ ⅓ `s_min`) a ratio of two such feeds would need `s_min` of about 55 bps. The candidate feeds must be measured before pairs are chosen.
- **Protocol fee.** *Answered 2026-10-30:* protocol fees are live on Base v4.
  - **Who sets them.** The controller `0xc53D…4b55` is not verified on Blockscout or Sourcify. One EOA (`0x2cf8…ce43`) drives it, setting `protocolFee = min(lpFee/4, 1000 pips)` per pool in batches: 41,454 `ProtocolFeeUpdated` events in the last 2M blocks, and 3,181 pools in the last 200k blocks.
  - **Our pool.** A full custom curve bypasses the fee, because `Pool.swap` returns early. With `PoolKey.fee = 0`, the controller's rule gives 0 anyway. What we pay is therefore set by allowlist policy, not by the mechanism.
  - **Proposal for W5.** The hook reads its own pool's `slot0.protocolFee` (StateLibrary) and charges it on the input, as §6 says. Governance can then set it through the existing controller, and the hook needs no admin key. Still open: where the hook sends the fee, since the controller has no verified interface.
- **Uniswap allowlist:** what does it require for ZLCA hooks? We need a quoter-based price instead of `slot0`.
- **Routing:** do 1inch and Odos route to hooked pools, and what is UniswapX's coverage on Base?
- **Calibration:** `k_σ·σ̂·√(age + L)` assumes a per-second volatility model. Check its units and calibration against the backtests.
