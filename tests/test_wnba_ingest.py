"""Offline tests for the WNBA ingester (pipeline/ingest/wnba.py).

Runs entirely against tests/fixtures/wnba_schedule_sample.json — no network.
From the repo root:  python3 -m unittest tests.test_wnba_ingest -v
"""
import copy
import gzip
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from pipeline.ingest.core import read_feed_json
from pipeline.ingest.wnba import (
    UnmappedTeamError,
    common_opponents,
    completed_regular_season,
    load_team_map,
    parse_games,
    team_log,
    todays_games,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wnba_schedule_sample.json"


def load_fixture_games():
    doc = json.loads(FIXTURE.read_text())
    return parse_games(doc, load_team_map()), doc


class TestTeamMap(unittest.TestCase):
    def test_all_15_wnba_teams_present(self):
        team_map = load_team_map()
        canonical = {v for v in team_map.values()}
        self.assertEqual(len(canonical), 15)
        self.assertEqual(team_map["NYL"], "wnba-nyl")

    def test_feed_aliases_resolve(self):
        team_map = load_team_map()
        self.assertEqual(team_map["CONN"], "wnba-con")
        self.assertEqual(team_map["PHO"], "wnba-phx")
        self.assertEqual(team_map["GS"], "wnba-gsv")


class TestParse(unittest.TestCase):
    def setUp(self):
        self.games, self.doc = load_fixture_games()

    def test_counts_and_allstar_skipped(self):
        # 12 fixture games, minus the All-Star exhibition (synthetic tricodes).
        self.assertEqual(len(self.games), 11)
        self.assertNotIn("1032600001", {g.game_id for g in self.games})

    def test_season_types_from_game_id(self):
        by_id = {g.game_id: g for g in self.games}
        self.assertEqual(by_id["1012600001"].season_type, "preseason")
        self.assertEqual(by_id["1022600101"].season_type, "regular")

    def test_scores_only_on_finals(self):
        by_id = {g.game_id: g for g in self.games}
        final = by_id["1022600101"]
        self.assertEqual(final.status, "final")
        self.assertEqual((final.away_score, final.home_score), ("90", "80"))
        scheduled = by_id["1022600201"]
        self.assertEqual(scheduled.status, "scheduled")
        self.assertEqual((scheduled.away_score, scheduled.home_score), ("", ""))

    def test_aliased_tricodes_normalized(self):
        by_id = {g.game_id: g for g in self.games}
        con_pho = by_id["1022600150"]
        self.assertEqual(con_pho.away_team_id, "wnba-con")
        self.assertEqual(con_pho.home_team_id, "wnba-phx")
        self.assertEqual(con_pho.away_abbrev, "CON")
        self.assertEqual(con_pho.home_abbrev, "PHX")

    def test_eastern_date_from_utc(self):
        # 2026-07-06T02:00:00Z is still July 5 in US/Eastern.
        by_id = {g.game_id: g for g in self.games}
        self.assertEqual(by_id["1022600201"].date, "2026-07-05")

    def test_unmapped_regular_season_team_is_fatal(self):
        doc = copy.deepcopy(self.doc)
        bad = doc["leagueSchedule"]["gameDates"][1]["games"][0]
        bad["homeTeam"]["teamTricode"] = "XXX"
        with self.assertRaises(UnmappedTeamError):
            parse_games(doc, load_team_map())


class TestReadFeedJson(unittest.TestCase):
    """Raw-cache parsing must survive what the CDN actually sends back:
    gzip bodies, UTF-8 BOMs, and (diagnosably) non-JSON error pages."""

    def _write(self, body: bytes) -> Path:
        f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.addCleanup(Path(f.name).unlink)
        f.write(body)
        f.close()
        return Path(f.name)

    def test_plain_json(self):
        self.assertEqual(read_feed_json(self._write(b'{"a": 1}')), {"a": 1})

    def test_utf8_bom(self):
        self.assertEqual(read_feed_json(self._write(b'\xef\xbb\xbf{"a": 1}')), {"a": 1})

    def test_gzip_body(self):
        self.assertEqual(read_feed_json(self._write(gzip.compress(b'{"a": 1}'))), {"a": 1})

    def test_non_json_body_names_the_file(self):
        path = self._write(b"<html>Access Denied</html>")
        with self.assertRaises(RuntimeError) as ctx:
            read_feed_json(path)
        self.assertIn(str(path), str(ctx.exception))
        self.assertIn("Access Denied", str(ctx.exception))


class TestQueries(unittest.TestCase):
    def setUp(self):
        self.games, _ = load_fixture_games()

    def test_todays_games(self):
        today = todays_games(self.games, date(2026, 7, 5))
        self.assertEqual(len(today), 2)
        self.assertEqual({g.game_id for g in today}, {"1022600201", "1022600202"})

    def test_completed_excludes_preseason_and_scheduled(self):
        finals = completed_regular_season(self.games)
        self.assertEqual(len(finals), 8)
        ids = {g.game_id for g in finals}
        self.assertNotIn("1012600001", ids)  # preseason final
        self.assertNotIn("1022600201", ids)  # scheduled

    def test_team_log(self):
        log = team_log(self.games, "wnba-nyl")
        self.assertEqual(len(log), 4)
        first = log[0]
        self.assertEqual(first["opponent_id"], "wnba-ind")
        self.assertEqual((first["scored"], first["allowed"], first["result"]), (90, 80, "W"))
        results = [r["result"] for r in log]
        self.assertEqual(results, ["W", "W", "W", "L"])

    def test_common_opponents_math(self):
        # Hand-computed from the fixture:
        # NYL vs common opps {IND x2, ATL x1}: wRF = (90+85+92)/3, wRA = (80+75+70)/3
        # LVA vs common opps {IND x1, ATL x2}: wRF = (88+95+100)/3, wRA = (84+90+82)/3
        out = common_opponents(self.games, "wnba-nyl", "wnba-lva")
        self.assertEqual(out["common_opponents"], ["wnba-atl", "wnba-ind"])

        nyl = out["wnba-nyl"]
        self.assertEqual(nyl["games"], 3)
        self.assertAlmostEqual(nyl["wRF"], 89.0)
        self.assertAlmostEqual(nyl["wRA"], 75.0)
        self.assertEqual(nyl["per_opponent"]["wnba-ind"], {"games": 2, "avg_scored": 87.5, "avg_allowed": 77.5})

        lva = out["wnba-lva"]
        self.assertEqual(lva["games"], 3)
        self.assertAlmostEqual(lva["wRF"], 94.3333)
        self.assertAlmostEqual(lva["wRA"], 85.3333)
        self.assertEqual(lva["per_opponent"]["wnba-atl"], {"games": 2, "avg_scored": 97.5, "avg_allowed": 86.0})

    def test_h2h_games_excluded_from_common_opponents(self):
        # NYL@LVA on 06-13 must not leak into either side's common-opponent rows.
        out = common_opponents(self.games, "wnba-nyl", "wnba-lva")
        self.assertNotIn("wnba-lva", out["wnba-nyl"]["per_opponent"])
        self.assertNotIn("wnba-nyl", out["wnba-lva"]["per_opponent"])


if __name__ == "__main__":
    unittest.main()
