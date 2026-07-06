#!/usr/bin/env python3
"""Model CLI.

    python3 -m pipeline.models list
    python3 -m pipeline.models validate-config path.json --sport WNBA
    python3 -m pipeline.models predict --sport WNBA [--date YYYY-MM-DD]
                                       [--model my_model] [--config path.json]

`predict` loads the sport's games CSV (run the adapter's fetch first),
builds the point-in-time context as of --date, runs the model on that
date's slate, and prints the pick sheet. Two CSVs land under
data/predictions/<sport>/: predictions_<date>.csv (every game, including
TOSS-UP/PASS and couldn't-run — sitting out is an output) and
picks_<date>.csv (published picks only).
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import fields
from datetime import date, datetime
from pathlib import Path

import pipeline.models  # noqa: F401  (auto-registers models + features)
from pipeline.features.context import ADAPTERS, FeatureContext, default_season
from pipeline.ingest import core
from pipeline.ingest.core import EASTERN, REPO_ROOT, todays_games
from pipeline.models.base import Pick, Prediction
from pipeline.models.config import load_params, validate_params
from pipeline.models.picks import make_picks, moneyline_lean, totals_lean
from pipeline.models.registry import all_models, get_model

PREDICTIONS_DIR = REPO_ROOT / "data" / "predictions"


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)
    return path


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.models", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show registered models")

    v = sub.add_parser("validate-config", help="validate a params JSON file")
    v.add_argument("path")
    v.add_argument("--sport", required=True, choices=sorted(ADAPTERS))

    pr = sub.add_parser("predict", help="run a model over one date's slate")
    pr.add_argument("--sport", required=True, choices=sorted(ADAPTERS))
    pr.add_argument("--date", help="YYYY-MM-DD (default: today, US/Eastern)")
    pr.add_argument("--season", help="override the season key of the games CSV")
    pr.add_argument("--model", default="my_model")
    pr.add_argument("--config", help="JSON params override file (else factory defaults)")

    args = p.parse_args(argv)

    if args.cmd == "list":
        for m in all_models():
            feats = ", ".join(m.required_features)
            print(f"{m.name:<12} consumes: {feats}")
        return

    if args.cmd == "validate-config":
        params = load_params(args.sport, args.path)
        validate_params(args.sport, params)
        print(f"OK: {args.path} valid for {args.sport}")
        return

    # ---- predict ----
    on = date.fromisoformat(args.date) if args.date else datetime.now(EASTERN).date()
    season = args.season or default_season(args.sport, on)
    games = core.read_games_csv(
        ADAPTERS[args.sport].games_csv_path(season),
        f"python3 -m pipeline.ingest.{args.sport.lower()} fetch",
    )
    slate = todays_games(games, on)
    if not slate:
        sys.exit(f"no {args.sport} games on {on.isoformat()} — nothing to predict")

    ctx = FeatureContext(args.sport, on, games)
    model = get_model(args.model)
    params = load_params(args.sport, args.config)
    abbrev = {t.team_id: t.abbrev for t in (ctx.team(g.away_team_id) for g in slate)}
    abbrev.update({t.team_id: t.abbrev for t in (ctx.team(g.home_team_id) for g in slate)})

    pred_rows: list[dict] = []
    pick_rows: list[dict] = []
    for game in slate:
        pred = model.predict(ctx, game, params)
        if pred is None:
            print(f"{game.date}  {game.away_abbrev:>4} @ {game.home_abbrev:<4} "
                  f"NO PREDICTION (insufficient data)")
            pred_rows.append({"game_id": game.game_id, "sport": args.sport,
                              "date": game.date, "away_team_id": game.away_team_id,
                              "home_team_id": game.home_team_id, "status": "no_prediction"})
            continue

        ml = moneyline_lean(pred, params)
        total = totals_lean(pred, params)
        picks = make_picks(pred, params)
        pick_rows.extend(vars(pk) for pk in picks)

        row = {"status": "ok", "ml_lean": ml, "total_lean": total, **vars(pred)}
        row["method_details"] = ""  # keep the CSV flat; details live in-memory
        pred_rows.append(row)

        winner_ab = abbrev[pred.winner_team_id]
        ml_txt = f"ML: {winner_ab} {ml}" if ml != "TOSS-UP" else "ML: TOSS-UP"
        tot_txt = (f"TOTAL: {total} {params['totals']['over' if total == 'OVER' else 'under']:g}"
                   if total != "PASS" else "TOTAL: PASS")
        print(f"{pred.date}  {game.away_abbrev:>4} @ {game.home_abbrev:<4} "
              f"pred {pred.disp_away}-{pred.disp_home} ({winner_ab} p={pred.win_prob:.2f}, "
              f"{pred.pred_confidence}) | {ml_txt} | {tot_txt}")

    out_dir = PREDICTIONS_DIR / args.sport.lower()
    pred_cols = ["status", "ml_lean", "total_lean"] + [f.name for f in fields(Prediction)]
    pred_path = _write_csv(out_dir / f"predictions_{on.isoformat()}.csv",
                           pred_rows, pred_cols)
    pick_path = _write_csv(out_dir / f"picks_{on.isoformat()}.csv",
                           pick_rows, [f.name for f in fields(Pick)])
    print(f"\n{len(pred_rows)} predictions -> {pred_path}")
    print(f"{len(pick_rows)} published picks -> {pick_path}")


if __name__ == "__main__":
    main()
