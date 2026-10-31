# Prop-AMM Público

Open, oracle-anchored liquidity for passive LPs: a research project that stopped after Phase 0.

> **Status: closed on 2026-10-31, with a no-go.** Backtests on three markets found no configuration in which an open, oracle-anchored pool beats passive pools for LPs with an affordable oracle. Nothing was built beyond feasibility tests, and nothing is deployed or audited. The analyzer and the measurements stay here as research. See [`docs/phase-0-results.md`](docs/phase-0-results.md).

## The idea

Proprietary AMMs ("prop AMMs") are pools whose quotes a single market maker updates every few hundred milliseconds. They already handle about a third of Solana's DEX volume and are growing fast on Base and Monad. Their LP markouts are positive, while LPs in passive AMMs keep losing to arbitrage. But only the operator can provide liquidity to a prop AMM.

Prop-AMM Público was to be the open version: a Uniswap v4 pool that quotes like a market maker.

- the mid price is anchored to an oracle;
- the spread adjusts to volatility, oracle age and flow;
- quotes are skewed by inventory;
- depth comes in layers.

Anyone could provide liquidity, and anyone could keep the oracle fresh.

## Results

| Market | Anchor | Result |
|---|---|---|
| wstETH/WETH on Ethereum, 28 days | Lido's rate, read on-chain | The pool keeps about +0.7–1.0 bp at 60 s, short of the +1.5 bp bar, because the incumbents already charge routed orders about 1 bp. |
| WETH/USDC on Base, 7 days | A fast oracle (Binance's 1 s closes) | On price-sensitive flow it loses once the oracle lags by 1 s. |
| MON/USDC on Monad, 7 days | A fast oracle (Bybit's microprice) | It loses once the oracle lags by 0.6 s, and Pyth's confidence would force a spread ten times what routed orders pay. |

Where the anchor is exact there is little LVR to save, and where there is more, the oracle comes too late. [`docs/phase-0-results.md`](docs/phase-0-results.md) scores all six go/no-go criteria, and [`docs/spec.md`](docs/spec.md) §12 has every measurement.

## Design principles

- **Non-custodial and immutable.** No owner, no admin keys, no pause, no proxy. Every parameter is fixed at deployment.
- **Exits always work.** Withdrawals never depend on the oracle and cannot be blocked.
- **The pool wins every rounding,** under one rounding policy to be proved with symbolic tests.
- **Data first.** Build the contracts only if the backtests show LPs clearly beating passive pools. They did not.

## Layout

| Path | What | Status |
|---|---|---|
| [`docs/phase-0-results.md`](docs/phase-0-results.md) | The go/no-go memo | final |
| [`docs/spec.md`](docs/spec.md) | Pool specification, and every Phase 0 measurement (§12) | draft, not implemented |
| [`analyzer/`](analyzer/) | Python: swap ingestion, LP markouts, LVR and two backtesters | complete for Phase 0 |
| [`contracts/`](contracts/) | Foundry: smoke tests of the v4 custom-curve architecture, fork tests of Pyth on Base | feasibility tests only |

## Quickstart

The analyzer needs [uv](https://docs.astral.sh/uv/). Its data goes to `analyzer/data/`, which git ignores.

```bash
cd analyzer
uv sync
uv run pytest
uv run propamm --help
```

The contracts need [Foundry](https://getfoundry.sh) and [pnpm](https://pnpm.io). See [`contracts/README.md`](contracts/README.md).

```bash
git submodule update --init --recursive
cd contracts
pnpm install
forge test
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs lint, type checks and tests for both packages on pushes to `main` and on pull requests.

## License

The code is under the MIT license (see [LICENSE](LICENSE)). The documentation in `docs/` is under CC BY 4.0.
