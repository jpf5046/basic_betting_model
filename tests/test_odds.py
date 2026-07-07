"""Offline tests for the odds package (pipeline/odds/).

Fixture responses mirror The Odds API v4 shapes; matching runs against
the same WNBA/MLB fixture feeds the ingester tests use. No network.
From the repo root:  python3 -m unittest tests.test_odds -v
"""
import json
import tempfile
import unittest
from pathlib import Path

from pipeline.grading.grader import grade_pick
from pipeline.ingest import mlb, wnba
from pipeline.odds import match as match_mod
from pipeline.odds.edge import edge_rows
from pipeline.odds.match import match_events, upsert_event_map, load_event_map
from pipeline.odds.money import (
    american_to_decimal,
    decimal_to_american,
    ev_per_100,
    implied_prob,
    median_price,
    payout_per_100,
)
from pipeline.odds.normalize import normalize_events
from pipeline.odds.store import consensus_h2h, consensus_total, closing_rows

FIXTURES = Path(__file__).parent / "fixtures"
SNAP_TS = "2026-07-05T12:00:00Z"


def wnba_rows():
    doc = json.loads((FIXTURES / "odds_api_wnba_sample.json").read_text())
    return normalize_events(doc, "WNBA", SNAP_TS)


def wnba_games():
    doc = json.loads((FIXTURES / "wnba_schedule_sample.json").read_text())
    return wnba.parse_games(doc, wnba.load_team_map())


class TestMoney(unittest.TestCase):
    def test_implied_prob(self):
        self.assertAlmostEqual(implied_prob(150), 0.4)
        self.assertAlmostEqual(implied_prob(-150), 0.6)
        self.assertAlmostEqual(implied_prob(100), 0.5)

    def test_payout_per_100(self):
        self.assertEqual(payout_per_100(150), 150.0)
        self.assertEqual(payout_per_100(-110), 90.91)
        self.assertEqual(payout_per_100(-150), 66.67)

    def test_decimal_round_trip(self):
        for odds in (150, -150, -110, 100, 245, -305):
            self.assertAlmostEqual(
                decimal_to_american(american_to_decimal(odds)), odds, places=1)

    def test_invalid_american_rejected(self):
        with self.assertRaises(ValueError):
            american_to_decimal(-50)

    def test_ev_per_100(self):
        # 54% at -130: 0.54*76.92 - 0.46*100 = -4.46 (edge is negative)
        self.assertAlmostEqual(ev_per_100(0.54, -130), -4.46, places=2)
        # 54% at +110: 0.54*110 - 0.46*100 = +13.40
        self.assertAlmostEqual(ev_per_100(0.54, 110), 13.40, places=2)

    def test_median_price_in_decimal_space(self):
        self.assertEqual(median_price([-130, -125, -135]), -130.0)
        # +100 and -110 straddle the American discontinuity: the naive
        # numeric median (-5) is nonsense; decimal-space gives ~-104.8.
        m = median_price([100, -110])
        self.assertLess(m, -100)
        self.assertGreater(m, -110)


class TestNormalize(unittest.TestCase):
    def test_rows_and_team_mapping(self):
        rows, unmatched = wnba_rows()
        self.assertEqual(unmatched, ["Springfield Isotopes"])
        e1 = [r for r in rows if r["event_id"] == "aaa111"]
        self.assertEqual(e1[0]["home_team_id"], "wnba-lva")
        self.assertEqual(e1[0]["away_team_id"], "wnba-nyl")
        h2h_lva = [r for r in e1 if r["market"] == "h2h" and r["outcome"] == "wnba-lva"]
        self.assertEqual([float(r["price_american"]) for r in h2h_lva], [-130, -125, -135])
        overs = [r for r in e1 if r["market"] == "totals" and r["outcome"] == "OVER"]
        self.assertEqual({float(r["point"]) for r in overs}, {165.5, 166.0})

    def test_bookmakerless_event_still_yields_identity_row(self):
        rows, _ = wnba_rows()
        e3 = [r for r in rows if r["event_id"] == "ccc333"]
        self.assertEqual(len(e3), 1)
        self.assertEqual(e3[0]["market"], "")

    def test_historical_wrapper_shape(self):
        events = json.loads((FIXTURES / "odds_api_wnba_sample.json").read_text())
        wrapped = {"timestamp": "2026-07-01T00:00:00Z", "data": events}
        rows, _ = normalize_events(wrapped, "WNBA", SNAP_TS)
        self.assertEqual(rows[0]["snapshot_ts"], "2026-07-01T00:00:00Z")


