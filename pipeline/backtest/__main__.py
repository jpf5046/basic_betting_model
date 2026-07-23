#!/usr/bin/env python3
"""Backtest CLI — replay a model over history and print a scorecard.

    python3 -m pipeline.backtest --sport MLB
    python3 -m pipeline.backtest --sport MLB --start 2026-05-01 --end 2026-06-30
    python3 -m pipeline.backtest --sport MLB --config tuned.json

Reads the sport's games CSV (run the ingest adapter's `fetch` first),
replays `--model` over every completed regular-season game in the window
using point-in-time contexts, and prints moneyline / score-realism /
totals metrics. With --config it uses tuned params so a change can be A/B'd
against the factory defaults in one line.
"""

from __future__ import annotations

import argparse

import pipeline.models  # noqa: F401  (registers models + features)
from pipeline.backtest.metrics import evaluate
from pipeline.backtest.runner import run_backtest
from pipeline.features.context import ADAPTERS, default_season
from pipeline.ingest import core
from pipeline.models.config import load_params


def _print_report(sport: str, n_eval: int, couldnt_run: int, m: dict) -> None:
    ml, sc, tot = m["moneyline"], m["score"], m["totals"]
    print(f"\n=== {sport} backtest — {n_eval} games graded "
          f"({couldnt_run} couldn't-run, excluded) ===")

    print("\n-- Moneyline --")
    print(f"  accuracy : {ml.accuracy:.1%}")
    print(f"  Brier    : {ml.brier:.4f}   (0.25 = coin flip, lower better)")
    print(f"  log-loss : {ml.log_loss:.4f}   (0.693 = coin flip, lower better)")
    print("  calibration (stated winner-prob vs. actual win rate):")
    print(f"    {'bin':>13}  {'n':>4}  {'mean_p':>7}  {'actual':>7}")
    for lo, hi, nb, mp, ar in ml.calibration:
        flag = "  <- overconfident" if mp - ar > 0.05 else ""
        print(f"    [{lo:.2f},{hi:.2f})  {nb:>4}  {mp:>7.3f}  {ar:>7.3f}{flag}")

    print("\n-- Score realism --")
    print(f"  MAE away/home/total : {sc.mae_away:.2f} / {sc.mae_home:.2f} / {sc.mae_total:.2f}")
    print(f"  |margin| pred vs actual : {sc.pred_margin_absmean:.2f} vs {sc.actual_margin_absmean:.2f}")
    print(f"  total mean pred vs actual: {sc.pred_total_mean:.2f} vs {sc.actual_total_mean:.2f}")
    print(f"  total STDEV pred vs actual: {sc.pred_total_std:.2f} vs {sc.actual_total_std:.2f}"
          "   <- dispersion gap")

    print("\n-- Totals --")
    print(f"  fired: {tot.n_over} OVER / {tot.n_under} UNDER / {tot.n_pass} PASS")
    hr = f"{tot.threshold_hit_rate:.1%}" if tot.threshold_hit_rate is not None else "n/a"
    print(f"  threshold hit-rate (fired bets): {hr}")
    print(f"  directional hit-rate vs ref line {tot.ref_line:g}: {tot.directional_hit_rate:.1%}")
    print()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.backtest", description=__doc__)
    p.add_argument("--sport", required=True, choices=sorted(ADAPTERS))
    p.add_argument("--start", help="YYYY-MM-DD (inclusive); default: season start")
    p.add_argument("--end", help="YYYY-MM-DD (inclusive); default: season end")
    p.add_argument("--season", help="override the season key of the games CSV")
    p.add_argument("--model", default="my_model")
    p.add_argument("--config", help="JSON params override file (else factory defaults)")
    args = p.parse_args(argv)

    season = args.season or default_season(args.sport, __import__("datetime").date.today())
    games = core.read_games_csv(
        ADAPTERS[args.sport].games_csv_path(season),
        f"python3 -m pipeline.ingest.{args.sport.lower()} fetch",
    )
    params = load_params(args.sport, args.config)
    results, couldnt = run_backtest(args.sport, games, params,
                                    start=args.start, end=args.end,
                                    model_name=args.model)
    if not results:
        raise SystemExit("no games in range to backtest")
    _print_report(args.sport, len(results), couldnt, evaluate(results, params))


if __name__ == "__main__":
    main()
