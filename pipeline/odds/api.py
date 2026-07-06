#!/usr/bin/env python3
"""The Odds API v4 client (https://the-odds-api.com/liveapi/guides/v4/).

Key handling: reads ODDS_API_KEY from the environment (or an explicit
--api-key). The key is sent only as a query parameter to the API; cached
raw files never contain it, and it is never printed.

Endpoints wrapped:
  * GET /v4/sports                                — all sport keys (incl.
    ones we have no ingester for, e.g. soccer_fifa_world_cup)
  * GET /v4/sports/{key}/events                   — upcoming events, cheap
  * GET /v4/sports/{key}/odds                     — live/upcoming odds
  * GET /v4/historical/sports/{key}/odds?date=…   — snapshot at a past
    instant (paid plans); response wraps the same event list in {"data":…}

Every response is cached verbatim under data/raw/odds/ before parsing
(PLAN.md §2 raw-first rule), and the quota headers the API returns
(x-requests-remaining / x-requests-used) are surfaced after each call so
you always know where the plan stands.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from pipeline.ingest.core import REPO_ROOT

BASE_URL = "https://api.the-odds-api.com/v4"
RAW_DIR = REPO_ROOT / "data" / "raw" / "odds"

# our sport -> The Odds API sport key
SPORT_KEYS = {
    "MLB": "baseball_mlb",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
    "WNBA": "basketball_wnba",
}

DEFAULT_MARKETS = "h2h,totals"
DEFAULT_REGIONS = "us"


class OddsAPIError(RuntimeError):
    pass


def api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("ODDS_API_KEY", "")
    if not key:
        sys.exit("no API key: set ODDS_API_KEY in the environment (or pass --api-key)")
    return key


def _get(path: str, params: dict, cache_name: str) -> tuple[object, dict]:
    """GET BASE_URL+path, cache the body, return (parsed_json, quota)."""
    query = urllib.parse.urlencode(params)
    url = f"{BASE_URL}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "basic-betting-model/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            headers = resp.headers
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise OddsAPIError(f"The Odds API returned {e.code} for {path}: {detail}") from e
    except urllib.error.URLError as e:
        raise OddsAPIError(f"could not reach The Odds API: {e}") from e

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (RAW_DIR / f"{cache_name}.{stamp}.json").write_bytes(body)

    quota = {
        "remaining": headers.get("x-requests-remaining", "?"),
        "used": headers.get("x-requests-used", "?"),
    }
    return json.loads(body), quota


def get_sports(key: str, include_inactive: bool = False) -> tuple[list, dict]:
    params = {"apiKey": key}
    if include_inactive:
        params["all"] = "true"
    return _get("/sports", params, "sports")


def get_events(key: str, sport_key: str) -> tuple[list, dict]:
    """Upcoming events (no odds) — works for any sport key, ingester or not."""
    return _get(f"/sports/{sport_key}/events", {"apiKey": key}, f"events.{sport_key}")


def get_odds(key: str, sport_key: str, markets: str = DEFAULT_MARKETS,
             regions: str = DEFAULT_REGIONS) -> tuple[list, dict]:
    params = {"apiKey": key, "markets": markets, "regions": regions,
              "oddsFormat": "american", "dateFormat": "iso"}
    return _get(f"/sports/{sport_key}/odds", params, f"odds.{sport_key}")


def get_historical_odds(key: str, sport_key: str, at_iso: str,
                        markets: str = DEFAULT_MARKETS,
                        regions: str = DEFAULT_REGIONS) -> tuple[dict, dict]:
    """Snapshot of the odds as they stood at `at_iso` (paid plans).
    Returns the wrapper dict: {timestamp, previous_timestamp,
    next_timestamp, data: [events]}."""
    params = {"apiKey": key, "date": at_iso, "markets": markets,
              "regions": regions, "oddsFormat": "american", "dateFormat": "iso"}
    return _get(f"/historical/sports/{sport_key}/odds", params,
                f"historical-odds.{sport_key}")


def latest_cached(prefix: str) -> Path | None:
    """Newest cached raw file for a given cache_name prefix, if any."""
    cached = sorted(RAW_DIR.glob(f"{prefix}.*.json"))
    return cached[-1] if cached else None
