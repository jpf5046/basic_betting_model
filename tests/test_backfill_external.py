"""Offline tests for pipeline/backfill_external.py — importing an external
games_scores / games_odds export (the fallback path for dates too old for
the live feeds or The Odds API's historical-snapshot window).

Fixtures are a small, hand-built subset shaped exactly like a real export
(tests/fixtures/external_scores_sample.csv, external_odds_sample.csv),
covering: a normal in-scope game, a game with NULL team ids (name-only
resolution), a relocated-franchise name (Arizona Coyotes -> Utah Mammoth),
the basketball_nba_preseason sport_key override, a not-yet-completed game,
an out-of-scope sport (no team registry), and a genuinely unmappable team
name. No network. From the repo root:
    python3 -m unittest tests.test_backfill_external -v
"""
import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import backfill_external as be
from pipeline.ingest import mlb, nba, nhl, wnba
from pipeline.odds import match, store

FIXTURES = Path(__file__).parent / "fixtures"
SCORES_CSV = FIXTURES / "external_scores_sample.csv"
ODDS_CSV = FIXTURES / "external_odds_sample.csv"

ALL_SPORTS = {"WNBA", "MLB", "NBA", "NHL"}


def read_rows(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class TestResolveSport(unittest.TestCase):
    def test_known_sport_keys(self):
        self.assertEqual(be._resolve_sport("basketball_nba"), ("NBA", None))
        self.assertEqual(be._resolve_sport("baseball_mlb"), ("MLB", None))
        self.assertEqual(be._resolve_sport("icehockey_nhl"), ("NHL", None))
        self.assertEqual(be._resolve_sport("basketball_wnba"), ("WNBA", None))

    def test_preseason_override(self):
        self.assertEqual(be._resolve_sport("basketball_nba_preseason"), ("NBA", "preseason"))

    def test_unknown_sport_key(self):
        self.assertEqual(be._resolve_sport("basketball_ncaab"), (None, None))


class TestBuildGames(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = read_rows(SCORES_CSV)
        cls.names = be.NameMaps()
        cls.by_sport_season, cls.skipped, cls.unmapped = be.build_games(
            cls.rows, ALL_SPORTS, cls.names)

    def _game(self, game_id: str):
        for games in self.by_sport_season.values():
            for g in games:
                if g.game_id == game_id:
                    return g
        raise AssertionError(f"{game_id} not found")

    def test_game_id_is_the_odds_api_event_id(self):
        g = self._game("00005f0bead0cd85b8134cf9bf78b328")
        self.assertEqual(g.away_team_id, "nba-uta")
        self.assertEqual(g.home_team_id, "nba-nyk")
        self.assertEqual((g.away_score, g.home_score), ("103", "118"))
        self.assertEqual(g.status, "final")
        self.assertEqual(g.season, "2023-24")
        self.assertEqual(g.date, "2024-01-30")  # UTC 00:41 -> Eastern prior day

    def test_null_team_ids_resolved_by_name(self):
        g = self._game("59663773d5f2ed65bb700098cb3bd7f8")
        self.assertEqual(g.home_team_id, "mlb-cws")
        self.assertEqual(g.away_team_id, "mlb-tex")

    def test_relocated_franchise_alias(self):
        g = self._game("1bf5188619829799756fb30c36aa2f03")
        self.assertEqual(g.home_team_id, "nhl-uta")  # Arizona Coyotes is home_team here

    def test_preseason_season_type(self):
        g = self._game("1c4d8e67129ceebd4851771b0492d1b3")
        self.assertEqual(g.season_type, "preseason")

    def test_not_completed_has_no_score(self):
        g = self._game("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(g.status, "scheduled")
        self.assertEqual((g.away_score, g.home_score), ("", ""))

    def test_out_of_scope_sport_skipped_and_reported(self):
        ids = {g.game_id for games in self.by_sport_season.values() for g in games}
        self.assertNotIn("1bef17cd72725d6dfe35e19c65bf6058", ids)
        self.assertEqual(self.skipped["basketball_ncaab"], 1)

    def test_unmappable_team_name_reported_not_raised(self):
        ids = {g.game_id for games in self.by_sport_season.values() for g in games}
        self.assertNotIn("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", ids)
        self.assertTrue(any("Fictional Nine" in line for line in self.unmapped))

    def test_sports_filter_excludes(self):
        by_sport_season, skipped, _ = be.build_games(self.rows, {"NBA"}, be.NameMaps())
        self.assertEqual(set(s for s, _ in by_sport_season), {"NBA"})
        self.assertTrue(any("excluded by --sports" in k for k in skipped))


class TestBuildOddsRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = read_rows(ODDS_CSV)
        cls.names = be.NameMaps()
        cls.by_sport, cls.skipped = be.build_odds_rows(cls.rows, ALL_SPORTS, cls.names)

    def test_out_of_scope_odds_skipped(self):
        self.assertNotIn("NCAAB", self.by_sport)
        self.assertEqual(self.skipped["basketball_ncaab"], 1)

    def test_h2h_outcome_is_team_id(self):
        nba_rows = self.by_sport["NBA"]
        h2h = [r for r in nba_rows if r["market"] == "h2h" and r["bookmaker"] == "betmgm"]
        outcomes = {r["outcome"] for r in h2h}
        self.assertEqual(outcomes, {"nba-nyk", "nba-uta"})

    def test_totals_outcome_is_over_under(self):
        totals = [r for r in self.by_sport["NBA"] if r["market"] == "totals"]
        self.assertEqual({r["outcome"] for r in totals}, {"OVER", "UNDER"})

    def test_repeated_bookmaker_preserves_both_snapshots(self):
        # BetMGM appears twice in the fixture at different last_update times
        # (price movement) -- both must survive as distinct snapshot rows.
        betmgm_h2h = [r for r in self.by_sport["NBA"]
                     if r["bookmaker"] == "betmgm" and r["market"] == "h2h"]
        self.assertEqual(len(betmgm_h2h), 4)  # 2 outcomes x 2 timestamps
        self.assertEqual({r["snapshot_ts"] for r in betmgm_h2h},
                         {"2024-01-30T09:00:00Z", "2024-01-30T10:58:34Z"})

    def test_null_team_id_row_still_resolves_by_name_for_odds(self):
        mlb_rows = self.by_sport["MLB"]
        h2h = [r for r in mlb_rows if r["event_id"] == "59663773d5f2ed65bb700098cb3bd7f8"]
        self.assertTrue(h2h)
        self.assertEqual(h2h[0]["home_team_id"], "mlb-cws")

    def test_relocated_franchise_alias_in_odds(self):
        nhl_rows = self.by_sport["NHL"]
        outcomes = {r["outcome"] for r in nhl_rows if r["market"] == "h2h"}
        self.assertIn("nhl-uta", outcomes)


class TestEndToEnd(unittest.TestCase):
    """Full main() run against temp dirs: games CSVs, odds snapshot, and
    event map all get written, and the flattened odds are consumable by
    the existing consensus/closing-line functions unmodified."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.patchers = [
            mock.patch.object(mlb, "OUT_DIR", self.tmp / "mlb"),
            mock.patch.object(nba, "OUT_DIR", self.tmp / "nba"),
            mock.patch.object(nhl, "OUT_DIR", self.tmp / "nhl"),
            mock.patch.object(wnba, "OUT_DIR", self.tmp / "wnba"),
            mock.patch.object(store, "ODDS_DIR", self.tmp / "odds"),
            mock.patch.object(match, "ODDS_DIR", self.tmp / "odds"),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_full_run_writes_games_odds_and_event_map(self):
        with contextlib.redirect_stdout(io.StringIO()):
            be.main(["--scores", str(SCORES_CSV), "--odds", str(ODDS_CSV)])

        nba_games = (self.tmp / "nba" / "games_2023-24.csv").read_text()
        self.assertIn("00005f0bead0cd85b8134cf9bf78b328", nba_games)

        rows = store.load_snapshot_rows("NBA")
        self.assertTrue(rows)
        h2h = store.consensus_h2h(rows, "00005f0bead0cd85b8134cf9bf78b328", "nba-nyk")
        self.assertIsNotNone(h2h)

        event_map = match.load_event_map("NBA")
        entry = event_map["00005f0bead0cd85b8134cf9bf78b328"]
        self.assertEqual(entry["game_id"], "00005f0bead0cd85b8134cf9bf78b328")
        self.assertEqual(entry["matched_on"], "external_backfill_exact")

    def test_rerun_upserts_without_duplicating(self):
        with contextlib.redirect_stdout(io.StringIO()):
            be.main(["--scores", str(SCORES_CSV)])
            be.main(["--scores", str(SCORES_CSV)])
        with open(self.tmp / "nba" / "games_2023-24.csv", newline="") as f:
            ids = [r["game_id"] for r in csv.DictReader(f)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_dry_run_writes_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            be.main(["--scores", str(SCORES_CSV), "--odds", str(ODDS_CSV), "--dry-run"])
        self.assertFalse((self.tmp / "nba").exists())
        self.assertFalse((self.tmp / "odds").exists())


if __name__ == "__main__":
    unittest.main()
