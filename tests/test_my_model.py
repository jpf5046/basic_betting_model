"""Offline tests for My Model v1 (pipeline/models/).

Built on the same WNBA fixture as the ingester/feature tests. The
NYL @ LVA expectations below are hand-computed from my_model.md §2-§5
with the WNBA factory params (home_adv 2.5, form 0.85/0.30, k 0.15):

  NYL (away): spg 88.5, sapg 78.5, L10 3-1 (.75), common wRF 89.0 wRA 75.0
  LVA (home): spg 93.0, sapg 85.75, L10 4-0 (1.0), common wRF 94.3333 wRA 85.3333
  H2H: one meeting, NYL 87-89 LVA

  M1 base:   away (88.5+85.75)/2 = 87.125     home (93.0+78.5)/2  = 85.75
  M2 common: away (89+85.3333)/2 = 87.1667    home (94.3333+75)/2 = 84.6667
  M3 form:   mult A 1.075, H 1.15 -> away 87.125*1.075/1.15 = 81.4429
                                     home 85.75*1.15/1.075  = 91.7326
  M4 home:   away 87.125-1.25 = 85.875        home 85.75+2.5 = 88.25
  M5 h2h:    away 87, home 89
  Blend .30/.35/.20/.10/.05: pred_away 85.8719, pred_home 86.9798
  total 172.8518, spread -1.1079, P(away) = 0.4587 -> LVA wins p 0.5413
  Gates: 0.5413 < 0.55 -> ML TOSS-UP; total 172.85 >= 168 -> OVER
  Evidence: 2 common opps (+1), h2h (+1), gp 4 (<20) -> 2 points -> LOW

From the repo root:  python3 -m unittest tests.test_my_model -v
"""
import json
import unittest
from datetime import date
from pathlib import Path

import pipeline.models  # noqa: F401  (auto-registers models + features)
from pipeline.features.context import FeatureContext
from pipeline.ingest import wnba
from pipeline.ingest.core import todays_games
from pipeline.models.config import ConfigError, FACTORY_DEFAULTS, load_params, validate_params
from pipeline.models.picks import make_picks, moneyline_lean, totals_lean
from pipeline.models.registry import get_model

FIXTURES = Path(__file__).parent / "fixtures"


def wnba_slate(as_of: date):
    doc = json.loads((FIXTURES / "wnba_schedule_sample.json").read_text())
    games = wnba.parse_games(doc, wnba.load_team_map())
    ctx = FeatureContext("WNBA", as_of, games)
    return ctx, games


class TestConfig(unittest.TestCase):
    def test_factory_defaults_all_valid(self):
        for sport, params in FACTORY_DEFAULTS.items():
            validate_params(sport, params)  # must not raise

    def test_bad_weights_rejected(self):
        params = load_params("WNBA")
        params["weights"]["season"] = 0.50  # sum now 1.20
        with self.assertRaises(ConfigError):
            validate_params("WNBA", params)

    def test_inverted_gates_rejected(self):
        params = load_params("MLB")
        params["ml_gates"] = {"strong": 0.55, "lean": 0.60}
        with self.assertRaises(ConfigError):
            validate_params("MLB", params)

    def test_override_file_deep_merges(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"k": 0.25, "totals": {"over": 170.0}}, f)
        params = load_params("WNBA", f.name)
        self.assertEqual(params["k"], 0.25)
        self.assertEqual(params["totals"]["over"], 170.0)
        self.assertEqual(params["totals"]["under"], 158.0)  # untouched
        self.assertEqual(params["weights"]["common"], 0.35)  # untouched


