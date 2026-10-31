"""`propamm`: command line for the Phase 0 analyzer."""

from __future__ import annotations

import argparse
import itertools
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl

from propamm_analyzer import flow, markouts, reference
from propamm_analyzer.ingest import (
    binance,
    bybit,
    chainlink,
    hypersync,
    lido,
    pyth,
    swaps,
    univ3,
    venues,
)
from propamm_analyzer.ingest.rpc import BlockClock, RpcClient
from propamm_analyzer.registry import (
    BASE,
    CANDIDATE_PAIRS,
    CHAINLINK_BASE,
    CHAINS,
    PYTH_FEEDS,
    ROUTERS,
    VENUE_ROUTERS,
    Chain,
)
from propamm_analyzer.sim import engine, oracle
from propamm_analyzer.sim.pool import PoolParams

HERMES_URL = "https://hermes.pyth.network"

DATA_DIR = Path("data")

# WETH's stand-ins in pools that hold native ETH: Uniswap v4's and Curve's or Fluid's.
NATIVE_ETH = (venues.NATIVE, venues.ETH_SENTINEL)


def _utc_timestamp(value: str) -> int:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def _horizons(value: str) -> list[int]:
    return [int(part) for part in value.split(",") if part]


def _floats(value: str) -> list[float]:
    return [float(part) for part in value.split(",") if part]


def _strings(value: str) -> list[str]:
    return [part for part in value.split(",") if part]


def _steps(value: str) -> tuple[tuple[float, float], ...]:
    """Steps as `"5:0,10:1"`: gross outflow up to 5 % of the reserve at +0 bp, then up to 10 %
    at +1 bp."""
    steps = []
    for part in value.split(","):
        beta, _, delta = part.partition(":")
        steps.append((float(beta) / 100, float(delta or 0) / 1e4))
    return tuple(steps)


def _binance_klines(args: argparse.Namespace) -> None:
    first = date.fromisoformat(args.date)
    last = date.fromisoformat(args.to) if args.to else first
    days = pl.date_range(first, last, eager=True).to_list()
    parts = []
    for day in days:
        archive = binance.download_daily_klines(args.symbol, args.interval, day, args.raw_dir)
        parts.append(binance.read_klines(archive, args.interval))
        print(f"{parts[-1].height} prices from {archive.name}")
    prices = pl.concat(parts)
    span = f"{first}" if first == last else f"{first}_{last}"
    name = f"{args.symbol}-{args.interval}-{span}.parquet"
    out: Path = args.out or DATA_DIR / "processed" / "prices" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    prices.write_parquet(out)
    print(f"{prices.height} prices -> {out}")


def _bybit_book(args: argparse.Namespace) -> None:
    first = date.fromisoformat(args.date)
    last = date.fromisoformat(args.to) if args.to else first
    days = pl.date_range(first, last, eager=True).to_list()

    def microprices(symbol: str) -> pl.DataFrame:
        parts = []
        for day in days:
            archive = bybit.download_daily_book(symbol, day, args.raw_dir)
            book = bybit.read_book(archive)
            spread = book.select(((pl.col("ask") / pl.col("bid") - 1) * 1e4).median()).item()
            print(f"{book.height} book tops in {archive.name}, median spread {spread:.2f} bp")
            parts.append(bybit.microprice(book))
        return pl.concat(parts).unique("ts_ms", keep="last", maintain_order=True)

    prices = microprices(args.symbol)
    name = args.symbol
    if args.convert:
        prices = bybit.in_quote(prices, microprices(args.convert))
        name = args.symbol.removesuffix("USDT") + args.convert.removesuffix("USDT")
    span = f"{first}" if first == last else f"{first}_{last}"
    out: Path = args.out or DATA_DIR / "processed" / "prices" / f"{name}-bybit-book-{span}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    prices.write_parquet(out)
    print(f"{prices.height} microprices -> {out}")


def _find_v3_pool(args: argparse.Namespace) -> None:
    rpc = RpcClient(args.rpc)
    address = univ3.find_pool(rpc, args.factory, args.token_a, args.token_b, args.fee)
    print(univ3.fetch_pool_info(rpc, address))


def _v3_swaps(args: argparse.Namespace) -> None:
    rpc = RpcClient(args.rpc)
    pool = univ3.fetch_pool_info(rpc, args.pool)
    latest = rpc.block_number()
    from_block = rpc.first_block_at_or_after(_utc_timestamp(args.start), hi=latest)
    to_block = rpc.first_block_at_or_after(_utc_timestamp(args.end), hi=latest) - 1
    swaps = univ3.fetch_swaps(rpc, pool, from_block, to_block, venue=args.venue, chain=args.chain)
    name = f"{args.chain}-{args.venue}-{pool.address[:10]}-{from_block}-{to_block}.parquet"
    out: Path = args.out or DATA_DIR / "processed" / "swaps" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    swaps.write_parquet(out)
    print(f"{swaps.height} swaps in blocks {from_block}-{to_block} -> {out}")


def _restamp(swaps: pl.DataFrame, blocks: Path) -> pl.DataFrame:
    """Swaps stamped with their block's time from `blocks` (hs-blocks), for chains whose own
    block stamps are whole seconds."""
    times = pl.read_parquet(blocks).select("block", pl.col("ts_ms").alias("block_ms"))
    stamped = swaps.join(times, on="block", how="left")
    if stamped["block_ms"].null_count():
        raise SystemExit(f"{blocks} misses blocks of the swaps")
    return stamped.with_columns(pl.col("block_ms").alias("ts_ms")).drop("block_ms")


def _markouts(args: argparse.Namespace) -> None:
    horizons = _horizons(args.horizons)
    swaps = pl.read_parquet(args.swaps)
    if args.blocks:
        swaps = _restamp(swaps, args.blocks)
    priced = markouts.attach_reference_prices(swaps, pl.read_parquet(args.prices), horizons)
    with pl.Config(tbl_cols=-1, tbl_width_chars=200, float_precision=3):
        for exclude_fees in (False, True):
            label = "fees excluded" if exclude_fees else "with fees"
            print(f"LP markouts, volume-weighted bps ({label}):")
            print(markouts.summarize(priced, horizons, exclude_fees=exclude_fees))
    if args.out:
        markouts.lp_markouts(priced, horizons).write_parquet(args.out)
        print(f"per-swap markouts -> {args.out}")


