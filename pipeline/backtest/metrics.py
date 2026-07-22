#!/usr/bin/env python3
"""Turn (prediction, actual) pairs into the numbers that tell us if the
model is any good — and, right now, exactly how under-dispersed it is.

Three families of metric:

  * Moneyline skill — accuracy, Brier, log-loss, and a calibration table
    (does "70% confident" win 70% of the time?).
  * Score realism — MAE of the predicted scores, and the headline
    diagnostic: predicted vs. actual spread of margins and totals. A model
    that calls every game a one-run game shows a tiny predicted margin
    stdev against a large actual one.
  * Totals skill — how the model's OVER/UNDER calls fare, both against its
    own fixed thresholds (which is where the all-OVER bias shows up) and
    against a neutral reference line, plus directional hit-rate.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from pipeline.backtest.runner import Result


@dataclass
class MoneylineMetrics:
    n: int
    accuracy: float          # share of games the predicted winner won
    brier: float             # mean (p_away - outcome_away)^2, lower better
    log_loss: float          # mean negative log-likelihood, lower better
    calibration: list[tuple] # (bin_lo, bin_hi, n, mean_pred, actual_rate)


@dataclass
class ScoreMetrics:
    n: int
    mae_away: float
    mae_home: float
    mae_total: float
    pred_margin_absmean: float   # mean |pred_spread|
    actual_margin_absmean: float
    pred_total_mean: float
    pred_total_std: float        # the dispersion smoking gun
    actual_total_mean: float
    actual_total_std: float


@dataclass
class TotalsMetrics:
    n_over: int
    n_under: int
    n_pass: int
    threshold_hit_rate: float | None   # win rate of fired OVER/UNDER calls
    ref_line: float
    directional_hit_rate: float        # over/under call vs ref_line, all games


def moneyline_metrics(results: list[Result], bins: int = 10) -> MoneylineMetrics:
    n = len(results)
    if n == 0:
        return MoneylineMetrics(0, 0.0, 0.0, 0.0, [])
    correct = brier = ll = 0.0
    for r in results:
        p = min(max(r.p_away, 1e-9), 1 - 1e-9)
        y = 1.0 if r.away_won else 0.0
        brier += (p - y) ** 2
        ll += -(y * math.log(p) + (1 - y) * math.log(1 - p))
        pred_away_wins = r.pred.winner_team_id == r.away_team_id
        if pred_away_wins == r.away_won:
            correct += 1

    # Calibration on the winner-relative probability (always >= 0.5): bin by
    # the model's stated confidence and see how often that side actually won.
    edges = [0.5 + 0.5 * i / bins for i in range(bins + 1)]
    table = []
    for lo, hi in zip(edges, edges[1:]):
        bucket = [r for r in results
                  if lo <= r.pred.win_prob < hi or (hi == edges[-1] and r.pred.win_prob == hi)]
        if not bucket:
            continue
        mean_pred = statistics.mean(r.pred.win_prob for r in bucket)
        hit = sum(1 for r in bucket
                  if (r.pred.winner_team_id == r.away_team_id) == r.away_won)
        table.append((round(lo, 3), round(hi, 3), len(bucket),
                      round(mean_pred, 3), round(hit / len(bucket), 3)))

    return MoneylineMetrics(n, round(correct / n, 4), round(brier / n, 4),
                            round(ll / n, 4), table)


def score_metrics(results: list[Result]) -> ScoreMetrics:
    n = len(results)
    if n == 0:
        return ScoreMetrics(0, *([0.0] * 9))
    mae_a = statistics.mean(abs(r.pred.pred_away - r.actual_away) for r in results)
    mae_h = statistics.mean(abs(r.pred.pred_home - r.actual_home) for r in results)
    mae_t = statistics.mean(abs(r.pred.pred_total - r.actual_total) for r in results)
    pred_margins = [abs(r.pred.pred_spread) for r in results]
    act_margins = [abs(r.actual_margin) for r in results]
    pred_totals = [r.pred.pred_total for r in results]
    act_totals = [r.actual_total for r in results]
    return ScoreMetrics(
        n=n,
        mae_away=round(mae_a, 3), mae_home=round(mae_h, 3), mae_total=round(mae_t, 3),
        pred_margin_absmean=round(statistics.mean(pred_margins), 3),
        actual_margin_absmean=round(statistics.mean(act_margins), 3),
        pred_total_mean=round(statistics.mean(pred_totals), 3),
        pred_total_std=round(statistics.pstdev(pred_totals), 3),
        actual_total_mean=round(statistics.mean(act_totals), 3),
        actual_total_std=round(statistics.pstdev(act_totals), 3),
    )


def totals_metrics(results: list[Result], params: dict,
                   ref_line: float | None = None) -> TotalsMetrics:
    """OVER/UNDER behaviour two ways: against the model's own fixed
    thresholds (the all-OVER symptom lives here), and directionally against
    a neutral reference line (defaults to the actual median total — the
    fairest 'did the model pick the right side' test without market lines)."""
    over_t = params["totals"]["over"]
    under_t = params["totals"]["under"]
    n_over = n_under = n_pass = 0
    fired_hits = fired_total = 0
    for r in results:
        pt = r.pred.pred_total
        if pt >= over_t:
            n_over += 1
            fired_total += 1
            fired_hits += 1 if r.actual_total > over_t else 0
        elif pt <= under_t:
            n_under += 1
            fired_total += 1
            fired_hits += 1 if r.actual_total < under_t else 0
        else:
            n_pass += 1

    if ref_line is None:
        ref_line = statistics.median(r.actual_total for r in results) if results else 0.0
    # Directional: exclude games landing exactly on the ref line (a push).
    dir_total = dir_hits = 0
    for r in results:
        if r.actual_total == ref_line:
            continue
        dir_total += 1
        picks_over = r.pred.pred_total >= ref_line
        actual_over = r.actual_total > ref_line
        dir_hits += 1 if picks_over == actual_over else 0

    return TotalsMetrics(
        n_over=n_over, n_under=n_under, n_pass=n_pass,
        threshold_hit_rate=round(fired_hits / fired_total, 4) if fired_total else None,
        ref_line=round(ref_line, 2),
        directional_hit_rate=round(dir_hits / dir_total, 4) if dir_total else 0.0,
    )


def evaluate(results: list[Result], params: dict) -> dict:
    """All three metric families, for one backtest run."""
    return {
        "moneyline": moneyline_metrics(results),
        "score": score_metrics(results),
        "totals": totals_metrics(results, params),
    }
