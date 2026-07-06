#!/usr/bin/env python3
"""Grader — PLAN.md §7 stage 2: score published picks against final scores.

Every pick is graded WIN / LOSS / PUSH at a flat $100 stake (+100 / −100 /
0 — no odds in the system yet, matching my_model.md's grading convention),
or resolved as one of two non-bet outcomes:

  VOID    — the game was postponed / suspended / cancelled: stake returned,
            $0, counted separately from the record
  PENDING — the game isn't final yet (still scheduled or live, or the
            games CSV hasn't been refreshed): not graded, run again after
            the next fetch

Rules:
  * Moneyline: the pick wins if the selected team scored more. NHL OT/SO
    finals count as plain wins — the final score already names the winner.
    A tied final (impossible in these leagues, defensive only) is a PUSH.
  * Totals: actual combined score vs the pick's line. Strictly over the
    line wins an OVER, strictly under wins an UNDER, exactly equal is a
    PUSH (possible on whole-number lines like NBA 225, impossible on
    fractional ones like MLB 9.2).
  * Doubleheaders need no special handling here: picks reference the
    unique per-game id (MLB gamePk), so game 1 and game 2 grade
    independently.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, fields
from pathlib import Path

from pipeline.ingest.core import Game

STAKE = 100  # flat stake per pick, dollars

NON_BET_STATUSES = ("postponed", "suspended", "cancelled")


@dataclass
class Grade:
    """One graded pick — the pick's fields plus the outcome."""

    game_id: str
    sport: str
    date: str
    bet_type: str      # ML | TOTAL
    selection: str     # team_id or OVER/UNDER
    confidence: str
    line: str
    game_status: str   # the game status the grade was based on
    actual_away: str   # final scores ("" unless final)
    actual_home: str
    result: str        # WIN | LOSS | PUSH | VOID | PENDING
    pnl: int           # +100 / -100 / 0


GRADE_FIELDS = [f.name for f in fields(Grade)]


def grade_pick(pick: dict, game: Game | None) -> Grade:
    """Grade one pick row (a picks CSV dict) against its game."""
    base = dict(
        game_id=pick["game_id"], sport=pick["sport"], date=pick["date"],
        bet_type=pick["bet_type"], selection=pick["selection"],
        confidence=pick.get("confidence", ""), line=pick.get("line", ""),
    )

    if game is None:
        # The pick's game vanished from the games CSV — treat as pending
        # and let a later fetch resolve it; never guess.
        return Grade(**base, game_status="missing", actual_away="",
                     actual_home="", result="PENDING", pnl=0)

    if game.status in NON_BET_STATUSES:
        return Grade(**base, game_status=game.status, actual_away="",
                     actual_home="", result="VOID", pnl=0)

    if game.status != "final" or not (game.away_score and game.home_score):
        return Grade(**base, game_status=game.status, actual_away="",
                     actual_home="", result="PENDING", pnl=0)

    away, home = int(game.away_score), int(game.home_score)

    if pick["bet_type"] == "ML":
        if away == home:  # defensive: no ties in these leagues
            result = "PUSH"
        else:
            winner = game.away_team_id if away > home else game.home_team_id
            result = "WIN" if pick["selection"] == winner else "LOSS"
    elif pick["bet_type"] == "TOTAL":
        total, line = away + home, float(pick["line"])
        if total == line:
            result = "PUSH"
        elif pick["selection"] == "OVER":
            result = "WIN" if total > line else "LOSS"
        else:  # UNDER
            result = "WIN" if total < line else "LOSS"
    else:
        raise ValueError(f"unknown bet_type {pick['bet_type']!r} on game {pick['game_id']}")

    pnl = {"WIN": STAKE, "LOSS": -STAKE, "PUSH": 0}[result]
    return Grade(**base, game_status=game.status, actual_away=str(away),
                 actual_home=str(home), result=result, pnl=pnl)


def grade_picks(picks: list[dict], games: list[Game]) -> list[Grade]:
    by_id = {g.game_id: g for g in games}
    return [grade_pick(p, by_id.get(p["game_id"])) for p in picks]


def summarize(grades: list[Grade]) -> dict:
    """Record and P&L, overall and by bet type (graded bets only —
    VOID/PENDING are reported but never in the record)."""

    def bucket(rows: list[Grade]) -> dict:
        return {
            "wins": sum(1 for g in rows if g.result == "WIN"),
            "losses": sum(1 for g in rows if g.result == "LOSS"),
            "pushes": sum(1 for g in rows if g.result == "PUSH"),
            "voids": sum(1 for g in rows if g.result == "VOID"),
            "pending": sum(1 for g in rows if g.result == "PENDING"),
            "pnl": sum(g.pnl for g in rows),
        }

    out = {"overall": bucket(grades)}
    for bt in sorted({g.bet_type for g in grades}):
        out[bt] = bucket([g for g in grades if g.bet_type == bt])
    return out


def read_picks_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_grades_csv(grades: list[Grade], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=GRADE_FIELDS)
        w.writeheader()
        w.writerows(vars(g) for g in grades)
    return path
