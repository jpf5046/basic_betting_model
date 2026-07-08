# HOWTO — running the pipeline

Everything that exists in this repo today and how to run it, in the order the
daily orchestrator (`python3 run_daily.py`) runs it: **ingest → inspect →
features → predict → odds → grade → backfill/database (optional)**.
For the architecture and what's coming next, see [`PLAN.md`](PLAN.md). New
here? Start at the root [`README.md`](README.md).

## Prerequisites

- Python **3.11+**, standard library only — there is nothing to `pip install`
  for the pipeline itself. All storage is flat CSVs under `data/`; no database
  is required to run any command below.
- Optional: `export DATABASE_URL=postgresql://...` mirrors the CSVs into
  PostgreSQL instead of the default local SQLite file — see §8 below. Needs
  `pip install "psycopg[binary]"`, the one optional dependency.
- Run every command **from the repo root**.
- `fetch` commands need normal internet access (they hit the leagues' public,
  key-less APIs). Everything else — queries, features, predictions, tests —
  works offline from previously fetched files.
- Odds commands additionally need `export ODDS_API_KEY=...` (The Odds API,
  https://the-odds-api.com). The key is read from the environment only —
  never commit it.

The four sports and their sources:

| Sport | Module | Source | Season key |
|---|---|---|---|
| WNBA | `pipeline.ingest.wnba` | cdn.wnba.com schedule feed (1 GET) | `2026` |
| MLB  | `pipeline.ingest.mlb`  | statsapi.mlb.com season schedule (1 GET) | `2026` |
| NBA  | `pipeline.ingest.nba`  | cdn.nba.com schedule feed (1 GET) | `2025-26` |
| NHL  | `pipeline.ingest.nhl`  | api-web.nhle.com per-club schedules (32 GETs) | `20252026` |

Season keys default to the current season; override with `--season` on any
command. Every command below also accepts `--help`.

---

## 0. Sanity-check the team registry

```bash
python3 scripts/validate_teams.py
# OK: 275 teams (CFB=136, MLB=30, NBA=30, NFL=32, NHL=32, WNBA=15), all checks passed
```

`data/teams.csv` is the canonical team registry (ids, conference/division,
venue coordinates, timezones, native API ids). Everything downstream keys off
its `team_id` values (`wnba-nyl`, `mlb-nyy`, …). Run the validator after any
hand edit.

## 1. Fetch a sport's season (schedule + scores)

```bash
python3 -m pipeline.ingest.wnba fetch
python3 -m pipeline.ingest.mlb  fetch
python3 -m pipeline.ingest.nba  fetch
python3 -m pipeline.ingest.nhl  fetch     # 32 requests, ~15s with politeness delays
```

Each fetch:

1. downloads the raw feed(s) to `data/raw/<sport>/…json` (gitignored —
   re-parsing never re-hits the API; pass `--offline` to reuse the newest
   cached files instead of downloading);
2. normalizes every game into `data/<sport>/games_<season>.csv` — one row per
   game with our canonical team ids, status, scores on finals, and
   sport-specific columns (MLB doubleheaders, NHL OT/SO).

One fetch per day is plenty: the feed contains the whole season, so
yesterday's finals and today's slate arrive together.

**If a fetch errors:** an "unexpected feed shape" message means the league
changed its JSON — the raw file it points at is what's needed to fix the
parser. An "unmapped team" error means `data/teams.csv` needs the missing id
or alias (deliberately loud, never silent).

## 2. Inspect what was ingested

```bash
python3 -m pipeline.ingest.mlb today                      # today's slate (US/Eastern)
python3 -m pipeline.ingest.mlb today --date 2026-07-04    # any date
python3 -m pipeline.ingest.mlb scores --team NYY --last 10
python3 -m pipeline.ingest.mlb common-opponents NYY BOS   # my_model method-2 inputs
```

Same three commands on every sport module. Teams are named by the abbrevs in
`data/teams.csv` (`NYL`, `NYY`, `BOS`, `TOR`, …). `scores` covers completed
regular-season games only — preseason, All-Star, postponed, and (NBA) Cup-final
games are deliberately excluded from logs.

## 3. Build a feature frame

```bash
python3 -m pipeline.features list                     # what's registered
python3 -m pipeline.features build --sport WNBA --date 2026-07-05
# -> data/features/wnba/features_2026-07-05.csv  (one wide row per game)
```

The frame computes **every registered feature** for both sides of each game on
that date's slate — season scoring, common opponents, last-10 form,
head-to-head, games played, home advantage, travel km — using only games
completed **before** that date (point-in-time; a backtest and a live run see
identical values). Blank cells mean "no data", never a made-up number.

To add a feature: drop one file into `pipeline/features/defs/` with an
`@register(...)` function (copy `travel_km.py` as the template). It appears in
`list`, the frame, and any model that asks for it — nothing else changes.

## 4. Run the model — predictions and the pick sheet

```bash
python3 -m pipeline.models list
python3 -m pipeline.models predict --sport WNBA --date 2026-07-05
# 2026-07-05   NYL @ LVA  pred 86-87 (LVA p=0.54, LOW) | ML: TOSS-UP | TOTAL: OVER 168
```

Outputs land in `data/predictions/<sport>/`:

- `predictions_<date>.csv` — **every** game: exact and display scores, win
  probability, HIGH/MEDIUM/LOW data confidence, and the leans, including
  TOSS-UP / PASS / `no_prediction` rows (sitting out is an output);
- `picks_<date>.csv` — the published pick sheet only (moneyline picks with
  STRONG/LEAN, totals picks with the threshold that fired).

### Tuning a config

Factory defaults (the `my_model.md` numbers) live in
`pipeline/models/config.py`. To experiment, write a JSON file with only the
values you want to change and pass it in:

```bash
cat > my-tweaks.json <<'EOF'
{"k": 0.20, "ml_gates": {"lean": 0.56}, "totals": {"over": 170.0}}
EOF
python3 -m pipeline.models validate-config my-tweaks.json --sport WNBA
python3 -m pipeline.models predict --sport WNBA --config my-tweaks.json
```

Overrides deep-merge over the defaults; validation enforces the invariants
(weights sum to 1.000, strong > lean > 0.5, over > under) and lists every
violation at once.

## 5. Pull odds and find the edge

The model predicts **odds-blind** on purpose — these commands join the market
on afterward, so the edge (model vs market) is measurable:

```bash
export ODDS_API_KEY=your-key
python3 -m pipeline.odds fetch --sport WNBA        # odds + auto-match to our games
python3 -m pipeline.odds edge  --sport WNBA --date 2026-07-05
# ML    wnba-lva  model 0.54 vs implied 0.57 @ -130 (3 books)  edge -0.025  EV $-4.46/100
# TOTAL OVER      model 172.85 vs market 165.5 @ -110 (3 books)  +7.35 units off the line
```

What `fetch` does: pulls h2h + totals odds, caches the raw response
(`data/raw/odds/`), normalizes to one row per event/bookmaker/market/outcome
keyed by our team ids (`data/odds/<sport>/snapshots_*.csv`), and **syncs the
event map** — `data/odds/<sport>/event_map.csv`, the durable
`game_id ↔ event_id` join. Matching is by sport + home + away + date, with
start-time proximity used only to break ties (MLB doubleheaders). It works
identically for future events, live odds, and historic snapshots, so odds
pulled at any time join to games through this one file. Consensus prices are
the **median across bookmakers** (taken in decimal space) at the latest
pre-game snapshot.

Also available:

```bash
python3 -m pipeline.odds sports                                    # every sport key the API offers
python3 -m pipeline.odds events --sport-key soccer_fifa_world_cup  # upcoming games, no ingester needed
python3 -m pipeline.odds fetch --sport MLB --historical 2026-06-05T16:00:00Z  # paid plans: point-in-time snapshot
python3 -m pipeline.odds match --sport MLB                         # re-run matching from cached snapshots
```

`--historical` is how historic odds meet historic games: pull the snapshot as
of a past instant and the same matcher fills the same event map. Every fetch
prints the API quota remaining.

## 6. Grade yesterday's picks

```bash
python3 -m pipeline.ingest.wnba fetch          # pull in the finals first
python3 -m pipeline.grading grade --sport WNBA # defaults to yesterday
# 2026-07-05  TOTAL OVER 165.5  @ -110   WIN      92-88  $+90.91
# record 1-0-0, P&L $+90.91 — 1/1 picks at market prices
```

Grades the picks published for a date (`--date` for any day) against the
current games CSV and writes `grades_<date>.csv` next to the picks file.
**When matched odds exist** (section 5), wins pay the real consensus-price
payout and totals grade against the *market* line — the actual bettable
proposition — with the price recorded on each grade. Without odds (or with
`--no-odds`) grading falls back to flat $100. Every pick resolves to one of:

| Result | Means | P&L |
|---|---|---|
| WIN / LOSS | at the consensus market price when matched, else flat $100 | payout / −100 |
| PUSH | total landing exactly on the graded line | 0 |
| VOID | game postponed / suspended / cancelled — stake returned | 0 |
| PENDING | game not final yet (or games CSV not refreshed) — re-run after the next fetch | 0 |

VOID and PENDING are reported but never counted in the record. Doubleheaders
grade independently (picks reference the unique per-game id), and NHL OT/SO
finals count as plain moneyline wins.

## 7. Backfill the season, optionally load it into a database

```bash
python3 -m pipeline.backfill --sports WNBA               # today's season, this sport
python3 -m pipeline.backfill --sports WNBA,MLB --through 2026-07-01
# WNBA: 187 games over 62 dates (150 labeled finals) -> data/frames/wnba_2026.csv
```

Replays every game on the sport's games CSV through the feature registry
**as of that game's own date** (`FeatureContext`, point-in-time by
construction — the same code the daily predict step uses), then labels each
row with its outcome once final. Writes the canonical frame CSV,
`data/frames/<sport>_<season>.csv` — identity, one column per feature per
side, and the outcome group (empty until a game is final). Run the sport's
`fetch` first; re-running is safe, it always rebuilds the file for the whole
range.

