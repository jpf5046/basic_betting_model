#!/usr/bin/env python3
"""Replay a model over historical finals and collect (prediction, actual) pairs.

`run_backtest` groups the eval games by date, builds one point-in-time
FeatureContext per date (so all of a day's games share it), predicts each
game, and pairs the prediction with the final score. `evaluate` turns those
pairs into the metrics we care about — see metrics.py.

Games where the model returns None (insufficient data — e.g. opening week
with no season baseline yet) are counted separately and excluded from the
skill metrics; sitting out is a legitimate model output, not a miss.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pipeline.features.context import FeatureContext
from pipeline.ingest import core
from pipeline.ingest.core import Game
from pipeline.models.base import Prediction
from pipeline.models.registry import get_model


@dataclass
class Result:
    """One evaluated game: what the model said vs. what happened."""

    game_id: str
    date: str
    away_team_id: str
    home_team_id: str
    actual_away: int
    actual_home: int
    pred: Prediction

    @property
    def actual_total(self) -> int:
        return self.actual_away + self.actual_home

    @property
    def actual_margin(self) -> int:
        """away minus home (matches pred_spread's sign convention)."""
        return self.actual_away - self.actual_home

    @property
    def away_won(self) -> bool:
        return self.actual_away > self.actual_home

    @property
    def p_away(self) -> float:
        """The model's P(away wins), recovered from the winner-relative
        win_prob (which is always >= 0.5 for the predicted winner)."""
        if self.pred.winner_team_id == self.away_team_id:
            return self.pred.win_prob
        return 1.0 - self.pred.win_prob


def run_backtest(sport: str, games: list[Game], params: dict,
                 start: str | None = None, end: str | None = None,
                 model_name: str = "my_model") -> tuple[list[Result], int]:
    """Return (results, n_couldnt_run) for every completed regular-season
    game in [start, end]. One FeatureContext is built per game date."""
    model = get_model(model_name)
    finals = core.completed_regular_season(games)
    if start:
        finals = [g for g in finals if g.date >= start]
    if end:
        finals = [g for g in finals if g.date <= end]

    by_date: dict[str, list[Game]] = {}
    for g in finals:
        by_date.setdefault(g.date, []).append(g)

    results: list[Result] = []
    couldnt_run = 0
    for d in sorted(by_date):
        ctx = FeatureContext(sport, date.fromisoformat(d), games)
        for g in by_date[d]:
            pred = model.predict(ctx, g, params)
            if pred is None:
                couldnt_run += 1
                continue
            results.append(Result(
                game_id=g.game_id, date=g.date,
                away_team_id=g.away_team_id, home_team_id=g.home_team_id,
                actual_away=int(g.away_score), actual_home=int(g.home_score),
                pred=pred,
            ))
    return results, couldnt_run
