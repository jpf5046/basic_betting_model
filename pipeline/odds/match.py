#!/usr/bin/env python3
"""Match odds events to our games — the durable game_id <-> event_id join.

Matching key, per the design discussion: sport + home team + away team +
date, with start-time proximity used ONLY to break ties (MLB
doubleheaders: same teams, same day, two games). Works identically for
future events, live odds, and historical snapshots — anything that
normalizes to (event_id, home_team_id, away_team_id, commence_time).

Matches persist in data/odds/<sport>/event_map.csv (idempotent upsert by
event_id), so odds pulled later — including historic snapshots pulled
long after the game — join to games through this one file.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline.ingest.core import EASTERN, REPO_ROOT, Game

ODDS_DIR = REPO_ROOT / "data" / "odds"

# Same-day matching tolerance: an event whose Eastern date is within one
# day of the game date is a candidate (late tips cross UTC midnight; the
# leagues' own gameDate handling makes true off-by-one rare).
DATE_SLOP_DAYS = 1

# Doubleheader tie-break: nearest start time wins, but only if it's
# within this window — otherwise the match is ambiguous and skipped.
TIME_TIEBREAK_HOURS = 4

MAP_FIELDS = ["event_id", "game_id", "sport", "commence_time", "matched_on", "matched_at"]


@dataclass
class MatchResult:
    matched: list[dict]      # event_map rows
    unmatched: list[dict]    # events we couldn't place (no candidate game)
    ambiguous: list[dict]    # >1 candidate and no usable time tie-break


def _eastern_date(iso_utc: str) -> str:
    if not iso_utc:
        return ""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    return dt.astimezone(EASTERN).date().isoformat()


def _abs_hours(iso_a: str, iso_b: str) -> float | None:
    if not iso_a or not iso_b:
        return None
    a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
    b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
    return abs((a - b).total_seconds()) / 3600.0


def match_events(event_rows: list[dict], games: list[Game], sport: str) -> MatchResult:
    """event_rows: normalized rows (one per event is enough — extra
    market rows are deduped by event_id here)."""
    events: dict[str, dict] = {}
    for r in event_rows:
        if r["event_id"] and r["event_id"] not in events:
            events[r["event_id"]] = r

    by_pair: dict[tuple[str, str], list[Game]] = {}
    for g in games:
        by_pair.setdefault((g.home_team_id, g.away_team_id), []).append(g)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = MatchResult([], [], [])

    for ev in events.values():
        if not (ev["home_team_id"] and ev["away_team_id"]):
            out.unmatched.append(ev)  # unmapped team names — reported upstream
            continue
        ev_date = _eastern_date(ev["commence_time"])
        candidates = [
            g for g in by_pair.get((ev["home_team_id"], ev["away_team_id"]), [])
            if not ev_date or not g.date
            or abs(datetime.fromisoformat(g.date).toordinal()
                   - datetime.fromisoformat(ev_date).toordinal()) <= DATE_SLOP_DAYS
        ]

        if not candidates:
            out.unmatched.append(ev)
            continue
        if len(candidates) == 1:
            game, matched_on = candidates[0], "teams+date"
        else:
            # Doubleheader (or slop overlap): nearest start time decides.
            timed = [(g, _abs_hours(ev["commence_time"], g.start_time_utc))
                     for g in candidates]
            timed = [(g, h) for g, h in timed if h is not None]
            timed.sort(key=lambda t: t[1])
            if not timed or timed[0][1] > TIME_TIEBREAK_HOURS or (
                    len(timed) > 1 and abs(timed[0][1] - timed[1][1]) < 1e-9):
                out.ambiguous.append(ev)
                continue
            game, matched_on = timed[0][0], "teams+time"

        out.matched.append({
            "event_id": ev["event_id"], "game_id": game.game_id, "sport": sport,
            "commence_time": ev["commence_time"], "matched_on": matched_on,
            "matched_at": now,
        })
    return out


# ------------------------------------------------------------ persistence

def event_map_path(sport: str) -> Path:
    return ODDS_DIR / sport.lower() / "event_map.csv"


def load_event_map(sport: str) -> dict[str, dict]:
    path = event_map_path(sport)
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {r["event_id"]: r for r in csv.DictReader(f)}


def upsert_event_map(sport: str, rows: list[dict]) -> Path:
    """Insert/update by event_id; existing matches for other events are kept."""
    current = load_event_map(sport)
    for r in rows:
        current[r["event_id"]] = {k: r[k] for k in MAP_FIELDS}
    path = event_map_path(sport)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MAP_FIELDS)
        w.writeheader()
        w.writerows(sorted(current.values(), key=lambda r: (r["commence_time"], r["event_id"])))
    return path
