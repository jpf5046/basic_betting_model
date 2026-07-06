"""Offline tests for historic backfill (pipeline/backfill.py + the
leaguegamelog parser + the per-adapter season helpers).

From the repo root:  python3 -m unittest tests.test_backfill -v
"""
import json
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from pipeline import backfill
from pipeline.ingest import leaguegamelog, mlb, nba, nhl, wnba
from pipeline.ingest.core import UnmappedTeamError

FIXTURES = Path(__file__).parent / "fixtures"


def wnba_resolver():
    return wnba._historic_resolver()


def nba_resolver():
    by_nba_id, _ = nba.load_team_maps()

    def resolve(team_id, abbrev):
        return by_nba_id.get(int(team_id)) if team_id else None

    return resolve


class TestLeagueGameLogParser(unittest.TestCase):
    def test_pairs_rows_into_games(self):
        doc = json.loads((FIXTURES / "leaguegamelog_wnba_sample.json").read_text())
        games, skipped = leaguegamelog.build_games(
            doc, "WNBA", "2024", "Regular Season", wnba_resolver())
        self.assertEqual(len(games), 2)
        self.assertEqual(skipped, 1)  # the orphan MIN row has no partner

        lva_nyl = next(g for g in games if g.game_id == "1022400101")
        self.assertEqual(lva_nyl.away_team_id, "wnba-lva")
        self.assertEqual(lva_nyl.home_team_id, "wnba-nyl")
        self.assertEqual((lva_nyl.away_score, lva_nyl.home_score), ("87", "90"))
        self.assertEqual(lva_nyl.date, "2024-06-01")
        self.assertEqual(lva_nyl.season_type, "regular")
        self.assertEqual(lva_nyl.status, "final")
        self.assertEqual(lva_nyl.start_time_utc, "")  # no times historically

    def test_alias_tricode_resolves(self):
        # stats.wnba.com spells Phoenix "PHO"; our registry says PHX.
        doc = json.loads((FIXTURES / "leaguegamelog_wnba_sample.json").read_text())
        games, _ = leaguegamelog.build_games(
            doc, "WNBA", "2024", "Regular Season", wnba_resolver())
        pho_sea = next(g for g in games if g.game_id == "1022400102")
        self.assertEqual(pho_sea.home_team_id, "wnba-phx")
        self.assertEqual(pho_sea.home_abbrev, "PHX")
        self.assertEqual(pho_sea.away_team_id, "wnba-sea")

    def test_nba_resolves_by_team_id(self):
        doc = json.loads((FIXTURES / "leaguegamelog_nba_sample.json").read_text())
        games, skipped = leaguegamelog.build_games(
            doc, "NBA", "2022-23", "Regular Season", nba_resolver())
        self.assertEqual((len(games), skipped), (1, 0))
        g = games[0]
        self.assertEqual((g.away_team_id, g.home_team_id), ("nba-bos", "nba-nyk"))
        self.assertEqual((g.away_score, g.home_score), ("120", "118"))
        self.assertEqual(g.season, "2022-23")

    def test_unmapped_team_is_fatal(self):
        doc = json.loads((FIXTURES / "leaguegamelog_nba_sample.json").read_text())
        doc["resultSets"][0]["rowSet"][0][1] = 99999  # bogus TEAM_ID
        with self.assertRaises(UnmappedTeamError):
            leaguegamelog.build_games(doc, "NBA", "2022-23", "Regular Season",
                                      nba_resolver())

    def test_bad_shape_is_loud(self):
        with self.assertRaises(RuntimeError):
            leaguegamelog.build_games({"nope": 1}, "NBA", "2022-23",
                                      "Regular Season", nba_resolver())


class TestSeasonHelpers(unittest.TestCase):
    def test_past_seasons_per_sport(self):
        # July 2026: MLB/WNBA current=2026, NBA current=2025-26, NHL 20252026
        self.assertEqual(mlb.past_seasons(3), ["2025", "2024", "2023"])
        self.assertEqual(wnba.past_seasons(3), ["2025", "2024", "2023"])
        self.assertEqual(nba.past_seasons(3), ["2024-25", "2023-24", "2022-23"])
        self.assertEqual(nhl.past_seasons(3), ["20242025", "20232024", "20222023"])

    def test_nba_year_boundary_format(self):
        # 2000-01 style boundaries keep two digits.
        with mock.patch.object(nba, "default_season", return_value="1999-00"):
            self.assertEqual(nba.past_seasons(1), ["1998-99"])


class TestHistoricTeams(unittest.TestCase):
    def test_arizona_coyotes_mapped_for_old_seasons(self):
        by_nhl_id, by_abbrev = nhl.load_team_maps()
        self.assertEqual(by_nhl_id[53], ("nhl-ari", "ARI"))
        self.assertEqual(by_abbrev["ARI"], "nhl-ari")
        # and the 32 active franchises are all still there
        self.assertEqual(len(by_nhl_id), 33)


class TestBackfillOrchestration(unittest.TestCase):
    def test_contract_enforced(self):
        broken = mock.Mock(spec=[])  # implements nothing
        with mock.patch.dict(backfill.ADAPTERS, {"MLB": broken}):
            with self.assertRaises(NotImplementedError):
                backfill.seasons_for("MLB", 3, None)

    def test_season_list_current_plus_back(self):
        self.assertEqual(backfill.seasons_for("MLB", 2, None),
                         [None, "2025", "2024"])
        self.assertEqual(backfill.seasons_for("MLB", 0, ["2019", "2018"]),
                         ["2019", "2018"])

    def test_failures_are_per_season_not_fatal(self):
        calls = []

        def fake_run_fetch(season, offline=False):
            calls.append(season)
            if season == "2024":
                raise RuntimeError("boom")
            return [], Path("/tmp/x.csv")

        fake = mock.Mock(run_fetch=mock.Mock(side_effect=fake_run_fetch),
                         past_seasons=mock.Mock(return_value=["2025", "2024", "2023"]))
        with mock.patch.dict(backfill.ADAPTERS, {"MLB": fake}):
            errors = backfill.backfill("MLB", 3, None, offline=False)
        self.assertEqual(calls, [None, "2025", "2024", "2023"])  # kept going
        self.assertEqual(len(errors), 1)
        self.assertIn("2024", errors[0])


if __name__ == "__main__":
    unittest.main()
