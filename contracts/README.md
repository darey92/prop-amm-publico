# contracts

This is the Foundry project that was to hold the pool's Uniswap v4 hook, its factory and its oracle adapter. The design is in [`../docs/spec.md`](../docs/spec.md). It only holds the toolchain, smoke tests and fork tests: Phase 0 ended in a no-go ([`../docs/phase-0-results.md`](../docs/phase-0-results.md)), so the hook was never written.

## Setup

Requires [Foundry](https://getfoundry.sh) and [pnpm](https://pnpm.io).

```bash
git submodule update --init --recursive   # Solidity dependencies
pnpm install                              # Pyth SDK
forge build
forge test
```

Use `FOUNDRY_PROFILE=ci forge test` to run 10,000 fuzz runs instead of 1,000.

The fork tests are skipped unless `BASE_RPC_URL` points at a Base endpoint that serves historical state. Base's own endpoint does; PublicNode needs a token for it.

```bash
BASE_RPC_URL=https://mainnet.base.org forge test --match-path 'test/fork/*' -vv
```

## Dependencies

| Package | Version | Source | License |
|---|---|---|---|
| OpenZeppelin uniswap-hooks | v1.2.1 | git submodule `lib/uniswap-hooks` | MIT |
| Uniswap v4-core | commit pinned by uniswap-hooks v1.2.1 | its nested submodule | per file: BUSL-1.1 for `PoolManager` and its internal libraries, MIT for interfaces, types and the libraries hooks use |
| Uniswap v4-periphery | commit pinned by uniswap-hooks v1.2.1 | its nested submodule | MIT (one GPL-3.0 file) |
| OpenZeppelin Contracts | commit pinned by uniswap-hooks v1.2.1 | its nested submodule | MIT |
| forge-std | v1.17.0 | git submodule `lib/forge-std` | MIT / Apache-2.0 |
| `@pythnetwork/pyth-sdk-solidity` | 4.3.1 | pnpm (`node_modules/`) | Apache-2.0 |

- **Single source per package.** `remappings.txt` points v4-core, v4-periphery and OpenZeppelin Contracts at the copies inside `lib/uniswap-hooks`, so everything compiles against the versions OpenZeppelin tested together. Auto-detection of remappings is off.
- **Where versions are pinned.** `foundry.lock` records the tag and commit of each submodule.
- **Why Pyth comes from pnpm.** The SDK is only published on npm. Its GitHub mirror is archived at v2.2.0 and predates `parsePriceFeedUpdatesUnique`.
- **No dependency code is copied into this repo.** Dependencies are fetched by commit or by version.
- **Only MIT code from v4-core goes into our contracts.** `src/` may use v4-core's interfaces, types and hook libraries (`Hooks`, `StateLibrary` and so on), which are MIT. The BUSL-1.1 files (`PoolManager`, `Pool`, `Position` and the like) are deployed only inside local tests, and `src/` must not import them.

## Smoke tests (`test/smoke/`)

**`CustomCurve.t.sol`** runs the full architecture on a real PoolManager and router, using [`FixedPriceCurve`](test/mocks/FixedPriceCurve.sol). That is a test-only hook that quotes a fixed mid price with a spread. It checks that:

- deposits become hook-owned ERC-6909 claims;
- all four swap cases fill at the quoted amounts, with pool-favouring rounding;
- the pool's value at the mid price never decreases (fuzzed);
- liquidity cannot bypass the hook;
- withdrawals return the whole inventory.

**`PythSdk.t.sol`** checks the Pyth SDK wiring. It also records a trap: in SDK 4.3.1, `MockPyth.parsePriceFeedUpdatesUnique` does **not** check uniqueness, while the deployed contract (v1.4.6) does. Tests of the one-update-per-window rule (spec §3, invariant I6) therefore need our own mock or a fork of the real contract.

## Fork tests (`test/fork/`)

**`PythUnique.t.sol`** runs the deployed Pyth contract on a Base fork with updates that someone really pushed on-chain. Each test forks one block before its update's transaction. The vectors in `test/fork/vectors/` come from the analyzer (`uv run propamm pyth-pushed --vectors ../contracts/test/fork/vectors`), which extracts signed updates from transaction calldata. The two-feed vector merges two updates signed by the same VAA, which is what Hermes returns for two feeds of one Pythnet slot. The tests check that:

- `parsePriceFeedUpdatesUnique` accepts the first update of its second and returns the vector's prices;
- it rejects the update when the window reaches back to its previous publish time, the rule `poke` relies on (spec §3, I6);
- it rejects an update older than the window;
- it stores nothing and charges no fee on Base;
- its gas stays at the measured values below.

## Facts checked on-chain (2026-10-30)

| | Base | Base Sepolia |
|---|---|---|
| Pyth contract | `0x8250f4aF4B972684F7b336503E2D6dFeDeB1487a` | `0xA2aa501b19aff244D90cc15a4Cf739D2725B5729` |
| Version | 1.4.6 | 1.4.6 |
| Update fee | 0 wei | 10 wei per update |
| Valid time period | 60 s | 60 s |
| `parsePriceFeedUpdatesUnique` | present | present |
| Its gas, cold: 1 feed (631-byte update) / 2 feeds (959 bytes) | 113,170 / 130,965 | not measured |

- **The PoolManager skips its own curve.** A hook that fills a swap entirely through `beforeSwap` sends an amount of zero to the PoolManager's curve. `Pool.swap` then returns before any protocol fee is computed, so the hook has to charge the v4 protocol fee itself (spec §6).
- **Protocol fees are live on Base v4.** The controller `0xc53DD825dedBE7814562528A1A30e8e5BCbB4b55` sets `min(lpFee/4, 1000 pips)` per pool, 3,181 pools in the last 200k blocks. It does not reach a full custom curve, and gives 0 for `PoolKey.fee = 0` (spec §12).
- **A whole `poke` will cost about 170–200k gas.** Pyth's verification alone takes 113k for one feed and 131k for two. Calldata adds about 10k and 15k, and the transaction 21k. The plan's 120k budget cannot be met.
- **Pyth's price API is now paid.** Hermes answers 401 without an API key, and keys come with data plans from $500/month. The contract itself still verifies any update, whoever fetched it (spec §12).
- **First gas figure.** A swap through the v4 test router against `FixedPriceCurve` costs about 135k gas (median). That figure covers only the v4 plumbing, with no oracle and no pricing. The target in the plan is ≤160k through the UniversalRouter.
- **First size figure.** The mock is already 15.7 kB of the 24 kB limit, so the real hook's size must be watched.