def _market_mid(swaps: pl.DataFrame) -> tuple[str, str, pl.DataFrame]:
    """The venue, pool and mids of the deepest pool that logs its mid, in oriented `swaps`.

    No exchange quotes staking tokens with depth, so the market reference is made on-chain.
    Thin pools' mids stray: wstETH's v4 1 bp pool sat within 0.3 bp of the v3 1 bp pool half
    the time, but strayed by up to 137 bp.
    """
    pools = swaps.group_by("venue", "pool").agg(
        pl.col("d1").abs().sum().alias("volume"),
        pl.col("price_after").is_not_null().all().alias("logs_mid"),
    )
    venue, pool = (
        pools.filter(pl.col("logs_mid"))
        .sort("volume", descending=True)
        .select("venue", "pool")
        .row(0)
    )
    return venue, pool, reference.pool_mids(swaps, [pool])


def _lido_rates(chain: Chain, from_block: int, to_block: int) -> pl.DataFrame:
    client = hypersync.HyperSyncClient.for_chain(chain.name)
    return lido.rate_history(client, chain.tokens["stETH"], from_block, to_block)


def _burst_days(swaps: pl.DataFrame, factor: float) -> list[date]:
    """UTC days whose volume is over `factor` times the median day's."""
    day = pl.from_epoch("ts_ms", time_unit="ms").dt.date()
    daily = swaps.group_by(day.alias("day")).agg(pl.col("d1").abs().sum().alias("volume"))
    threshold = factor * daily.select(pl.col("volume").median()).item()
    return list(daily.filter(pl.col("volume") > threshold).sort("day")["day"])


def _lst_markouts(args: argparse.Namespace) -> None:
    chain = CHAINS[args.chain]
    horizons = _horizons(args.horizons)
    swaps = markouts.orient(pl.read_parquet(args.swaps), {chain.tokens["WETH"], *NATIVE_ETH})
    if "price_after" not in swaps.columns:
        raise SystemExit(f"{args.swaps} has no price_after column: download it again (hs-swaps)")
    venue, mid_pool, mids = _market_mid(swaps)
    first, last = swaps.select(pl.col("block").min(), pl.col("block").max().alias("last")).row(0)
    rates = _lido_rates(chain, first - args.rate_lookback, last)
    bursts = _burst_days(swaps, args.burst)
    swaps = swaps.with_columns(
        pl.col("tx_to").is_in(list(ROUTERS[chain.name])).alias("routed"),
        pl.from_epoch("ts_ms", time_unit="ms").dt.date().is_in(bursts).alias("burst"),
    )
    routed, calm, eth = pl.col("routed"), ~pl.col("burst"), pl.col("d1").abs()
    by_volume = swaps.group_by("venue").agg(pl.col("d1").abs().sum()).sort("d1", descending=True)
    segments = [
        ("all", pl.lit(True)),
        *((venue, pl.col("venue") == venue) for venue in by_volume["venue"]),
        ("routed", routed),
        ("not routed", ~routed),
        ("calm days", calm),
        ("burst days", ~calm),
        ("routed, calm days", routed & calm),
        ("not routed, calm days", ~routed & calm),
        ("under 10 ETH", eth < 10),
        ("10 to 100 ETH", eth.is_between(10, 100, closed="left")),
        ("100 ETH or more", eth >= 100),
        ("routed under 100 ETH, calm days", routed & calm & (eth < 100)),
    ]
    print(f"{args.swaps.name}: {swaps.height} swaps, volume in ETH")
    print(f"burst days (over {args.burst}x the median volume): {', '.join(map(str, bursts))}")
    print(f"market mid: {venue} pool {mid_pool}; Lido rate from {rates.height} reports")
    references = (
        ("market mid", mids, 6, (False, True)),
        ("Lido rate", rates.select("ts_ms", "price"), 48, (False,)),
    )
    for name, prices, staleness_h, fee_views in references:
        priced = markouts.attach_reference_prices(
            swaps, prices, horizons, max_staleness_ms=staleness_h * 3_600_000
        )
        for exclude_fees in fee_views:
            label = "fees excluded; Fluid's fee is not read" if exclude_fees else "with fees"
            table = markouts.summarize_segments(
                priced, segments, horizons, exclude_fees=exclude_fees
            )
            with pl.Config(tbl_rows=30, tbl_cols=-1, tbl_width_chars=200, float_precision=2):
                print(f"\nLP markouts against the {name}, volume-weighted bps ({label}):")
                print(table)


def _markout(table: pl.DataFrame, segment: str, horizon: int) -> float | None:
    rows = table.filter(pl.col("segment") == segment) if table.height else table
    return rows[f"markout_{horizon}s_bps"][0] if rows.height else None


def _print_run(
    config: engine.SimConfig, table: pl.DataFrame, stats: dict[str, Any], unit: str = "WETH"
) -> None:
    params = config.pool
    steps = ", ".join(f"to {beta:.0%} at +{delta * 1e4:g} bp" for beta, delta in params.steps)
    print(
        f"pool: s_min {params.s_min * 1e4:g} bp, offset {params.offset * 1e4:g} bp, "
        f"k_inv {params.k_inv * 1e4:g} bp, steps {steps}, clamp {'on' if params.clamp else 'off'}, "
        f"q_max {params.q_max:g}; TVL {config.tvl:,.1f} {unit}"
    )
    if params.k_sigma or params.s0:
        print(
            f"spread: max(s_min, {params.s0 * 1e4:g} bp + {params.k_sigma:g}·sigma·√(age + "
            f"{params.latency_s:g} s)), max age {params.max_age_s:g} s, "
            f"keeper share {params.keeper_share:.0%}"
        )
    routing = "all-or-nothing" if config.routing == "all" else "split"
    arb = f"unwinds at the mid ± {config.arb_cost * 1e4:g} bp" if config.arbitrage else "off"
    print(f"routing {routing}, gas {config.gas_gwei:g} gwei, arbitrageur {arb}")
    print(
        f"organic: {stats['organic_fills']} fills from {stats['routed_orders']} routed orders, "
        f"{stats['organic_volume']:,.1f} {unit} of {stats['routed_volume']:,.0f}; per day mean "
        f"{stats['organic_per_day_mean']:,.1f}, median {stats['organic_per_day_median']:,.1f}"
    )
    print(
        f"arbitrage: {stats['arb_fills']} fills, {stats['arb_volume']:,.1f} {unit}, "
        f"per day mean {stats['arb_per_day_mean']:,.1f}"
    )
    print(
        f"P&L against holding over {stats['days']} days, at the last mid: "
        f"{stats['pnl']:+.4f} {unit} ({stats['pnl_bp']:+.2f} bp of TVL, APR {stats['apr']:+.2%}); "
        f"at the anchor: {stats['pnl_at_anchor']:+.4f} {unit}"
    )
    kept = ", ".join(
        f"{h} s {pnl:+.4f} {unit} (APR {stats['markout_apr'][h]:+.2%})"
        for h, pnl in stats["markout_pnl"].items()
    )
    print(f"spread kept after each horizon's adverse selection: {kept}")
    print(
        f"inventory q at block starts: min {stats['q_min']:+.3f}, max {stats['q_max']:+.3f}, "
        f"last {stats['q_last']:+.3f}"
    )
    with pl.Config(tbl_rows=20, tbl_cols=-1, tbl_width_chars=200, float_precision=2):
        print("markouts against the reference, volume-weighted bps:")
        print(table)