This is the flat-file half of the future backtester's dataset (PLAN.md §5,
§7); the model/pick/grade columns come later as the daily orchestrator
accumulates that history.

**Optional:** mirror the CSVs into a real database so you can query them with
SQL:

```bash
python3 -m pipeline.db load --sports WNBA     # teams.csv + games CSV + frame CSV -> db
python3 -m pipeline.db status
# teams        275 rows
# games         44 rows
# frames        44 rows
# (sqlite data/model.db)
```

With no setup this writes a local SQLite file, `data/model.db`. To use
PostgreSQL instead:

```bash
pip install "psycopg[binary]"                              # the one optional dependency
export DATABASE_URL=postgresql://user:pass@host:5432/betting_model
python3 -m pipeline.db load --sports WNBA
python3 -m pipeline.db status
# (postgres (DATABASE_URL))
```

Same commands, same schema, same output either way — `Db.connect()` just
checks whether `DATABASE_URL` is set. `load` is a full upsert (safe to re-run
after a fresh `fetch`/`backfill`); the CSVs stay the source of truth, so the
database can be deleted and rebuilt from them at any time. Three tables ship
today: `teams`, `games`, `frames` (the frame CSV's row, with the feature
vector stored as a JSON column). See `PLAN.md` §1 for the rest of the planned
schema.

