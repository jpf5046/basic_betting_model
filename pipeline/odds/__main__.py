#!/usr/bin/env python3
"""Odds CLI (The Odds API v4; set ODDS_API_KEY in the environment).

    python3 -m pipeline.odds sports [--all]
    python3 -m pipeline.odds events --sport-key soccer_fifa_world_cup
    python3 -m pipeline.odds fetch --sport WNBA [--historical 2026-07-05T12:00:00Z]
    python3 -m pipeline.odds match --sport WNBA
    python3 -m pipeline.odds edge --sport WNBA --date 2026-07-05

fetch = pull odds (live, or a historical snapshot on paid plans), cache
raw, write a normalized snapshot CSV, and sync the game_id <-> event_id
map against the sport's games CSV. `events` works for ANY sport key —
including sports we have no ingester for — and costs little/no quota.
`edge` joins a day's predictions to the latest matched odds.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone

from pipeline.ingest.core import REPO_ROOT
from pipeline.odds import api
from pipeline.odds.edge import EDGE_FIELDS, edge_rows
from pipeline.odds.match import load_event_map, match_events, upsert_event_map
from pipeline.odds.normalize import normalize_events
from pipeline.odds.store import load_snapshot_rows, write_snapshot

PREDICTIONS_DIR = REPO_ROOT / "data" / "predictions"


def _games(sport: str, season: str | None, on=None):
    from datetime import date as date_cls
    from pipeline.features.context import ADAPTERS, default_season
    from pipeline.ingest import core
    on = on or datetime.now(timezone.utc).date()
    if not isinstance(on, date_cls):
        on = date_cls.fromisoformat(str(on))
    season = season or default_season(sport, on)
    path = ADAPTERS[sport].games_csv_path(season)
    if not path.exists():
        return []
    return core.read_games_csv(path, f"python3 -m pipeline.ingest.{sport.lower()} fetch")


def _sync_matches(sport: str, rows: list[dict], season: str | None) -> None:
    games = _games(sport, season)
    if not games:
        print(f"note: no games CSV for {sport} yet — odds cached, matching skipped "
              f"(run the ingester fetch, then `python3 -m pipeline.odds match --sport {sport}`)")
        return
    result = match_events(rows, games, sport)
    if result.matched:
        path = upsert_event_map(sport, result.matched)
        print(f"matched {len(result.matched)} events -> {path}")
    for ev in result.unmatched:
        who = f"{ev['away_name']} @ {ev['home_name']}"
        print(f"  unmatched: {who} ({ev['commence_time']}) — no candidate game")
    for ev in result.ambiguous:
        print(f"  AMBIGUOUS: {ev['away_name']} @ {ev['home_name']} "
              f"({ev['commence_time']}) — multiple candidates, no usable time tie-break")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.odds", description=__doc__)
    p.add_argument("--api-key", help="overrides the ODDS_API_KEY env var")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sports", help="list The Odds API sport keys")
    s.add_argument("--all", action="store_true", help="include out-of-season sports")

    e = sub.add_parser("events", help="upcoming events for any sport key (no ingester needed)")
    e.add_argument("--sport-key", required=True, help="e.g. soccer_fifa_world_cup")

    f = sub.add_parser("fetch", help="pull odds, cache, normalize, and sync the event map")
    f.add_argument("--sport", required=True, choices=sorted(api.SPORT_KEYS))
    f.add_argument("--markets", default=api.DEFAULT_MARKETS)
    f.add_argument("--regions", default=api.DEFAULT_REGIONS)
    f.add_argument("--historical", metavar="ISO",
                   help="pull the snapshot as of this instant (paid plans), e.g. for "
                        "matching historic odds to historic games")
    f.add_argument("--season", help="games CSV season key for matching")

    m = sub.add_parser("match", help="re-run matching from already-cached odds")
    m.add_argument("--sport", required=True, choices=sorted(api.SPORT_KEYS))
    m.add_argument("--season")

    g = sub.add_parser("edge", help="model vs market for one day's predictions")
    g.add_argument("--sport", required=True, choices=sorted(api.SPORT_KEYS))
    g.add_argument("--date", required=True, help="YYYY-MM-DD of the predictions run")

    args = p.parse_args(argv)

    if args.cmd == "sports":
        sports, quota = api.get_sports(api.api_key(args.api_key), include_inactive=args.all)
        for row in sports:
            flag = "" if row.get("active") else "  (inactive)"
            print(f"{row['key']:<40} {row.get('title', '')}{flag}")
        print(f"\nquota: {quota['remaining']} requests remaining")
        return

    if args.cmd == "events":
        events, quota = api.get_events(api.api_key(args.api_key), args.sport_key)
        for ev in events:
            print(f"{ev.get('commence_time', '')}  {ev.get('away_team', ''):>28} @ "
                  f"{ev.get('home_team', '')}")
        print(f"\n{len(events)} upcoming events; quota: {quota['remaining']} remaining")
        return

    if args.cmd == "fetch":
        key = api.api_key(args.api_key)
        sport_key = api.SPORT_KEYS[args.sport]
        if args.historical:
            doc, quota = api.get_historical_odds(key, sport_key, args.historical,
                                                 args.markets, args.regions)
        else:
            doc, quota = api.get_odds(key, sport_key, args.markets, args.regions)
        snapshot_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows, unmatched_names = normalize_events(doc, args.sport, snapshot_ts)
        if unmatched_names:
            print(f"unmapped team names (add to NAME_ALIASES?): {unmatched_names}")
        snap_ts = rows[0]["snapshot_ts"] if rows else snapshot_ts
        path = write_snapshot(args.sport, rows, snap_ts)
        events_n = len({r['event_id'] for r in rows})
        print(f"wrote {len(rows)} odds rows ({events_n} events) -> {path}")
        _sync_matches(args.sport, rows, args.season)
        print(f"quota: {quota['remaining']} requests remaining ({quota['used']} used)")
        return

    if args.cmd == "match":
        rows = load_snapshot_rows(args.sport)
        if not rows:
            sys.exit(f"no cached snapshots under data/odds/{args.sport.lower()}/ — "
                     f"run `python3 -m pipeline.odds fetch --sport {args.sport}` first")
        _sync_matches(args.sport, rows, args.season)
        return

    if args.cmd == "edge":
        pred_path = PREDICTIONS_DIR / args.sport.lower() / f"predictions_{args.date}.csv"
        if not pred_path.exists():
            sys.exit(f"{pred_path} not found — run `python3 -m pipeline.models predict "
                     f"--sport {args.sport} --date {args.date}` first")
        with open(pred_path, newline="") as fh:
            predictions = [r for r in csv.DictReader(fh) if r["status"] == "ok"]
        emap = load_event_map(args.sport)
        by_game = {r["game_id"]: r["event_id"] for r in emap.values()}
        snapshot_rows = load_snapshot_rows(args.sport)

        all_rows: list[dict] = []
        for pred in predictions:
            event_id = by_game.get(pred["game_id"])
            if not event_id:
                print(f"{pred['away_team_id']} @ {pred['home_team_id']}: no matched odds event")
                continue
            for row in edge_rows(pred, event_id, snapshot_rows):
                all_rows.append(row)
                if row["bet_type"] == "ML":
                    print(f"{row['date']}  ML    {row['selection']:<10} model {row['model_prob']:.2f} "
                          f"vs implied {row['implied_prob']:.2f} @ {row['market_price']:g} "
                          f"({row['books']} books)  edge {row['edge']:+.3f}  "
                          f"EV ${row['ev_per_100']:+.2f}/100")
                else:
                    print(f"{row['date']}  TOTAL {row['selection']:<10} model {row['model_total']} "
                          f"vs market {row['market_point']:g} @ {row['market_price']:g} "
                          f"({row['books']} books)  {row['edge']:+.2f} units off the line")

        out = pred_path.with_name(f"edges_{args.date}.csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=EDGE_FIELDS)
            w.writeheader()
            w.writerows(all_rows)
        print(f"\n{len(all_rows)} edge rows -> {out}")
        return


if __name__ == "__main__":
    main()