def _between(frame: pl.DataFrame, start: str | None, end: str | None) -> pl.DataFrame:
    """The rows from `start` (inclusive) to `end` (exclusive), ISO times in UTC by default."""
    if start:
        frame = frame.filter(pl.col("ts_ms") >= _utc_timestamp(start) * 1_000)
    if end:
        frame = frame.filter(pl.col("ts_ms") < _utc_timestamp(end) * 1_000)
    return frame


def _simulate(args: argparse.Namespace) -> None:
    chain = CHAINS["ethereum"]
    horizons = _horizons(args.horizons)
    swaps = markouts.orient(pl.read_parquet(args.swaps), {chain.tokens["WETH"], *NATIVE_ETH})
    venue, mid_pool, mids = _market_mid(swaps)
    bursts = _burst_days(swaps, args.burst)
    window = _between(swaps, args.start, args.end)
    first, last = window.select(pl.col("block").min(), pl.col("block").max().alias("last")).row(0)
    orders = engine.organic_orders(window, ROUTERS[chain.name])
    window_mids = mids.filter(pl.col("block").is_between(first, last))
    if args.anchor == "rate":
        rates = _lido_rates(chain, first - args.rate_lookback, last)
        anchor = f"Lido rate from {rates.height} reports"
    else:
        # Bounds that no on-chain anchor reaches: the market mid at the end of the previous
        # block, or (with foresight) at the end of the block itself. The engine applies an
        # anchor from the block after it, so foresight moves each mid one block earlier.
        lead = 1 if args.anchor == "foresight" else 0
        rates = window_mids.with_columns(pl.col("block") - lead)
        rates = pl.concat([rates.head(1).with_columns(pl.col("block") - 1), rates])
        anchor = f"the market mid itself ({args.anchor}), an upper bound"
    tvl = args.tvl_usd / args.usd_per_eth
    print(f"{args.swaps.name}, blocks {first}-{last}: {orders.height} routed orders")
    print(f"market mid: {venue} pool {mid_pool}; anchor: {anchor}")
    print(f"burst days (over {args.burst}x the median volume): {', '.join(map(str, bursts))}")
    if args.steps and len(args.cap) > 1:
        raise SystemExit("--steps replaces --cap, so give one or the other")
    grid = list(
        itertools.product(
            args.s_min, args.offset, args.k_inv, args.cap, args.q_max, args.routing, args.arb_cost
        )
    )
    rows = []
    for s_min, offset, k_inv, cap, q_max, routing, arb_cost in grid:
        params = PoolParams(
            s_min=s_min / 1e4,
            offset=offset / 1e4,
            k_inv=k_inv / 1e4,
            steps=_steps(args.steps) if args.steps else ((cap / 100, 0.0),),
            clamp=not args.no_clamp,
            q_max=q_max,
        )
        config = engine.SimConfig(
            pool=params,
            tvl=tvl,
            routing=routing,
            gas_gwei=args.gas_gwei,
            arbitrage=not args.no_arb,
            arb_cost=arb_cost / 1e4,
        )
        result = engine.simulate(orders, window_mids, rates, config)
        table, stats = engine.evaluate(result, orders, mids, horizons, burst_days=bursts)
        if len(grid) == 1:
            print()
            _print_run(config, table, stats)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                result.fills.write_parquet(args.out)
                print(f"fills -> {args.out}")
            return
        keys = {
            "s_min": s_min,
            "offset": offset,
            "k_inv": k_inv,
            "cap": cap,
            "q_max": q_max,
            "routing": routing,
            "arb_cost": arb_cost,
        }
        rows.append(keys | _grid_row(table, stats, args.grid_horizon))
    _print_grid(rows, args.out, "WETH")


def _grid_row(table: pl.DataFrame, stats: dict[str, Any], h: int) -> dict[str, Any]:
    return {
        "organic_day": stats["organic_per_day_median"],
        "share": stats["organic_volume"] / stats["routed_volume"],
        "arb_day": stats["arb_per_day_mean"],
        f"organic_{h}s": _markout(table, "organic", h),
        f"arb_{h}s": _markout(table, "arbitrage", h),
        f"all_{h}s": _markout(table, "all fills", h),
        f"apr_{h}s_pct": stats["markout_apr"][h] * 100,
        "pnl_bp": stats["pnl_bp"],
        "apr_pct": stats["apr"] * 100,
        "q_max_seen": stats["q_max"],
    }


def _print_grid(rows: list[dict[str, Any]], out: Path | None, unit: str) -> None:
    frame = pl.DataFrame(rows)
    with pl.Config(tbl_rows=500, tbl_cols=-1, tbl_width_chars=250, float_precision=2):
        print(f"\n{frame.height} configurations; volumes in {unit} per day, markouts in bps:")
        print(frame)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(out)
        print(f"grid -> {out}")