## 7a. Backfill games + odds from an existing export

For dates too old for the live feeds (they only serve the current season)
or too old for The Odds API's historical-snapshot endpoint (its window is
only a few days), import an existing `games_scores` / `games_odds`-shaped
export instead:

```bash
python3 -m pipeline.backfill_external --scores scores.csv --odds odds.csv
# scores: 41213 rows -> 6284 games across 11 sport/season file(s)
#   MLB 2024: 2268 games (2268 final)
#   ...
# odds: 38907 rows -> 812634 normalized quote rows
#   MLB: 601422 rows (2201 events)
#   ...
```

Input is two CSVs, one row per game each, sharing a `game_key` column —
which turns out to already be The Odds API's own event id (a 32-character
hex string), so it's used directly as our `game_id` for these rows; no
fuzzy matching needed, the join is exact and already in the source data.
`--odds` is optional. `--dry-run` reports without writing anything.

Only sport_keys we have a team registry for are imported — `WNBA`, `MLB`,
`NBA`, `NHL` (plus `basketball_nba_preseason`, mapped to NBA/preseason).
Everything else — soccer leagues, NCAAB, NFL, rugby league, euroleague,
and whatever other sport_keys your export has — is reported and skipped,
not silently dropped: there's no `data/teams.csv` entry to resolve those
team names against. `--sports` narrows further if you only want a subset
of the four right now.

