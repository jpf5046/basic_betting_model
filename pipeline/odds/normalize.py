#!/usr/bin/env python3
"""Normalize The Odds API events into flat rows keyed by our team ids.

The API names teams with full names ("New York Liberty", "Boston Red
Sox") which match data/teams.csv `name` for the big leagues; NAME_ALIASES
absorbs the known spelling differences. An event whose teams can't be
mapped is kept (raw names, blank team ids) and reported — odds are
enrichment, so unmatched is a warning, never a crash. New aliases get
added here as they're discovered.

One normalized row per (event, bookmaker, market, outcome):
  snapshot_ts, event_id, sport, commence_time, home_team_id, away_team_id,
  home_name, away_name, bookmaker, market (h2h|totals),
  outcome (team_id | OVER | UNDER), price_american, point
"""

from __future__ import annotations

import csv

from pipeline.ingest.core import TEAMS_CSV

# The Odds API name -> data/teams.csv name, where they differ.
NAME_ALIASES = {
    "Los Angeles Clippers": "LA Clippers",
    "Oakland Athletics": "Athletics",
    "St Louis Cardinals": "St. Louis Cardinals",
    "St Louis Blues": "St. Louis Blues",
    "Montréal Canadiens": "Montreal Canadiens",
    "Utah Hockey Club": "Utah Mammoth",
    # Franchise relocated after the 2023-24 season; historic odds/scores
    # exports still use the old name.
    "Arizona Coyotes": "Utah Mammoth",
}

ROW_FIELDS = ["snapshot_ts", "event_id", "sport", "commence_time",
              "home_team_id", "away_team_id", "home_name", "away_name",
              "bookmaker", "market", "outcome", "price_american", "point"]


def load_name_map(sport: str) -> dict[str, str]:
    """Full team name (incl. aliases) -> team_id for one sport."""
    with open(TEAMS_CSV, newline="") as f:
        by_name = {r["name"]: r["team_id"] for r in csv.DictReader(f)
                   if r["sport"] == sport}
    out = dict(by_name)
    for alias, canonical in NAME_ALIASES.items():
        if canonical in by_name:
            out.setdefault(alias, by_name[canonical])
    return out


def normalize_events(doc: object, sport: str, snapshot_ts: str,
                     name_map: dict[str, str] | None = None
                     ) -> tuple[list[dict], list[str]]:
    """Flatten an odds response (a plain event list, or the historical
    endpoint's {"data": [...]} wrapper) into rows.

    Returns (rows, unmatched_team_names). Events without bookmakers (the
    /events endpoint) still yield one identity row per event with
    market="", so matching can run on odds-less fetches too.
    """
    if isinstance(doc, dict) and "data" in doc:
        snapshot_ts = doc.get("timestamp") or snapshot_ts
        events = doc["data"]
    else:
        events = doc
    if not isinstance(events, list):
        raise RuntimeError(
            "unexpected odds response shape (expected an event list or a "
            "{'data': [...]} wrapper) — inspect the cached raw file in data/raw/odds/"
        )

    name_map = name_map if name_map is not None else load_name_map(sport)
    rows: list[dict] = []
    unmatched: set[str] = set()

    for ev in events:
        home_name, away_name = ev.get("home_team", ""), ev.get("away_team", "")
        home_id = name_map.get(home_name, "")
        away_id = name_map.get(away_name, "")
        for name, mapped in ((home_name, home_id), (away_name, away_id)):
            if name and not mapped:
                unmatched.add(name)
        base = {
            "snapshot_ts": snapshot_ts,
            "event_id": ev.get("id", ""),
            "sport": sport,
            "commence_time": ev.get("commence_time", ""),
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_name": home_name,
            "away_name": away_name,
        }
        bookmakers = ev.get("bookmakers") or []
        if not bookmakers:
            rows.append({**base, "bookmaker": "", "market": "",
                         "outcome": "", "price_american": "", "point": ""})
            continue
        for bk in bookmakers:
            for market in bk.get("markets") or []:
                mkey = market.get("key", "")
                for oc in market.get("outcomes") or []:
                    name = oc.get("name", "")
                    if mkey == "h2h":
                        outcome = name_map.get(name, name)  # team_id if known
                    elif mkey == "totals":
                        outcome = name.upper()  # OVER / UNDER
                    else:
                        outcome = name
                    rows.append({
                        **base,
                        "bookmaker": bk.get("key", ""),
                        "market": mkey,
                        "outcome": outcome,
                        "price_american": oc.get("price", ""),
                        "point": oc.get("point", ""),
                    })
    return rows, sorted(unmatched)