def _simulate_oracle(args: argparse.Namespace) -> None:
    chain = CHAINS[args.chain]
    horizons = _horizons(args.horizons)
    unit = args.quote
    swaps = markouts.orient(pl.read_parquet(args.swaps), {chain.tokens[unit]})
    if args.blocks:
        swaps = _restamp(swaps, args.blocks)
    prices = (
        pl.concat([pl.read_parquet(path) for path in args.prices]).unique("ts_ms").sort("ts_ms")
    )
    bursts = _burst_days(swaps, args.burst)
    window = _between(swaps, args.start, args.end)
    first, last = window.select(pl.col("block").min(), pl.col("block").max().alias("last")).row(0)
    if args.blocks:
        blocks = pl.read_parquet(args.blocks).filter(pl.col("block").is_between(first, last))
        if blocks.height != last - first + 1:
            raise SystemExit(f"{args.blocks} does not cover blocks {first}-{last}")
        blocks = blocks.sort("block")
    else:
        stamped = window.select("block", "ts_ms").unique("block").sort("block")
        blocks = oracle.block_times(first, last, stamped, args.block_ms)
    reference = oracle.reference_at_blocks(blocks, prices)
    routers = set(ROUTERS[chain.name]) | {address.lower() for address in args.extra_routers}
    if not args.all_routers:
        routers -= VENUE_ROUTERS[chain.name]
    orders = engine.organic_orders(window, routers)
    if args.max_paid is not None:
        # Orders that paid far over the market went where a price-seeking router would not send
        # them, so quoting better would not win them either.
        before = reference.select(pl.col("block") + 1, pl.col("price").alias("market"))
        paid = (
            orders.join(before, on="block", how="left")
            .with_columns(
                pl.when(pl.col("sells_base"))
                .then(pl.col("amount_out") / pl.col("amount_in"))
                .otherwise(pl.col("amount_in") / pl.col("amount_out"))
                .alias("average")
            )
            .with_columns(
                pl.when(pl.col("sells_base"))
                .then(1 - pl.col("average") / pl.col("market"))
                .otherwise(pl.col("average") / pl.col("market") - 1)
                .alias("paid")
            )
        )
        orders = paid.filter(pl.col("paid") * 1e4 <= args.max_paid).drop(
            "market", "average", "paid"
        )
    # The volatility estimate starts from the window's first day of 1 s returns.
    start_ms = blocks["ts_ms"][0]
    day_one = prices.filter(pl.col("ts_ms").is_between(start_ms, start_ms + 86_400_000))
    sigma0 = day_one.select(
        ((pl.col("price").log().diff() ** 2).sum() / (pl.col("ts_ms").diff() / 1_000).sum()).sqrt()
    ).item()
    mean_price = reference.select(pl.col("price").mean()).item()
    days = (blocks["ts_ms"][-1] - start_ms) / 86_400_000
    print(f"{args.swaps.name}, blocks {first}-{last} ({days:.1f} days): {orders.height} orders")
    print(
        f"reference: {prices.height} prices from {', '.join(p.name for p in args.prices)}; "
        f"starting volatility {sigma0 * 1e4:.2f} bp per √s"
    )
    if args.steps and len(args.cap) > 1:
        raise SystemExit("--steps replaces --cap, so give one or the other")
    grid = list(
        itertools.product(
            args.s_min,
            args.k_sigma,
            args.latency,
            args.k_inv,
            args.cap,
            args.routing,
            args.arb_cost,
            args.keeper_share,
            args.threshold,
        )
    )
    pushes_for: dict[tuple[float, float], pl.DataFrame] = {}
    rows = []
    for s_min, k_sigma, latency, k_inv, cap, routing, arb_cost, share, push_at in grid:
        threshold = push_at * s_min / 1e4
        if (latency, threshold) not in pushes_for:
            pushes_for[latency, threshold] = oracle.keeper_pushes(
                blocks,
                prices,
                latency_s=latency,
                threshold=threshold,
                heartbeat_s=args.heartbeat,
                sigma0=sigma0,
            )
        pushes = pushes_for[latency, threshold]
        params = PoolParams(
            s_min=s_min / 1e4,
            s0=args.s0 / 1e4,
            k_sigma=k_sigma,
            latency_s=latency,
            max_age_s=args.max_age,
            k_inv=k_inv / 1e4,
            steps=_steps(args.steps) if args.steps else ((cap / 100, 0.0),),
            clamp=not args.no_clamp,
            q_max=args.q_max,
            keeper_share=share,
        )
        config = engine.SimConfig(
            pool=params,
            tvl=args.tvl_usd,
            routing=routing,
            gas_gwei=args.gas_gwei,
            gas_in_base=True,
            arbitrage=not args.no_arb,
            arb_cost=arb_cost / 1e4,
            block_ms=args.block_ms,
        )
        # The pool opens once the keeper has pushed a price, `latency` into the window.
        opens = pushes["block"][0] + 1
        open_orders = orders.filter(pl.col("block") >= opens)
        open_reference = reference.filter(pl.col("block") >= opens)
        result = engine.simulate(open_orders, open_reference, pushes, config)
        table, stats = engine.evaluate(
            result, open_orders, prices, horizons, burst_days=bursts, max_staleness_ms=10_000
        )
        pushed = pushes.filter(pl.col("block").is_between(first - 1, last)).height
        keeper_cost = pushed * args.push_gas * args.gas_gwei * engine.GWEI * mean_price
        if len(grid) == 1:
            print()
            _print_run(config, table, stats, unit)
            print(
                f"keeper: {pushed} pushes ({pushed / days:,.0f} a day), gas {keeper_cost:,.2f} "
                f"{unit}, fees {result.keeper_fees:,.2f} {unit} "
                f"({result.keeper_fees / keeper_cost:.2f}x its gas)"
            )
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                result.fills.write_parquet(args.out)
                print(f"fills -> {args.out}")
            return
        keys = {
            "s_min": s_min,
            "k_sigma": k_sigma,
            "latency": latency,
            "k_inv": k_inv,
            "cap": cap,
            "routing": routing,
            "arb_cost": arb_cost,
            "keeper": share,
            "push_at": push_at,
        }
        keeper = {
            "pushes_day": pushed / days,
            "keeper_x_gas": result.keeper_fees / keeper_cost if keeper_cost else None,
        }
        rows.append(keys | _grid_row(table, stats, args.grid_horizon) | keeper)
    _print_grid(rows, args.out, unit)


def _pyth_feeds(args: argparse.Namespace) -> None:
    # The feed list is the one Hermes endpoint that needs no API key.
    params = {"query": args.query} if args.query else {}
    response = httpx.get(f"{HERMES_URL}/v2/price_feeds", params=params, timeout=60.0)
    response.raise_for_status()
    for feed in sorted(response.json(), key=lambda f: f["attributes"].get("symbol", "")):
        attributes = feed["attributes"]
        print(
            f"0x{feed['id']}  {attributes.get('symbol', '?'):28}  {attributes.get('schedule', '')}"
        )


