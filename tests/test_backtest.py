"""Offline tests for the backtest harness (pipeline/backtest/).

Builds a tiny synthetic season in memory — no CSVs, no network.
From the repo root:  python3 -m unittest tests.test_backtest -v
"""
import unittest

from pipeline.backtest.metrics import evaluate, moneyline_metrics, score_metrics
from pipeline.backtest.runner import Result
from pipeline.models.base import Prediction


def make_result(game_id, actual_away, actual_home, pred_away, pred_home,
                win_prob, winner_is_away):
    away, home = "A", "H"
    pred = Prediction(
        game_id=game_id, sport="MLB", date="2026-05-01",
        away_team_id=away, home_team_id=home,
        pred_away=pred_away, pred_home=pred_home,
        disp_away=round(pred_away), disp_home=round(pred_home),
        pred_total=pred_away + pred_home, pred_spread=pred_away - pred_home,
        win_prob=win_prob, winner_team_id=away if winner_is_away else home,
        pred_confidence="MEDIUM",
    )
    return Result(game_id=game_id, date="2026-05-01", away_team_id=away,
                  home_team_id=home, actual_away=actual_away,
                  actual_home=actual_home, pred=pred)


class TestResult(unittest.TestCase):
    def test_derived_fields(self):
        r = make_result("g1", 6, 2, 5.0, 4.0, 0.60, True)
        self.assertEqual(r.actual_total, 8)
        self.assertEqual(r.actual_margin, 4)
        self.assertTrue(r.away_won)
        self.assertAlmostEqual(r.p_away, 0.60)  # winner is away

    def test_p_away_flips_for_home_winner(self):
        r = make_result("g2", 2, 6, 4.0, 5.0, 0.60, False)
        self.assertAlmostEqual(r.p_away, 0.40)  # winner is home => p_away = 1-0.6


class TestMoneylineMetrics(unittest.TestCase):
    def test_perfect_calls_full_accuracy(self):
        results = [
            make_result("g1", 6, 2, 5.0, 4.0, 0.60, True),   # away won, picked away
            make_result("g2", 2, 6, 4.0, 5.0, 0.60, False),  # home won, picked home
        ]
        m = moneyline_metrics(results)
        self.assertEqual(m.accuracy, 1.0)

    def test_wrong_calls_zero_accuracy(self):
        results = [
            make_result("g1", 2, 6, 5.0, 4.0, 0.60, True),   # home won, picked away
        ]
        self.assertEqual(moneyline_metrics(results).accuracy, 0.0)


class TestScoreMetrics(unittest.TestCase):
    def test_dispersion_gap_is_measured(self):
        # Two predictions both ~1-run margins; actuals are blowouts. The
        # metric must show predicted |margin| << actual |margin|.
        results = [
            make_result("g1", 9, 1, 5.2, 4.8, 0.55, True),
            make_result("g2", 1, 9, 4.8, 5.2, 0.55, False),
        ]
        sc = score_metrics(results)
        self.assertLess(sc.pred_margin_absmean, 1.0)
        self.assertEqual(sc.actual_margin_absmean, 8.0)


class TestEvaluate(unittest.TestCase):
    def test_returns_all_three_families(self):
        results = [make_result("g1", 6, 2, 5.0, 4.0, 0.60, True)]
        params = {"totals": {"over": 9.5, "under": 8.5}}
        out = evaluate(results, params)
        self.assertIn("moneyline", out)
        self.assertIn("score", out)
        self.assertIn("totals", out)


if __name__ == "__main__":
    unittest.main()
