"""Offline tests for the feature registry (pipeline/features/).

Contexts are built from the same fixture feeds the ingester tests use —
no network, no games CSVs on disk.
From the repo root:  python3 -m unittest tests.test_features -v
"""
import json
import unittest
from datetime import date
from pathlib import Path

import pipeline.features  # noqa: F401  (auto-registers everything in defs/)
from pipeline.features.context import FeatureContext
from pipeline.features.frame import build_rows
from pipeline.features.registry import features_for, get_feature, register
from pipeline.ingest import nhl, wnba
from pipeline.ingest.core import todays_games

FIXTURES = Path(__file__).parent / "fixtures"


def wnba_context(as_of: date):
    doc = json.loads((FIXTURES / "wnba_schedule_sample.json").read_text())
    games = wnba.parse_games(doc, wnba.load_team_map())
    return FeatureContext("WNBA", as_of, games), games


def nhl_context(as_of: date):
    docs = json.loads((FIXTURES / "nhl_club_schedules_sample.json").read_text())
    by_nhl_id, by_abbrev = nhl.load_team_maps()
    games = nhl.parse_games(list(docs.values()), by_nhl_id, by_abbrev)
    return FeatureContext("NHL", as_of, games), games


class TestRegistry(unittest.TestCase):
    def test_defs_auto_registered(self):
        names = {s.name for s in features_for("WNBA")}
        self.assertEqual(names, {
            "season_scoring", "common_opponents", "last10_form",
            "home_advantage", "head_to_head", "games_played", "travel_km",
        })

    def test_duplicate_name_rejected(self):
        with self.assertRaises(ValueError):
            @register("season_scoring")
            def season_scoring(ctx, game, team_id):  # pragma: no cover
                return None

    def test_spec_metadata(self):
        spec = get_feature("travel_km")
        self.assertEqual(spec.version, 1)
        self.assertEqual(spec.side, "team")
        spec = get_feature("home_advantage")
        self.assertEqual(spec.side, "game")


class TestPointInTime(unittest.TestCase):
    def test_context_only_sees_strictly_before_as_of(self):
        ctx, _ = wnba_context(date(2026, 6, 10))
        dates = {g.date for g in ctx.past_games}
        self.assertEqual(len(ctx.past_games), 5)
        self.assertTrue(all(d < "2026-06-10" for d in dates))

    def test_scheduled_and_preseason_never_in_context(self):
        ctx, _ = wnba_context(date(2026, 12, 31))  # far future as_of
        ids = {g.game_id for g in ctx.past_games}
        self.assertNotIn("1012600001", ids)  # preseason final
        self.assertNotIn("1022600201", ids)  # scheduled


class TestWnbaFeatures(unittest.TestCase):
    def setUp(self):
        self.ctx, games = wnba_context(date(2026, 7, 5))
        slate = todays_games(games, date(2026, 7, 5))
        self.by_code = {(g.away_abbrev, g.home_abbrev): g for g in slate}
        self.nyl_lva = self.by_code[("NYL", "LVA")]
        self.phx_sea = self.by_code[("PHX", "SEA")]

    def run_feature(self, name, game, team_id):
        return get_feature(name).fn(self.ctx, game, team_id)

    def test_season_scoring(self):
        out = self.run_feature("season_scoring", self.nyl_lva, "wnba-nyl")
        self.assertEqual(out, {"spg": 88.5, "sapg": 78.5, "gp": 4})

    def test_season_scoring_none_without_games(self):
        self.assertIsNone(self.run_feature("season_scoring", self.phx_sea, "wnba-sea"))

    def test_last10_form(self):
        out = self.run_feature("last10_form", self.nyl_lva, "wnba-nyl")
        self.assertEqual(out, {"wins": 3, "losses": 1, "otl": 0, "l10_pct": 0.75})

    def test_common_opponents_matches_core_math(self):
        out = self.run_feature("common_opponents", self.nyl_lva, "wnba-nyl")
        self.assertEqual(out, {"opponents": 2, "games": 3, "wrf": 89.0, "wra": 75.0})

    def test_head_to_head_both_sides(self):
        away = self.run_feature("head_to_head", self.nyl_lva, "wnba-nyl")
        home = self.run_feature("head_to_head", self.nyl_lva, "wnba-lva")
        self.assertEqual(away, {"games": 1, "avg_scored": 87.0, "avg_allowed": 89.0})
        self.assertEqual(home, {"games": 1, "avg_scored": 89.0, "avg_allowed": 87.0})

    def test_games_played_and_home_advantage(self):
        self.assertEqual(self.run_feature("games_played", self.nyl_lva, "wnba-nyl"), 4)
        self.assertEqual(self.run_feature("home_advantage", self.nyl_lva, None), 2.5)

    def test_travel_km_zero_when_staying_put(self):
        # NYL's last game was at LVA's arena and today's game is there too.
        self.assertEqual(self.run_feature("travel_km", self.nyl_lva, "wnba-nyl"), 0.0)
        # LVA played that same game at home.
        self.assertEqual(self.run_feature("travel_km", self.nyl_lva, "wnba-lva"), 0.0)

    def test_travel_km_phoenix_to_seattle(self):
        # PHX last hosted on 06-15; today they play in Seattle (~1,790 km).
        km = self.run_feature("travel_km", self.phx_sea, "wnba-phx")
        self.assertGreater(km, 1700)
        self.assertLess(km, 1900)

    def test_travel_km_none_without_history(self):
        self.assertIsNone(self.run_feature("travel_km", self.phx_sea, "wnba-sea"))


class TestNhlFeatures(unittest.TestCase):
    def test_last10_form_is_points_based(self):
        ctx, _ = nhl_context(date(2026, 1, 1))
        game = type("G", (), {})()  # last10_form only reads the team log
        out = get_feature("last10_form").fn(ctx, game, "nhl-tor")
        # TOR: OTL, W, W, L -> points (2*2 + 1) / (2*4) = 0.625
        self.assertEqual(out, {"wins": 2, "losses": 1, "otl": 1, "l10_pct": 0.625})


class TestFrame(unittest.TestCase):
    def test_build_rows_shape(self):
        ctx, games = wnba_context(date(2026, 7, 5))
        slate = todays_games(games, date(2026, 7, 5))
        columns, rows = build_rows(ctx, slate)
        self.assertEqual(len(rows), 2)
        for col in ("game_id", "away_team_id", "home_team_id",
                    "away_season_scoring_spg", "home_common_opponents_wrf",
                    "home_advantage", "away_travel_km"):
            self.assertIn(col, columns)
        nyl_lva = next(r for r in rows if r["away_team_id"] == "wnba-nyl")
        self.assertEqual(nyl_lva["away_season_scoring_spg"], 88.5)
        self.assertEqual(nyl_lva["home_advantage"], 2.5)

    def test_missing_data_stays_blank_not_wrong(self):
        ctx, games = wnba_context(date(2026, 7, 5))
        slate = todays_games(games, date(2026, 7, 5))
        _, rows = build_rows(ctx, slate)
        phx_sea = next(r for r in rows if r["home_team_id"] == "wnba-sea")
        # SEA has no completed games in the fixture: None -> blank marker
        # column, never a fabricated number.
        self.assertEqual(phx_sea.get("home_season_scoring", ""), "")
        self.assertNotIn("home_season_scoring_spg", phx_sea)


if __name__ == "__main__":
    unittest.main()
