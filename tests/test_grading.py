"""Offline tests for the grader (pipeline/grading/).

Games are constructed directly — grading is pure logic over picks + finals.
From the repo root:  python3 -m unittest tests.test_grading -v
"""
import unittest

from pipeline.grading.grader import grade_pick, grade_picks, summarize
from pipeline.ingest.core import Game


def make_game(game_id="g1", status="final", away=5, home=3, sport_prefix="mlb",
              away_team="mlb-nyy", home_team="mlb-bos", ot="", doubleheader="N",
              game_number="1"):
    return Game(
        game_id=game_id, season="2026", season_type="regular", date="2026-07-05",
        start_time_utc="", status=status,
        away_team_id=away_team, home_team_id=home_team,
        away_abbrev="", home_abbrev="",
        away_score=str(away) if status == "final" else "",
        home_score=str(home) if status == "final" else "",
        venue="", ot=ot, doubleheader=doubleheader, game_number=game_number,
    )


def make_pick(game_id="g1", bet_type="ML", selection="mlb-nyy",
              confidence="STRONG", line=""):
    return {"game_id": game_id, "sport": "MLB", "date": "2026-07-05",
            "bet_type": bet_type, "selection": selection,
            "confidence": confidence, "line": line}


class TestMoneyline(unittest.TestCase):
    def test_win_and_loss(self):
        game = make_game(away=5, home=3)  # NYY wins on the road
        win = grade_pick(make_pick(selection="mlb-nyy"), game)
        loss = grade_pick(make_pick(selection="mlb-bos"), game)
        self.assertEqual((win.result, win.pnl), ("WIN", 100))
        self.assertEqual((loss.result, loss.pnl), ("LOSS", -100))
        self.assertEqual((win.actual_away, win.actual_home), ("5", "3"))

    def test_nhl_overtime_final_counts_as_plain_win(self):
        game = make_game(away=2, home=3, away_team="nhl-nyr", home_team="nhl-bos", ot="SO")
        grade = grade_pick(make_pick(selection="nhl-bos"), game)
        self.assertEqual((grade.result, grade.pnl), ("WIN", 100))

    def test_defensive_tie_is_push(self):
        game = make_game(away=4, home=4)
        grade = grade_pick(make_pick(selection="mlb-nyy"), game)
        self.assertEqual((grade.result, grade.pnl), ("PUSH", 0))


class TestTotals(unittest.TestCase):
    def test_over_win_loss(self):
        win = grade_pick(make_pick(bet_type="TOTAL", selection="OVER", line="9.2"),
                         make_game(away=6, home=5))   # total 11 > 9.2
        loss = grade_pick(make_pick(bet_type="TOTAL", selection="OVER", line="9.2"),
                          make_game(away=4, home=3))  # total 7 < 9.2
        self.assertEqual((win.result, win.pnl), ("WIN", 100))
        self.assertEqual((loss.result, loss.pnl), ("LOSS", -100))

    def test_under_win_loss(self):
        win = grade_pick(make_pick(bet_type="TOTAL", selection="UNDER", line="7.8"),
                         make_game(away=3, home=2))   # total 5 < 7.8
        loss = grade_pick(make_pick(bet_type="TOTAL", selection="UNDER", line="7.8"),
                          make_game(away=6, home=4))  # total 10 > 7.8
        self.assertEqual((win.result, win.pnl), ("WIN", 100))
        self.assertEqual((loss.result, loss.pnl), ("LOSS", -100))

    def test_whole_number_line_pushes(self):
        # NBA-style 225 line, game lands exactly on it — both sides push.
        game = make_game(away=113, home=112, away_team="nba-bos", home_team="nba-nyk")
        over = grade_pick(make_pick(bet_type="TOTAL", selection="OVER", line="225"), game)
        under = grade_pick(make_pick(bet_type="TOTAL", selection="UNDER", line="225"), game)
        self.assertEqual((over.result, over.pnl), ("PUSH", 0))
        self.assertEqual((under.result, under.pnl), ("PUSH", 0))


class TestNonBetOutcomes(unittest.TestCase):
    def test_postponed_suspended_cancelled_void(self):
        for status in ("postponed", "suspended", "cancelled"):
            grade = grade_pick(make_pick(), make_game(status=status))
            self.assertEqual((grade.result, grade.pnl), ("VOID", 0), status)
            self.assertEqual(grade.game_status, status)

    def test_not_final_yet_pending(self):
        for status in ("scheduled", "live"):
            grade = grade_pick(make_pick(), make_game(status=status))
            self.assertEqual((grade.result, grade.pnl), ("PENDING", 0), status)

    def test_missing_game_pending_never_guessed(self):
        grade = grade_pick(make_pick(game_id="gone"), None)
        self.assertEqual((grade.result, grade.game_status), ("PENDING", "missing"))

    def test_unknown_bet_type_is_loud(self):
        with self.assertRaises(ValueError):
            grade_pick(make_pick(bet_type="SPREAD"), make_game())


class TestDoubleheader(unittest.TestCase):
    def test_both_games_grade_independently(self):
        g1 = make_game(game_id="dh1", away=3, home=5, away_team="mlb-bal",
                       home_team="mlb-bos", doubleheader="Y", game_number="1")
        g2 = make_game(game_id="dh2", away=8, home=2, away_team="mlb-bal",
                       home_team="mlb-bos", doubleheader="Y", game_number="2")
        picks = [make_pick(game_id="dh1", selection="mlb-bal"),
                 make_pick(game_id="dh2", selection="mlb-bal")]
        grades = grade_picks(picks, [g1, g2])
        self.assertEqual([g.result for g in grades], ["LOSS", "WIN"])


class TestSummary(unittest.TestCase):
    def test_record_and_pnl(self):
        games = [
            make_game(game_id="a", away=5, home=3),
            make_game(game_id="b", away=2, home=6),
            make_game(game_id="c", status="postponed"),
            make_game(game_id="d", status="scheduled"),
        ]
        picks = [
            make_pick(game_id="a", selection="mlb-nyy"),                        # WIN
            make_pick(game_id="a", bet_type="TOTAL", selection="OVER", line="9.2"),  # 8 < 9.2 LOSS
            make_pick(game_id="b", selection="mlb-nyy"),                        # LOSS
            make_pick(game_id="c", selection="mlb-nyy"),                        # VOID
            make_pick(game_id="d", selection="mlb-nyy"),                        # PENDING
        ]
        s = summarize(grade_picks(picks, games))
        self.assertEqual(s["overall"], {"wins": 1, "losses": 2, "pushes": 0,
                                        "voids": 1, "pending": 1, "pnl": -100})
        self.assertEqual(s["ML"]["wins"], 1)
        self.assertEqual(s["TOTAL"]["losses"], 1)


if __name__ == "__main__":
    unittest.main()
