"""Offline tests for the canonical frame (pipeline/frame) and the
backtester (pipeline/backtest), built on the WNBA fixture feed.

From the repo root:  python3 -m unittest tests.test_frame_backtest -v
"""
import json
import unittest
from pathlib import Path

from pipeline.backtest.metrics import bucket, compute_metrics
from pipeline.backtest.runner import make_run_id, run_backtest
from pipeline.frame import build_frame, outcome_fields
from pipeline.ingest import wnba

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_games():
    doc = json.loads((FIXTURES / "wnba_schedule_sample.json").read_text())
    return wnba.parse_games(doc, wnba.load_team_map())


class TestCanonicalFrame(unittest.TestCase):
    def setUp(self):
        self.games = fixture_games()
        self.columns, self.rows = build_frame("WNBA", "2026", games=self.games)
        self.by_id = {r["game_id"]: r for r in self.rows}

    def test_one_row_per_game(self):
        self.assertEqual(len(self.rows), len(self.games))
        for col in ("game_id", "away_season_scoring_spg", "final_away",
                    "actual_total", "winner_team_id"):
            self.assertIn(col, self.columns)

    def test_features_are_as_of_game_date(self):
        # 06-03 game: only the 06-01 game preceded it -> NYL gp = 1.
        early = self.by_id["1022600108"]
        self.assertEqual(early["home_games_played"], 1)
        # 07-05 game: NYL has 4 completed games by then.
        late = self.by_id["1022600201"]
        self.assertEqual(late["away_games_played"], 4)

    def test_outcomes_on_finals_blank_on_scheduled(self):
        final = self.by_id["1022600143"]  # NYL 87 @ LVA 89
        self.assertEqual((final["final_away"], final["final_home"]), (87, 89))
        self.assertEqual(final["actual_total"], 176)
        self.assertEqual(final["actual_margin"], -2)
        self.assertEqual(final["winner_team_id"], "wnba-lva")
        scheduled = self.by_id["1022600201"]
        self.assertEqual(scheduled["final_away"], "")
        self.assertEqual(scheduled["winner_team_id"], "")

    def test_date_slice(self):
        _, rows = build_frame("WNBA", "2026", date_from="2026-06-05",
                              date_to="2026-06-11", games=self.games)
        self.assertEqual({r["game_id"] for r in rows},
                         {"1022600115", "1022600122", "1022600129", "1022600136"})

    def test_outcome_fields_never_guess(self):
        g = fixture_games()[0]
        g.status = "postponed"
        self.assertEqual(outcome_fields(g)["final_away"], "")


class TestMetrics(unittest.TestCase):
    def test_bucket_roi(self):
        from tests.test_grading import make_game, make_pick
        from pipeline.grading.grader import grade_pick
        games = {"a": make_game(game_id="a", away=5, home=3),
                 "b": make_game(game_id="b", away=2, home=6)}
        grades = [grade_pick(make_pick(game_id="a", selection="mlb-nyy"), games["a"]),
                  grade_pick(make_pick(game_id="b", selection="mlb-nyy"), games["b"])]
        b = bucket(grades)
        self.assertEqual(b["record"], "1-1-0")
        self.assertEqual(b["staked"], 200)
        self.assertEqual(b["pnl"], 0)
        self.assertEqual(b["roi"], 0.0)

    def test_cumulative_series_ends_at_total(self):
        from tests.test_grading import make_game, make_pick
        from pipeline.grading.grader import grade_pick
        g1 = make_game(game_id="a", away=5, home=3)
        grades = [grade_pick(make_pick(game_id="a", selection="mlb-nyy"), g1)]
        m = compute_metrics(grades, games_seen=1, games_predicted=1, games_no_pick=0)
        self.assertEqual(m["cumulative_pnl"][-1][1], m["overall"]["pnl"])


class TestBacktester(unittest.TestCase):
    def setUp(self):
        self.games = fixture_games()
        self.result = run_backtest("WNBA", "2026", games=self.games, use_odds=False)

    def test_replays_only_finals(self):
        # Fixture: 8 regular-season finals; scheduled/preseason never replayed.
        self.assertEqual(self.result.metrics["games"], 8)

    def test_early_season_no_prediction_counted(self):
        # 06-01 NYL@IND: nothing completed before it -> model can't run.
        self.assertGreaterEqual(self.result.metrics["no_prediction"], 1)
        self.assertEqual(self.result.metrics["predicted"],
                         self.result.metrics["games"] - self.result.metrics["no_prediction"])

    def test_grades_are_final_outcomes_only(self):
        o = self.result.metrics["overall"]
        self.assertEqual(o["pending"], 0)
        self.assertEqual(o["voids"], 0)
        self.assertEqual(o["picks"], o["wins"] + o["losses"] + o["pushes"])
        self.assertGreater(o["picks"], 0)

    def test_factory_run_has_no_duplicate_baseline(self):
        self.assertTrue(self.result.is_factory)
        self.assertIsNone(self.result.baseline)

    def test_deterministic_run_id_and_metrics(self):
        again = run_backtest("WNBA", "2026", games=fixture_games(), use_odds=False)
        self.assertEqual(again.run_id, self.result.run_id)
        self.assertEqual(again.metrics, self.result.metrics)

    def test_run_id_changes_with_slice(self):
        other = make_run_id("WNBA", "2026", "my_model", "2026-06-01",
                            "2026-06-05", self.result.params)
        self.assertNotEqual(other, self.result.run_id)

    def test_known_pick_graded_correctly(self):
        # 06-03 IND@NYL: with only 06-01 in history the model heavily
        # favors NYL (STRONG ML) and predicts a huge total (OVER 168).
        # Actual: NYL won 85-75, total 160 -> ML WIN, OVER LOSS.
        day = [g for g in self.result.grades if g.game_id == "1022600108"]
        ml = next(g for g in day if g.bet_type == "ML")
        total = next(g for g in day if g.bet_type == "TOTAL")
        self.assertEqual((ml.selection, ml.result, ml.pnl), ("wnba-nyl", "WIN", 100))
        self.assertEqual((total.selection, total.result, total.pnl),
                         ("OVER", "LOSS", -100))

    def test_odds_lookup_changes_payouts(self):
        result = run_backtest("WNBA", "2026", games=fixture_games(), use_odds=False)
        flat_pnl = result.metrics["overall"]["pnl"]
        # Re-run with a fake consensus of -110 on everything.
        from pipeline.backtest import runner
        original = runner._make_odds_lookup
        runner._make_odds_lookup = lambda sport: (lambda pick: (-110, None))
        try:
            priced = run_backtest("WNBA", "2026", games=fixture_games(), use_odds=True)
        finally:
            runner._make_odds_lookup = original
        self.assertEqual(priced.priced_picks, priced.metrics["overall"]["picks"])
        # Same record, smaller winnings: every WIN pays 90.91 instead of 100.
        self.assertEqual(priced.metrics["overall"]["record"],
                         result.metrics["overall"]["record"])
        self.assertLess(priced.metrics["overall"]["pnl"], flat_pnl)


if __name__ == "__main__":
    unittest.main()