def _write_vector(pushed: pyth.PushedUpdate, update: pyth.AccumulatorUpdate, out: Path) -> None:
    """A fork-test vector: fork at `block - 1`, just before the update was pushed."""
    messages = update.messages
    vector = {
        "chain": BASE.name,
        "block": pushed.block,
        "txHash": pushed.tx_hash,
        "slot": update.slot,
        "feedIds": [m.feed_id for m in messages],
        "prices": [m.price for m in messages],
        "confs": [m.conf for m in messages],
        "expos": [m.expo for m in messages],
        "publishTimes": [m.publish_time for m in messages],
        "prevPublishTimes": [m.prev_publish_time for m in messages],
        "data": "0x" + update.encode().hex(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(vector, indent=2) + "\n")
    print(f"vector: {len(messages)} message(s), block {pushed.block} -> {out}")


def _pyth_pushed(args: argparse.Namespace) -> None:
    rpc = RpcClient(args.rpc)
    head = rpc.block_number()
    pushed = list(pyth.fetch_pushed_updates(rpc, BASE.pyth, head - args.blocks, head))
    frame = pyth.messages_frame(pushed)
    names = {feed_id: symbol for symbol, feed_id in PYTH_FEEDS.items()}
    print(f"{len(pushed)} pushed updates in blocks {head - args.blocks}-{head}")
    with pl.Config(tbl_rows=40, tbl_width_chars=200, float_precision=2, fmt_str_lengths=70):
        print(pyth.summarize_messages(frame, names))
    if args.out:
        frame.write_parquet(args.out)
        print(f"messages -> {args.out}")
    if args.vectors is None:
        return
    # The latest single-message update of the requested feed, and the latest pair of updates
    # signed by the same VAA, merged into one two-message update.
    feed_id = PYTH_FEEDS.get(args.vector_feed, args.vector_feed).lower()
    singles = [p for p in pushed if [m.feed_id for m in p.update.messages] == [feed_id]]
    if singles:
        _write_vector(singles[-1], singles[-1].update, args.vectors / "pyth-1-feed.json")
    by_vaa: dict[bytes, list[pyth.PushedUpdate]] = {}
    for p in pushed:
        if len(p.update.messages) == 1:
            by_vaa.setdefault(p.update.vaa, []).append(p)
    pairs = [
        group
        for group in by_vaa.values()
        if len({g.update.messages[0].feed_id for g in group}) >= 2
    ]
    if pairs:
        group = pairs[-1]
        first = group[0]
        second = next(
            g for g in group if g.update.messages[0].feed_id != first.update.messages[0].feed_id
        )
        merged = pyth.merge_updates([first.update, second.update])
        _write_vector(first, merged, args.vectors / "pyth-2-feeds.json")


def _token(symbol_or_address: str, chain: Chain = BASE) -> str:
    return chain.tokens.get(symbol_or_address, symbol_or_address).lower()


def _venues(args: argparse.Namespace) -> None:
    rpc = RpcClient(args.rpc)
    pairs = CANDIDATE_PAIRS if args.candidates else [(args.token_a, args.token_b)]
    if not args.candidates and not (args.token_a and args.token_b):
        raise SystemExit("give two tokens (symbols or addresses) or --candidates")
    decimals = {venues.NATIVE: 18}
    frames = []
    for symbol_a, symbol_b in pairs:
        token_a, token_b = _token(symbol_a), _token(symbol_b)
        for token in (token_a, token_b):
            if token not in decimals:
                decimals[token] = rpc.call_function(token, "decimals()")[0]
        pools = venues.find_pools(
            rpc, BASE.venues, token_a, token_b, wrapped_native=BASE.tokens["WETH"]
        )
        frames.append(
            venues.pools_frame(
                pools,
                f"{symbol_a}/{symbol_b}",
                token_b,
                decimals,
                wrapped_native=BASE.tokens["WETH"],
            )
        )
        print(f"{symbol_a}/{symbol_b}: {len(pools)} pools")
    table = pl.concat(frames)
    with pl.Config(tbl_rows=200, tbl_width_chars=220, float_precision=4, fmt_str_lengths=40):
        print(table.drop("token0", "token1", "liquidity"))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        table.write_parquet(args.out)
        print(f"pools -> {args.out}")


def _window(rpc: RpcClient, args: argparse.Namespace) -> tuple[int, int]:
    """Blocks for `--days` back from the head, or for `--start`/`--end` in UTC."""
    latest = rpc.block_number()
    if args.days is not None:
        start = rpc.block_timestamp(latest) - int(args.days * 86_400)
        return rpc.first_block_at_or_after(start, hi=latest), latest
    from_block = rpc.first_block_at_or_after(_utc_timestamp(args.start), hi=latest)
    return from_block, rpc.first_block_at_or_after(_utc_timestamp(args.end), hi=latest) - 1


def _pair_swaps(args: argparse.Namespace) -> None:
    rpc = RpcClient(args.rpc)
    token_a, token_b = _token(args.token_a), _token(args.token_b)
    weth = BASE.tokens["WETH"]
    from_block, to_block = _window(rpc, args)
    pools = venues.find_pools(rpc, BASE.venues, token_a, token_b, wrapped_native=weth)
    decimals = {venues.NATIVE: 18}
    for token in (token_a, token_b):
        decimals[token] = rpc.call_function(token, "decimals()")[0]
    clock = BlockClock(rpc, from_block, to_block)
    by_name = {venue.name: venue for venue in BASE.venues}
    frames, rows = [], []
    for pool in pools:
        frame = swaps.fetch_swaps(
            rpc,
            by_name[pool.venue],
            pool,
            from_block,
            to_block,
            timestamp=clock.timestamp,
            decimals=decimals,
            chain=BASE.name,
        )
        if frame.is_empty():
            continue
        # Quote volume: the side of the pool that holds token_b (WETH or native ETH).
        quote_is_1 = pool.token1 == token_b or (token_b == weth and pool.token1 == venues.NATIVE)
        volume = frame["d1" if quote_is_1 else "d0"].abs().sum()
        rows.append(
            {
                "venue": pool.venue,
                "setting": pool.setting,
                "swaps": frame.height,
                "senders": frame["sender"].n_unique(),
                "volume": volume,
            }
        )
        frames.append(frame)
    pair = f"{args.token_a}/{args.token_b}"
    days = (clock.timestamp(to_block) - clock.timestamp(from_block)) / 86_400
    summary = pl.DataFrame(rows).sort("volume", descending=True)
    summary = summary.with_columns(
        (pl.col("volume") / days).alias("volume_per_day"),
        (pl.col("volume") / pl.col("volume").sum()).alias("share"),
    )
    with pl.Config(tbl_rows=50, tbl_width_chars=160, float_precision=2):
        print(f"{pair}: blocks {from_block}-{to_block} ({days:.2f} days), volume in {args.token_b}")
        print(summary)
    if frames and args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        pl.concat(frames).sort("block", "log_index").write_parquet(args.out)
        print(f"swaps -> {args.out}")


def _hs_swaps(args: argparse.Namespace) -> None:
    chain = CHAINS[args.chain]
    rpc = RpcClient(args.rpc or chain.public_rpc)
    client = hypersync.HyperSyncClient.for_chain(chain.name)
    token_a, token_b = _token(args.token_a, chain), _token(args.token_b, chain)
    wrapped = chain.tokens[chain.wrapped_native]
    from_block, to_block = _window(rpc, args)
    to_block = min(to_block, client.height() - 1)
    pools = venues.find_pools(
        rpc,
        chain.venues,
        token_a,
        token_b,
        wrapped_native=wrapped,
        hypersync=client,
        read_state=False,
    )
    decimals = dict.fromkeys(NATIVE_ETH, 18)
    for pool in pools:
        for token in (pool.token0, pool.token1):
            if token not in decimals:
                decimals[token] = rpc.call_function(token, "decimals()")[0]
    # Chunks keep the decoded logs of busy pairs, like Base's WETH/USDC, from filling memory.
    step = args.chunk_blocks or to_block - from_block + 1
    parts = []
    for start in range(from_block, to_block + 1, step):
        end = min(start + step - 1, to_block)
        parts.append(
            swaps.fetch_swaps_hypersync(
                client, chain.venues, pools, start, end, decimals=decimals, chain=chain.name
            )
        )
        if args.chunk_blocks:
            print(f"blocks {start}-{end}: {parts[-1].height} swaps", flush=True)
    frame = pl.concat(parts, how="vertical_relaxed")
    quote_tokens = {token_b, *NATIVE_ETH} if token_b == wrapped else {token_b}
    quoted = flow.with_quote(frame, quote_tokens)
    days = (rpc.block_timestamp(to_block) - rpc.block_timestamp(from_block)) / 86_400
    settings = {pool.pool: pool.setting for pool in pools}
    summary = (
        quoted.group_by("venue", "pool")
        .agg(
            pl.len().alias("swaps"),
            pl.col("tx_to").n_unique().alias("entry_points"),
            pl.col("quote").sum().alias("volume"),
        )
        .with_columns(
            pl.col("pool").replace_strict(settings, default=None).alias("setting"),
            (pl.col("volume") / days).alias("volume_per_day"),
            (pl.col("volume") / pl.col("volume").sum()).alias("share"),
        )
        .sort("volume", descending=True)
        .select("venue", "setting", "swaps", "entry_points", "volume", "volume_per_day", "share")
    )
    pair = f"{args.token_a}/{args.token_b}"
    with pl.Config(tbl_rows=50, tbl_width_chars=200, fmt_str_lengths=80, float_precision=2):
        print(f"{pair} on {chain.name}: {len(pools)} pools found, blocks {from_block}-{to_block}")
        print(f"({days:.2f} days), volume in {args.token_b}")
        print(summary)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(args.out)
        print(f"{frame.height} swaps -> {args.out}")


def _hs_blocks(args: argparse.Namespace) -> None:
    chain = CHAINS[args.chain]
    rpc = RpcClient(args.rpc or chain.public_rpc)
    client = hypersync.HyperSyncClient.for_chain(chain.name)
    from_block, to_block = _window(rpc, args)
    to_block = min(to_block, client.height() - 1)
    times = client.block_times(from_block, to_block)
    if len(times) != to_block - from_block + 1:
        raise SystemExit(f"HyperSync sent {len(times)} of {to_block - from_block + 1} headers")
    headers = pl.DataFrame(
        {"block": list(times), "ts": list(times.values())},
        schema={"block": pl.Int64, "ts": pl.Int64},
    )
    blocks = oracle.subsecond_block_times(headers)
    span_ms = blocks.select(pl.col("ts_ms").max() - pl.col("ts_ms").min()).item()
    spacing = span_ms / (to_block - from_block)
    name = f"{chain.name}-{from_block}-{to_block}.parquet"
    out: Path = args.out or DATA_DIR / "processed" / "blocks" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    blocks.write_parquet(out)
    print(f"{blocks.height} blocks, {spacing:.1f} ms apart on average -> {out}")


def _describe(rpc: RpcClient, address: str | None) -> str:
    if address is None:
        return "contract creation"
    code = rpc.call("eth_getCode", [address, "latest"])
    return "EOA" if code in ("0x", "") else f"contract ({(len(code) - 2) // 2} B)"


def _flow(args: argparse.Namespace) -> None:
    chain = CHAINS[args.chain]
    routers = ROUTERS[chain.name]
    rpc = RpcClient(args.rpc or chain.public_rpc)
    usd = args.usd_per_eth

    def amount(eth: float) -> str:
        return f"{eth:,.0f} ETH" + (f" (${eth * usd / 1e6:,.2f}M)" if usd else "")

    for path in args.swaps:
        frame = flow.with_quote(pl.read_parquet(path), {chain.tokens["WETH"], *NATIVE_ETH})
        per_day = flow.daily(frame, routers)
        stats = per_day.select(
            pl.col("volume").mean().alias("mean"),
            pl.col("volume").median().alias("median"),
            pl.col("routed").mean().alias("routed_mean"),
            pl.col("routed").median().alias("routed_median"),
            (pl.col("routed").sum() / pl.col("volume").sum()).alias("routed_share"),
        ).row(0, named=True)
        print(f"\n== {path.name}: {frame.height} swaps on {per_day.height} UTC days")
        print(f"   volume per day: mean {amount(stats['mean'])}, median {amount(stats['median'])}")
        print(
            f"   routed per day: mean {amount(stats['routed_mean'])}, "
            f"median {amount(stats['routed_median'])}; {stats['routed_share']:.0%} of the volume"
        )
        print("   ETH per day:", [round(v) for v in per_day["volume"].to_list()])
        print("   routed ETH per day:", [round(v) for v in per_day["routed"].to_list()])
        by_venue = frame.group_by("venue").agg(pl.col("quote").sum()).sort("quote", descending=True)
        total = by_venue["quote"].sum()
        print("   by venue:", ", ".join(f"{v} {q / total:.0%}" for v, q in by_venue.iter_rows()))
        print("   transactions called:")
        for row in flow.entry_points(frame, routers).head(args.top).iter_rows(named=True):
            label = row["label"] or _describe(rpc, row["tx_to"])
            print(f"     {row['tx_to']} {label:30} {row['share']:6.1%} in {row['swaps']} swaps")


def _chainlink_history(args: argparse.Namespace) -> None:
    rpc = RpcClient(args.rpc)
    info = chainlink.feed(rpc, CHAINLINK_BASE.get(args.feed, args.feed))
    from_block, to_block = _window(rpc, args)
    history = chainlink.answer_history(rpc, info, from_block, to_block)
    print(f"{info.description} ({info.proxy}), aggregator {info.aggregator}")
    with pl.Config(tbl_rows=60, float_precision=8):
        print(history)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        history.write_parquet(args.out)
        print(f"answers -> {args.out}")


def _add_window(parser: argparse.ArgumentParser) -> None:
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--days", type=float, help="this many days back from the head")
    window.add_argument("--start", help="ISO time, UTC unless an offset is given (with --end)")
    parser.add_argument("--end", help="ISO time, exclusive")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="propamm", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    # A blank BASE_RPC_URL (an empty line in .env) falls back to the free endpoint.
    rpc_default = os.environ.get("BASE_RPC_URL") or BASE.public_rpc

    klines = commands.add_parser(
        "binance-klines", help="download Binance spot klines for one UTC day or a range"
    )
    klines.add_argument("symbol", help="e.g. ETHUSDC")
    klines.add_argument("date", help="UTC day, YYYY-MM-DD")
    klines.add_argument("--to", help="last UTC day of a range, inclusive")
    klines.add_argument("--interval", default="1s", choices=sorted(binance.INTERVAL_MS))
    klines.add_argument("--raw-dir", type=Path, default=DATA_DIR / "raw" / "binance")
    klines.add_argument("--out", type=Path)
    klines.set_defaults(func=_binance_klines)

    book = commands.add_parser(
        "bybit-book", help="Bybit spot microprices from its order-book archive, by UTC day"
    )
    book.add_argument("symbol", help="e.g. MONUSDT")
    book.add_argument("date", help="UTC day, YYYY-MM-DD")
    book.add_argument("--to", help="last UTC day of a range, inclusive")
    book.add_argument("--convert", help="divide by this book's microprice, e.g. USDCUSDT")
    book.add_argument(
        "--raw-dir", type=Path, default=DATA_DIR / "raw" / "bybit" / "orderbook" / "spot"
    )
    book.add_argument("--out", type=Path)
    book.set_defaults(func=_bybit_book)

    find = commands.add_parser("find-v3-pool", help="look up a v3-style pool by tokens and fee")
    find.add_argument("--rpc", default=rpc_default)
    find.add_argument("--factory", default=BASE.uniswap_v3_factory)
    find.add_argument("--token-a", required=True)
    find.add_argument("--token-b", required=True)
    find.add_argument("--fee", type=int, required=True, help="fee in pips: 500 = 0.05 %%")
    find.set_defaults(func=_find_v3_pool)

    swaps = commands.add_parser("v3-swaps", help="fetch a v3-style pool's swaps in a UTC window")
    swaps.add_argument("--rpc", default=rpc_default)
    swaps.add_argument("--pool", required=True)
    swaps.add_argument("--start", required=True, help="ISO time, UTC unless an offset is given")
    swaps.add_argument("--end", required=True, help="ISO time, exclusive")
    swaps.add_argument("--venue", default="uniswap-v3")
    swaps.add_argument("--chain", default=BASE.name)
    swaps.add_argument("--out", type=Path)
    swaps.set_defaults(func=_v3_swaps)

    pair = commands.add_parser("pair-swaps", help="every swap of a pair across Base venues")
    pair.add_argument("token_a", help="base token: symbol from the registry or address")
    pair.add_argument("token_b", help="quote token")
    _add_window(pair)
    pair.add_argument("--rpc", default=rpc_default)
    pair.add_argument("--out", type=Path, help="optional parquet with every swap")
    pair.set_defaults(func=_pair_swaps)

    hs_swaps = commands.add_parser(
        "hs-swaps", help="a pair's swaps across a chain's venues, via HyperSync (needs a token)"
    )
    hs_swaps.add_argument("token_a", help="symbol from the chain's registry, or address")
    hs_swaps.add_argument("token_b", help="the quote token, e.g. WETH")
    hs_swaps.add_argument("--chain", choices=sorted(CHAINS), default="ethereum")
    _add_window(hs_swaps)
    hs_swaps.add_argument("--rpc", help="for pool state and block times (default: the chain's)")
    hs_swaps.add_argument(
        "--chunk-blocks", type=int, help="fetch this many blocks at a time, to bound memory"
    )
    hs_swaps.add_argument("--out", type=Path, help="optional parquet")
    hs_swaps.set_defaults(func=_hs_swaps)

    hs_blocks = commands.add_parser(
        "hs-blocks",
        help="every block's time in ms, via HyperSync, for chains that stamp whole seconds",
    )
    hs_blocks.add_argument("--chain", choices=sorted(CHAINS), default="monad")
    _add_window(hs_blocks)
    hs_blocks.add_argument("--rpc", help="for block times (default: the chain's)")
    hs_blocks.add_argument("--out", type=Path, help="parquet (default: data/processed/blocks/)")
    hs_blocks.set_defaults(func=_hs_blocks)

    flows = commands.add_parser("flow", help="daily volume of swap files and the routed share")
    flows.add_argument("swaps", type=Path, nargs="+", help="parquet files from hs-swaps")
    flows.add_argument("--chain", choices=sorted(CHAINS), default="ethereum")
    flows.add_argument("--usd-per-eth", type=float, help="also show volumes in dollars")
    flows.add_argument("--top", type=int, default=8, help="how many called contracts to list")
    flows.add_argument("--rpc", help="tells contracts from EOAs (default: the chain's)")
    flows.set_defaults(func=_flow)

    lst = commands.add_parser(
        "lst-markouts", help="wstETH/WETH LP markouts against the Uniswap mid and Lido's rate"
    )
    lst.add_argument("swaps", type=Path, help="parquet from hs-swaps")
    lst.add_argument("--chain", choices=sorted(CHAINS), default="ethereum")
    lst.add_argument("--horizons", default="0,12,60,300,3600", help="seconds, comma-separated")
    lst.add_argument(
        "--burst", type=float, default=3.0, help="burst day: over this times the median volume"
    )
    lst.add_argument(
        "--rate-lookback",
        type=int,
        default=14_400,
        help="blocks before the first swap to search for the last Lido report",
    )
    lst.set_defaults(func=_lst_markouts)

    sim = commands.add_parser(
        "simulate",
        help="backtest the rate-anchored pool on wstETH/WETH flow; lists of values run a grid",
    )
    sim.add_argument("swaps", type=Path, help="wstETH/WETH parquet from hs-swaps on Ethereum")
    sim.add_argument("--s-min", type=_floats, default=[1.0], help="half-spread, bp")
    sim.add_argument(
        "--offset", type=_floats, default=[1.5], help="the mid's discount to Lido's rate, bp"
    )
    sim.add_argument(
        "--k-inv", type=_floats, default=[10.0], help="the mid's skew at a one-sided pool, bp"
    )
    sim.add_argument(
        "--cap", type=_floats, default=[10.0], help="per-block outflow cap, %% of the reserve"
    )
    sim.add_argument("--steps", help="instead of --cap: 'β%%:δbp,...', e.g. '5:0,10:1'")
    sim.add_argument(
        "--q-max", type=_floats, default=[1.0], help="stop adding to an imbalance past this q"
    )
    sim.add_argument("--no-clamp", action="store_true", help="let the skew move both sides")
    sim.add_argument(
        "--routing", type=_strings, default=["all"], help="'all' (all-or-nothing) or 'split'"
    )
    sim.add_argument(
        "--arb-cost", type=_floats, default=[1.0], help="the arbitrageur's unwind cost, bp"
    )
    sim.add_argument("--no-arb", action="store_true", help="leave the arbitrageur out")
    sim.add_argument(
        "--anchor",
        choices=("rate", "market", "foresight"),
        default="rate",
        help="Lido's rate, or as upper bounds the market mid one block late or with foresight",
    )
    sim.add_argument("--gas-gwei", type=float, default=0.1, help="base fee plus tip")
    sim.add_argument("--tvl-usd", type=float, default=250_000)
    sim.add_argument("--usd-per-eth", type=float, default=2_239)
    sim.add_argument("--start", help="simulate from this ISO time (UTC unless an offset is given)")
    sim.add_argument("--end", help="simulate up to this ISO time, exclusive")
    sim.add_argument("--horizons", default="0,12,60,300,3600", help="seconds, comma-separated")
    sim.add_argument("--grid-horizon", type=int, default=60, help="the markout a grid shows")
    sim.add_argument(
        "--burst", type=float, default=3.0, help="burst day: over this times the median volume"
    )
    sim.add_argument(
        "--rate-lookback",
        type=int,
        default=14_400,
        help="blocks before the first swap to search for the last Lido report",
    )
    sim.add_argument("--out", type=Path, help="parquet: a run's fills, or a grid's table")
    sim.set_defaults(func=_simulate)

    fast = commands.add_parser(
        "simulate-oracle",
        help="backtest the fast-oracle pool on a volatile pair, with a CEX price as the oracle",
    )
    fast.add_argument("swaps", type=Path, help="the pair's parquet from hs-swaps")
    fast.add_argument(
        "--prices", type=Path, nargs="+", required=True, help="reference prices (binance-klines)"
    )
    fast.add_argument("--chain", choices=sorted(CHAINS), default="base")
    fast.add_argument("--quote", default="USDC", help="the quote token's symbol")
    fast.add_argument("--block-ms", type=int, default=2_000, help="the chain's block time")
    fast.add_argument(
        "--blocks", type=Path, help="every block's time from hs-blocks, instead of a fixed grid"
    )
    fast.add_argument("--s-min", type=_floats, default=[2.0], help="half-spread floor, bp")
    fast.add_argument("--s0", type=float, default=0.0, help="spread base under the max, bp")
    fast.add_argument(
        "--k-sigma", type=_floats, default=[1.0], help="spread per sigma·√(age + latency)"
    )
    fast.add_argument(
        "--latency",
        type=_floats,
        default=[2.0],
        help="the pool's information lag behind the arbitrageur, s",
    )
    fast.add_argument(
        "--threshold", type=_floats, default=[0.25], help="the keeper pushes past this times s_min"
    )
    fast.add_argument("--heartbeat", type=float, default=10.0, help="or past this age, s")
    fast.add_argument("--max-age", type=float, default=60.0, help="swaps revert past it, s")
    fast.add_argument("--k-inv", type=_floats, default=[10.0], help="skew at a one-sided pool, bp")
    fast.add_argument(
        "--cap", type=_floats, default=[10.0], help="per-block outflow cap, %% of the reserve"
    )
    fast.add_argument("--steps", help="instead of --cap: 'β%%:δbp,...', e.g. '5:0,10:1'")
    fast.add_argument("--q-max", type=float, default=1.0, help="stop adding to an imbalance")
    fast.add_argument("--no-clamp", action="store_true", help="let the skew move both sides")
    fast.add_argument("--routing", type=_strings, default=["split"], help="'all' or 'split'")
    fast.add_argument(
        "--arb-cost", type=_floats, default=[1.0], help="the arbitrageur's unwind cost, bp"
    )
    fast.add_argument("--no-arb", action="store_true", help="leave the arbitrageur out")
    fast.add_argument(
        "--max-paid",
        type=float,
        help="keep only orders that paid at most this many bp over the reference a block before",
    )
    fast.add_argument(
        "--all-routers",
        action="store_true",
        help="also count routers that reach only their own venue (registry.VENUE_ROUTERS)",
    )
    fast.add_argument(
        "--extra-routers",
        type=_strings,
        default=[],
        help="more entry points to count as routed, e.g. unlabelled ones with many senders",
    )
    fast.add_argument(
        "--keeper-share", type=_floats, default=[0.15], help="φ, the keeper's part of the spread"
    )
    fast.add_argument("--push-gas", type=int, default=170_000, help="gas of one keeper push")
    fast.add_argument("--gas-gwei", type=float, default=0.006, help="base fee plus tip")
    fast.add_argument("--tvl-usd", type=float, default=250_000)
    fast.add_argument("--start", help="simulate from this ISO time (UTC unless an offset is given)")
    fast.add_argument("--end", help="simulate up to this ISO time, exclusive")
    fast.add_argument("--horizons", default="0,2,12,60,300", help="seconds, comma-separated")
    fast.add_argument("--grid-horizon", type=int, default=60, help="the markout a grid shows")
    fast.add_argument(
        "--burst", type=float, default=3.0, help="burst day: over this times the median volume"
    )
    fast.add_argument("--out", type=Path, help="parquet: a run's fills, or a grid's table")
    fast.set_defaults(func=_simulate_oracle)

    feed = commands.add_parser("chainlink", help="a Chainlink feed's answers on Base")
    feed.add_argument("feed", help=f"proxy address or one of: {', '.join(CHAINLINK_BASE)}")
    _add_window(feed)
    feed.add_argument("--rpc", default=rpc_default)
    feed.add_argument("--out", type=Path, help="optional parquet")
    feed.set_defaults(func=_chainlink_history)

    feeds = commands.add_parser("pyth-feeds", help="search Pyth price feeds on Hermes")
    feeds.add_argument("query", nargs="?", help="e.g. eurc; omit to list every feed")
    feeds.set_defaults(func=_pyth_feeds)

    pushed = commands.add_parser(
        "pyth-pushed", help="Pyth updates pushed on-chain recently: confidence and fork vectors"
    )
    pushed.add_argument("--rpc", default=rpc_default)
    pushed.add_argument("--blocks", type=int, default=200_000, help="how far back from the head")
    pushed.add_argument("--out", type=Path, help="optional parquet with one row per message")
    pushed.add_argument("--vectors", type=Path, help="directory for fork-test vectors")
    pushed.add_argument("--vector-feed", default="Crypto.ETH/USD", help="symbol or feed id")
    pushed.set_defaults(func=_pyth_pushed)

    pools = commands.add_parser("venues", help="find a pair's pools across Base venues")
    pools.add_argument("token_a", nargs="?", help="symbol from the registry or address")
    pools.add_argument("token_b", nargs="?")
    pools.add_argument("--candidates", action="store_true", help="every candidate pair")
    pools.add_argument("--rpc", default=rpc_default)
    pools.add_argument("--out", type=Path, help="optional parquet")
    pools.set_defaults(func=_venues)

    marks = commands.add_parser("markouts", help="volume-weighted LP markouts")
    marks.add_argument("--swaps", type=Path, required=True)
    marks.add_argument("--prices", type=Path, required=True)
    marks.add_argument("--horizons", default=",".join(map(str, markouts.DEFAULT_HORIZONS_S)))
    marks.add_argument("--blocks", type=Path, help="block times from hs-blocks, for Monad")
    marks.add_argument("--out", type=Path, help="optional parquet with per-swap markouts")
    marks.set_defaults(func=_markouts)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