Team names are resolved the same way the odds adapter already does —
`data/teams.csv` names plus `pipeline/odds/normalize.py`'s `NAME_ALIASES`.
Old exports surface franchise relocations the live alias list has never
needed (e.g. Arizona Coyotes → Utah Mammoth); when you hit one, add it to
`NAME_ALIASES` the same way. Any team name that still doesn't resolve is
listed by name and `game_key` in the run's output — nothing is imported
silently unmapped.

Odds come back with **every bookmaker's every quote** (including
`spreads`, which the rest of the pipeline doesn't grade against yet, but
which are stored — a future spread-betting pick policy would find them
already there). Re-running is safe: games upsert by `game_id` into the
season's games CSV, and the odds/event-map files are written the same way
the live odds adapter writes them, so `pipeline.odds.edge` and
`pipeline.grading` read backfilled games exactly like live ones.

## 8. Run the tests

```bash
python3 -m unittest discover -s tests          # all (offline, no network)
python3 -m unittest tests.test_my_model -v     # one suite
python3 -m unittest tests.test_backfill tests.test_db tests.test_backfill_external -v
```

All suites run from committed fixture feeds — no fetch required. The
Postgres-specific test (`tests.test_db.TestPostgres`) is skipped unless
`TEST_DATABASE_URL` points at a reachable PostgreSQL — it's a real round-trip
against the live server, not a mock, so it needs one.

---

## A typical morning — one command

```bash
python3 run_daily.py                       # all four sports, today (US/Eastern)
python3 run_daily.py --sports WNBA,MLB     # just the in-season ones
python3 run_daily.py --date 2026-07-03     # rebuild a missed day
python3 run_daily.py --skip-odds           # odds-blind (or just leave ODDS_API_KEY unset)
```

`run_daily.py` (identical: `python3 -m pipeline daily`) is the PLAN.md §7
orchestrator. Per sport it chains **ingest → grade yesterday → predict
today → odds fetch → edge report**, then writes a markdown daily report —
stage log, grade card, pick sheet, edge table — to
`data/reports/daily_<date>.md`. Stages are isolated and idempotent: a
sport with no games today is SKIPPED ("off-season"), not an error; a
failing stage is recorded (and makes the run exit nonzero) without
stopping the other sports; re-running a date is always safe. Odds stages
skip themselves when `ODDS_API_KEY` isn't set.

Step by step, the same loop by hand is:

```bash
python3 -m pipeline.ingest.wnba fetch                 # yesterday's finals + today's slate
python3 -m pipeline.grading grade --sport WNBA        # grade yesterday at market prices
python3 -m pipeline.models predict --sport WNBA       # today's pick sheet (odds-blind)
python3 -m pipeline.odds fetch --sport WNBA           # today's odds + event matching
python3 -m pipeline.odds edge --sport WNBA --date "$(date +%F)"   # where's the edge?
```

The unattended-cron version of this exists as
`.github/workflows/daily.yml` but is **intentionally disabled** (every
line commented out); the file's header explains how to enable it when
we're ready. Still open from §7: standings snapshots.