class TestMyModelPrediction(unittest.TestCase):
    def setUp(self):
        self.ctx, games = wnba_slate(date(2026, 7, 5))
        slate = todays_games(games, date(2026, 7, 5))
        self.nyl_lva = next(g for g in slate if g.away_abbrev == "NYL")
        self.phx_sea = next(g for g in slate if g.away_abbrev == "PHX")
        self.model = get_model("my_model")
        self.params = load_params("WNBA")
        self.pred = self.model.predict(self.ctx, self.nyl_lva, self.params)

    def test_blended_scores_match_hand_math(self):
        self.assertAlmostEqual(self.pred.pred_away, 85.8719, places=3)
        self.assertAlmostEqual(self.pred.pred_home, 86.9798, places=3)
        self.assertAlmostEqual(self.pred.pred_total, 172.8518, places=3)
        self.assertAlmostEqual(self.pred.pred_spread, -1.1079, places=3)

    def test_method_estimates_recorded(self):
        est = self.pred.method_details["estimates"]
        self.assertAlmostEqual(est["season"][0], 87.125, places=3)
        self.assertAlmostEqual(est["common"][1], 84.6667, places=3)
        self.assertAlmostEqual(est["form"][0], 81.4429, places=3)
        self.assertEqual(est["home"], (85.875, 88.25))
        self.assertEqual(est["h2h"], (87.0, 89.0))
        # all five methods had data -> factory weights unchanged
        self.assertEqual(self.pred.method_details["weights_used"]["common"], 0.35)

    def test_win_probability_and_winner(self):
        self.assertEqual(self.pred.winner_team_id, "wnba-lva")
        self.assertEqual(self.pred.win_prob, 0.54)

    def test_confidence_label(self):
        self.assertEqual(self.pred.pred_confidence, "LOW")

    def test_picks_tossup_ml_but_over_total(self):
        self.assertEqual(moneyline_lean(self.pred, self.params), "TOSS-UP")
        self.assertEqual(totals_lean(self.pred, self.params), "OVER")
        picks = make_picks(self.pred, self.params)
        self.assertEqual(len(picks), 1)
        self.assertEqual((picks[0].bet_type, picks[0].selection, picks[0].line),
                         ("TOTAL", "OVER", "168"))

    def test_no_prediction_without_season_data(self):
        # SEA has no completed games in the fixture -> model can't run.
        self.assertIsNone(self.model.predict(self.ctx, self.phx_sea, self.params))


class TestRenormalization(unittest.TestCase):
    def test_common_dropped_and_weights_renormalized(self):
        # As of 06-03 only NYL@IND (06-01) is final: the 06-03 IND@NYL game
        # has no common opponents (they've only played each other), so
        # method 2 drops and .30/.20/.10/.05 renormalize over 0.65.
        ctx, games = wnba_slate(date(2026, 6, 3))
        game = next(g for g in games if g.game_id == "1022600108")
        pred = get_model("my_model").predict(ctx, game, load_params("WNBA"))
        w = pred.method_details["weights_used"]
        self.assertEqual(w["common"], 0.0)
        self.assertAlmostEqual(w["season"], 0.30 / 0.65, places=4)
        self.assertAlmostEqual(w["h2h"], 0.05 / 0.65, places=4)
        # stored weights are rounded to 6dp, so the sum is exact to ~1e-5
        self.assertAlmostEqual(sum(w.values()), 1.0, places=4)
        # away IND: base 80; form 80*0.85/1.15; home 78.75; h2h 80
        expected_away = (0.30 * 80 + 0.20 * (80 * 0.85 / 1.15)
                         + 0.10 * 78.75 + 0.05 * 80) / 0.65
        self.assertAlmostEqual(pred.pred_away, expected_away, places=3)


class TestDisplayTieBreak(unittest.TestCase):
    def test_mlb_nhl_break_ties_toward_winner(self):
        from pipeline.models.defs.my_model import MyModel
        # 4.4 vs 4.6 both round to 4/5? use 4.4 vs 4.4-style true tie
        self.assertEqual(MyModel._display_scores("MLB", 4.4, 4.4, False), (4, 5))
        self.assertEqual(MyModel._display_scores("NHL", 3.5, 3.4, True), (4, 3))

    def test_basketball_keeps_rounded_tie(self):
        from pipeline.models.defs.my_model import MyModel
        self.assertEqual(MyModel._display_scores("NBA", 110.4, 110.4, True), (110, 110))
        self.assertEqual(MyModel._display_scores("WNBA", 84.4, 84.4, False), (84, 84))


if __name__ == "__main__":
    unittest.main()
