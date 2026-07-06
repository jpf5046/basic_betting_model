#!/usr/bin/env python3
"""Backtest CLI.

    python3 -m pipeline.backtest run --sport WNBA --season 2026
    python3 -m pipeline.backtest run --sport MLB --season 2024 --config tweaks.json
    python3 -m pipeline.backtest run --sport WNBA --season 2025 --from 2025-06-01 --to 2025-07-31

Replays the season slice through the model day by day (point-in-time
contexts, same code as the daily run), grades every published pick
against the actual finals — at consensus market prices when the odds
event map covers a game — and prints candidate vs factory-baseline
metrics. Results persist to data/backtests/<sport>/run_<id>.json plus a
full pick-log CSV.
"""

from __future__ import annotations

import argparse

from pipeline.backtest.runner import persist, run_backtest
from pipeline.features.context import ADAPTERS


def _fmt(value, pct: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.1f}%" if pct else str(value)


def _print_block(label: str, m: dict) -> None:
    o = m["overall"]
    print(f"\n{label}")
    print(f"  record {o['record']}   ROI {_fmt(o['roi'], pct=True)}   "
          f"P&L ${o['pnl']:+.2f}   picks {o['picks']}   "
          f"pass rate {_fmt(m['pass_rate'], pct=True) if m['pass_rate'] is not None else '—'}")
    print(f"  games {m['games']} ({m['no_prediction']} no-prediction)"
          + (f", voids {o['voids']}" if o["voids"] else "")
          + (f", pending {o['pending']}" if o["pending"] else ""))
    for name, table in (("by bet type", m["by_bet_type"]), ("by tier", m["by_tier"])):
        for key, b in table.items():
            print(f"    {name:<12} {key:<7} {b['record']:>9}  "
                  f"ROI {_fmt(b['roi'], pct=True)}  ${b['pnl']:+.2f}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.backtest", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="replay a season slice through a config")
    r.add_argument("--sport", required=True, choices=sorted(ADAPTERS))
    r.add_argument("--season", required=True)
    r.add_argument("--model", default="my_model")
    r.add_argument("--config", help="JSON params override (else factory defaults)")
    r.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    r.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    r.add_argument("--no-odds", action="store_true",
                   help="grade flat $100 even if matched odds exist")

    args = p.parse_args(argv)

    result = run_backtest(args.sport, args.season, args.model, args.config,
                          args.date_from, args.date_to, use_odds=not args.no_odds)
    label = "factory defaults" if result.is_factory else f"candidate ({args.config})"
    print(f"backtest {result.run_id}: {result.sport} {result.season} "
          f"{result.date_from} → {result.date_to}, model {result.model}")
    priced = result.priced_picks
    total_picks = result.metrics["overall"]["picks"]
    if total_picks:
        print(f"pricing: {priced}/{total_picks} picks at consensus market prices"
              + ("" if priced else " (no odds data — flat $100)"))
    _print_block(label, result.metrics)
    if result.baseline:
        _print_block("factory baseline (same slice)", result.baseline)
        d_pnl = result.metrics["overall"]["pnl"] - result.baseline["overall"]["pnl"]
        print(f"\ncandidate vs baseline P&L: ${d_pnl:+.2f}")
    json_path, picks_path = persist(result)
    print(f"\nsaved -> {json_path}\n         {picks_path}")


if __name__ == "__main__":
    main()