## Where files live

| Path | What | Committed? |
|---|---|---|
| `data/teams.csv` | canonical team registry | yes |
| `data/raw/<sport>/` | raw API responses, timestamped | no (gitignored) |
| `data/<sport>/games_<season>.csv` | normalized season games | regenerated by fetch |
| `data/features/<sport>/features_<date>.csv` | per-slate feature frames | regenerated |
| `data/predictions/<sport>/predictions_<date>.csv` | full model output per slate | regenerated |
| `data/predictions/<sport>/picks_<date>.csv` | published pick sheet | regenerated |
| `data/predictions/<sport>/grades_<date>.csv` | graded picks (WIN/LOSS/PUSH/VOID/PENDING) | regenerated |
| `data/predictions/<sport>/edges_<date>.csv` | model-vs-market edge rows | regenerated |
| `data/odds/<sport>/snapshots_*.csv` | normalized odds, one row per event/book/market/outcome | regenerated |
| `data/odds/<sport>/event_map.csv` | durable game_id ↔ event_id join | **worth committing** — it's state |
| `data/reports/daily_<date>.md` | the orchestrator's daily report | regenerated (commit if you want history) |
| `data/frames/<sport>_<season>.csv` | labeled canonical frame (backfill) | regenerated |
| `data/model.db` | local SQLite mirror of teams/games/frames | no (gitignored) — absent when `DATABASE_URL` is set |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `…games_<season>.csv not found — run fetch first` | Run the sport's `fetch` (or check `--season` matches the CSV name in `data/<sport>/`) |
| `unknown sport(s) …` from `pipeline.backfill` / `pipeline.db` | `--sports` takes a comma-separated subset of `WNBA,MLB,NBA,NHL` |
| `nothing backfilled — no games CSVs found` | Run the sport's ingest `fetch` before `python3 -m pipeline.backfill` |
| `DATABASE_URL is set but the driver is missing` | `pip install "psycopg[binary]"` |
| `pipeline.db load` reports `0 frame rows` for a sport | Run `python3 -m pipeline.backfill --sports <SPORT>` first — `load` mirrors what's already on disk, it doesn't compute frames |
| `no cached feed in data/raw/… — run fetch without --offline` | `--offline` needs a prior real fetch's cache |
| `unmapped tricode/team id …` | Add the team or alias to `data/teams.csv` / the adapter's alias map, rerun `scripts/validate_teams.py` |
| `unexpected feed shape…` | League changed its JSON; the message names the cached raw file to inspect |
| `no <SPORT> games on <date>` | Off-season or an off day — check `today --date` on a known game day |
| `NO PREDICTION (insufficient data)` | A team has no completed regular-season games yet (early season); the model refuses to guess |
| `picks_<date>.csv not found` when grading | `predict` was never run for that date — the grader only grades what was actually published |
| Grades stuck on `PENDING` | The games CSV predates the final — run the sport's `fetch` again, then re-grade |
| `no API key: set ODDS_API_KEY…` | `export ODDS_API_KEY=...` before any odds command |
| `unmapped team names (add to NAME_ALIASES?)` | The Odds API spells a team differently — add the alias in `pipeline/odds/normalize.py` |
| `AMBIGUOUS: … multiple candidates` | Doubleheader whose start times couldn't break the tie — check the games CSV has `start_time_utc` for both games |
| `no matched odds event` in edge/grading | Run `python3 -m pipeline.odds fetch` (or `match`) after the games CSV exists |
| `backfill_external` reports a sport_key as "skipped" | Either it's outside `--sports`, or there's genuinely no `data/teams.csv` entry for that sport yet (soccer, NCAAB, NFL, …) |
| `backfill_external` lists a team name under "unmapped" | Add it to `NAME_ALIASES` in `pipeline/odds/normalize.py` — old exports surface franchise relocations (e.g. Arizona Coyotes → Utah Mammoth) the live alias list never needed |
