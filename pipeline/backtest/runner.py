#!/usr/bin/env python3
"""Backtest runner — PLAN.md §6 / build-order item 9.

Replays a season (or date slice) through a model + params exactly the way
the daily pipeline would have lived it: a fresh point-in-time context per
date, predictions from the model, picks from the pick policy, grading
against the actual finals — at consensus market prices whenever the odds
event map covers a game (real ROI), flat $100 otherwise.

Per PLAN §6, a run with non-factory params ALWAYS also replays the
factory-default baseline over the same slice, so every result comes with
its "vs baseline" comparison built in. Runs are deterministic: run_id is
a hash of (model, sport, slice, params), and results persist to
data/backtests/<sport>/ as JSON metrics + a full pick-log CSV.
"""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date as date_cls, datetime, timezone
from pathlib import Path

import pipeline.models  # noqa: F401  (registers models + features)
from pipeline.backtest.metrics import compute_metrics
from pipeline.features.context import FeatureContext
from pipeline.frame import load_games
from pipeline.grading.grader import GRADE_FIELDS, Grade, grade_pick
from pipeline.ingest.core import REPO_ROOT, Game
from pipeline.models.config import FACTORY_DEFAULTS, load_params
from pipeline.models.picks import make_picks
from pipeline.models.registry import get_model

BACKTESTS_DIR = REPO_ROOT / "data" / "backtests"

# Game types worth betting on — preseason/exhibition/All-Star slates are
# never replayed (the daily pipeline wouldn't pick them either).
BETTABLE_TYPES = ("regular", "playoffs", "playin", "nbacup_final")


@dataclass
class BacktestResult:
    run_id: str
    sport: str
    season: str
    model: str
    date_from: str
    date_to: str
    params: dict
    is_factory: bool
    priced_picks: int          # picks graded at market prices
    metrics: dict
    baseline: dict | None      # factory metrics over the same slice (None if is_factory)
    grades: list[Grade] = field(default_factory=list, repr=False)


def make_run_id(sport: str, season: str, model: str, date_from: str,
                date_to: str, params: dict) -> str:
    blob = json.dumps({"sport": sport, "season": season, "model": model,
                       "from": date_from, "to": date_to, "params": params},
                      sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def _make_odds_lookup(sport: str):
    """Same consensus join grading uses; None when no odds data exists."""
    from pipeline.odds.match import load_event_map
    from pipeline.odds.store import consensus_h2h, consensus_total, load_snapshot_rows

    emap = load_event_map(sport)
    rows = load_snapshot_rows(sport)
    if not emap or not rows:
        return None
    by_game = {r["game_id"]: r["event_id"] for r in emap.values()}

    def lookup(pick: dict):
        event_id = by_game.get(pick["game_id"])
        if not event_id:
            return None, None
        if pick["bet_type"] == "ML":
            h2h = consensus_h2h(rows, event_id, pick["selection"])
            return (h2h[0], None) if h2h else (None, None)
        total = consensus_total(rows, event_id, pick["selection"])
        return (total[1], total[0]) if total else (None, None)

    return lookup


def _replay(sport: str, games: list[Game], in_range: list[Game], model,
            params: dict, odds_lookup) -> tuple[list[Grade], int, int, int]:
    """One pass over the slice: (grades, games_seen, predicted, no_pick)."""
    grades: list[Grade] = []
    games_seen = predicted = no_pick = 0
    for day in sorted({g.date for g in in_range}):
        ctx = FeatureContext(sport, date_cls.fromisoformat(day), games)
        for game in (g for g in in_range if g.date == day):
            games_seen += 1
            pred = model.predict(ctx, game, params)
            if pred is None:
                continue
            predicted += 1
            picks = make_picks(pred, params)
            if not picks:
                no_pick += 1
                continue
            for pick in picks:
                row = {**vars(pick)}
                price, point = odds_lookup(row) if odds_lookup else (None, None)
                grades.append(grade_pick(row, game, price, point))
    return grades, games_seen, predicted, no_pick


def run_backtest(sport: str, season: str, model_name: str = "my_model",
                 config_path: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 games: list[Game] | None = None,
                 use_odds: bool = True) -> BacktestResult:
    games = load_games(sport, season, games)
    in_range = [
        g for g in games
        if g.status == "final" and g.away_score and g.home_score
        and g.season_type in BETTABLE_TYPES
        and (date_from is None or g.date >= date_from)
        and (date_to is None or g.date <= date_to)
    ]
    model = get_model(model_name)
    params = load_params(sport, config_path)
    is_factory = params == FACTORY_DEFAULTS[sport]
    odds_lookup = _make_odds_lookup(sport) if use_odds else None

    grades, seen, predicted, no_pick = _replay(
        sport, games, in_range, model, params, odds_lookup)
    metrics = compute_metrics(grades, seen, predicted, no_pick)

    baseline = None
    if not is_factory:
        b_grades, b_seen, b_pred, b_no_pick = _replay(
            sport, games, in_range, model, load_params(sport), odds_lookup)
        baseline = compute_metrics(b_grades, b_seen, b_pred, b_no_pick)

    span = sorted({g.date for g in in_range}) or ["", ""]
    return BacktestResult(
        run_id=make_run_id(sport, season, model_name,
                           date_from or span[0], date_to or span[-1], params),
        sport=sport, season=season, model=model_name,
        date_from=date_from or span[0], date_to=date_to or span[-1],
        params=params, is_factory=is_factory,
        priced_picks=sum(1 for g in grades if g.price_american),
        metrics=metrics, baseline=baseline, grades=grades,
    )


def persist(result: BacktestResult) -> tuple[Path, Path]:
    out_dir = BACKTESTS_DIR / result.sport.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {k: v for k, v in asdict(result).items() if k != "grades"}
    meta["created_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json_path = out_dir / f"run_{result.run_id}.json"
    json_path.write_text(json.dumps(meta, indent=2))
    picks_path = out_dir / f"run_{result.run_id}_picks.csv"
    with open(picks_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GRADE_FIELDS)
        w.writeheader()
        w.writerows(vars(g) for g in result.grades)
    return json_path, picks_path