class TestMatching(unittest.TestCase):
    def test_wnba_matches_by_teams_and_date(self):
        rows, _ = wnba_rows()
        result = match_events(rows, wnba_games(), "WNBA")
        matched = {m["event_id"]: m for m in result.matched}
        self.assertEqual(matched["aaa111"]["game_id"], "1022600201")
        self.assertEqual(matched["bbb222"]["game_id"], "1022600202")
        self.assertEqual(matched["aaa111"]["matched_on"], "teams+date")
        # the fake-team event can't match
        self.assertEqual([e["event_id"] for e in result.unmatched], ["ccc333"])

    def test_mlb_doubleheader_disambiguated_by_time(self):
        doc = json.loads((FIXTURES / "odds_api_mlb_dh_sample.json").read_text())
        rows, _ = normalize_events(doc, "MLB", SNAP_TS)
        games_doc = json.loads((FIXTURES / "mlb_schedule_sample.json").read_text())
        by_mlb_id, _ = mlb.load_team_maps()
        games = mlb.parse_games(games_doc, by_mlb_id)
        result = match_events(rows, games, "MLB")
        matched = {m["event_id"]: m for m in result.matched}
        self.assertEqual(matched["dh-early"]["game_id"], "813005")  # 17:05Z game
        self.assertEqual(matched["dh-late"]["game_id"], "813006")   # 23:10Z game
        self.assertEqual(matched["dh-early"]["matched_on"], "teams+time")
        self.assertEqual(result.ambiguous, [])

    def test_event_map_upsert_is_idempotent(self):
        rows, _ = wnba_rows()
        result = match_events(rows, wnba_games(), "WNBA")
        original = match_mod.ODDS_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                match_mod.ODDS_DIR = Path(tmp)
                upsert_event_map("WNBA", result.matched)
                upsert_event_map("WNBA", result.matched)  # again
                emap = load_event_map("WNBA")
                self.assertEqual(len(emap), 2)
                self.assertEqual(emap["aaa111"]["game_id"], "1022600201")
        finally:
            match_mod.ODDS_DIR = original


class TestStore(unittest.TestCase):
    def test_consensus_h2h_and_total(self):
        rows, _ = wnba_rows()
        price, books = consensus_h2h(rows, "aaa111", "wnba-lva")
        self.assertEqual((price, books), (-130.0, 3))
        point, tprice, tbooks = consensus_total(rows, "aaa111", "OVER")
        self.assertEqual(point, 165.5)  # median of 165.5, 165.5, 166.0
        self.assertEqual(tbooks, 3)

    def test_closing_prefers_latest_pregame_snapshot(self):
        rows, _ = wnba_rows()
        older = [dict(r, snapshot_ts="2026-07-04T12:00:00Z") for r in rows]
        postgame = [dict(r, snapshot_ts="2026-07-06T09:00:00Z") for r in rows]
        merged = older + rows + postgame
        chosen = closing_rows(merged, "aaa111", "h2h")
        self.assertTrue(all(r["snapshot_ts"] == SNAP_TS for r in chosen))


class TestEdge(unittest.TestCase):
    def make_prediction(self):
        return {
            "game_id": "1022600201", "sport": "WNBA", "date": "2026-07-05",
            "away_team_id": "wnba-nyl", "home_team_id": "wnba-lva",
            "winner_team_id": "wnba-lva", "win_prob": "0.54",
            "pred_total": "172.8518",
        }

    def test_ml_and_total_edges(self):
        rows, _ = wnba_rows()
        out = edge_rows(self.make_prediction(), "aaa111", rows)
        ml = next(r for r in out if r["bet_type"] == "ML")
        self.assertEqual(ml["market_price"], -130.0)
        self.assertAlmostEqual(ml["implied_prob"], 0.5652, places=4)
        self.assertAlmostEqual(ml["edge"], 0.54 - 0.5652, places=4)
        self.assertAlmostEqual(ml["ev_per_100"], -4.46, places=2)

        total = next(r for r in out if r["bet_type"] == "TOTAL")
        self.assertEqual(total["selection"], "OVER")  # 172.85 > 165.5
        self.assertEqual(total["market_point"], 165.5)
        self.assertAlmostEqual(total["edge"], 172.8518 - 165.5, places=3)


class TestOddsAwareGrading(unittest.TestCase):
    def game(self, away=90, home=88, status="final"):
        from tests.test_grading import make_game
        return make_game(game_id="g1", status=status, away=away, home=home,
                         away_team="wnba-nyl", home_team="wnba-lva")

    def pick(self, bet_type="ML", selection="wnba-nyl", line=""):
        return {"game_id": "g1", "sport": "WNBA", "date": "2026-07-05",
                "bet_type": bet_type, "selection": selection,
                "confidence": "", "line": line}

    def test_ml_win_pays_market_price(self):
        plus = grade_pick(self.pick(), self.game(), price_american=120)
        minus = grade_pick(self.pick(), self.game(), price_american=-150)
        self.assertEqual((plus.result, plus.pnl, plus.price_american), ("WIN", 120.0, "120"))
        self.assertEqual((minus.result, minus.pnl), ("WIN", 66.67))

    def test_loss_always_costs_the_stake(self):
        grade = grade_pick(self.pick(selection="wnba-lva"), self.game(), price_american=-150)
        self.assertEqual((grade.result, grade.pnl), ("LOSS", -100))

    def test_total_graded_against_market_line(self):
        # Model threshold was 168, market line 165.5, actual total 167:
        # the bettable OVER 165.5 WINS even though 167 < the model's 168.
        grade = grade_pick(self.pick(bet_type="TOTAL", selection="OVER", line="168"),
                           self.game(away=90, home=77),
                           price_american=-110, market_point=165.5)
        self.assertEqual((grade.result, grade.pnl, grade.line), ("WIN", 90.91, "165.5"))

    def test_no_odds_stays_flat_100(self):
        grade = grade_pick(self.pick(), self.game())
        self.assertEqual((grade.result, grade.pnl, grade.price_american), ("WIN", 100, ""))


if __name__ == "__main__":
    unittest.main()
