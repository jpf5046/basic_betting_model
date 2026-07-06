# HOWTO — running the pipeline

Everything that exists in this repo today and how to run it, in the order the
daily process will eventually run it: **ingest → inspect → features → predict
→ odds → grade**.
For the architecture and what's coming next, see [`PLAN.md`](PLAN.md).

## Prerequisites

- Python **3.11+**, standard library only — there is nothing to `pip install`.
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
venue coordinates, timezones, native API ids, and an `active` flag —
`active=0` rows are historic franchises kept so backfilled seasons map).
Everything downstream keys off its `team_id` values (`wnba-nyl`,
`mlb-nyy`, …). Run the validator after any hand edit.

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

### Historic backfill

```bash
python3 -m pipeline.backfill --sport all --back 3        # current + last 3 seasons, every sport
python3 -m pipeline.backfill --sport WNBA --seasons 2024,2023
```

Backfill writes the same per-season CSVs (`data/<sport>/games_2024.csv`, …),
so features, predictions, grading, and odds matching read history through
the exact same code path as today's slate. Sources per sport: MLB and NHL
reuse their normal endpoints with a past season key; NBA and WNBA switch to
the leagues' stats APIs (`leaguegamelog`) because the CDN schedule feeds
only serve the current season. Historic games carry no tip-off times —
odds matching for them is by teams + date, which is the matcher's primary
key anyway (only MLB doubleheaders need times, and those are rare).

Defunct franchises live in `data/teams.csv` as `active=0` rows (e.g. the
Arizona Coyotes) so old games map cleanly; clubs that didn't exist in a
season (Utah before 2024-25) 404 and are skipped with a note. Historic
**odds** are a separate, quota-costing step:
`python3 -m pipeline.odds fetch --sport X --historical <ISO>` joins them to
backfilled games through the same event map.

**Adding a new sport? Backfill is part of the definition of done.** Every
adapter must implement `run_fetch(season)` (any season, not just the
current one) and `past_seasons(back)` — `pipeline/backfill.py` is generic
over that contract and fails loudly on adapters that skip it.

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

## 7. Run the tests

```bash
python3 -m unittest discover -s tests          # all (offline, no network)
python3 -m unittest tests.test_my_model -v     # one suite
```

All suites run from committed fixture feeds — no fetch required.

---

## A typical morning, today (by hand)

```bash
python3 -m pipeline.ingest.wnba fetch                 # yesterday's finals + today's slate
python3 -m pipeline.grading grade --sport WNBA        # grade yesterday at market prices
python3 -m pipeline.models predict --sport WNBA       # today's pick sheet (odds-blind)
python3 -m pipeline.odds fetch --sport WNBA           # today's odds + event matching
python3 -m pipeline.odds edge --sport WNBA --date "$(date +%F)"   # where's the edge?
```

That's the whole daily loop, by hand. The orchestrator that chains these
steps on a schedule (plus standings snapshots and a daily report) is the next
build item — see `PLAN.md` §7 and §10. This section becomes one command
(`python3 -m pipeline daily`) when it lands.

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

## Troubleshooting

| Symptom | Fix |
|---|---|
| `…games_<season>.csv not found — run fetch first` | Run the sport's `fetch` (or check `--season` matches the CSV name in `data/<sport>/`) |
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
