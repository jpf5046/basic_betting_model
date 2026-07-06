# PLAN.md — Backend Design & Build Wish List

**Goal:** a daily, automated pipeline that (1) predicts the winner and final score of
upcoming games, (2) publishes picks, (3) grades yesterday's picks once outcomes are known,
and (4) makes it trivially easy to add new features (e.g. travel time), define new models,
and backtest any model configuration against history.

The three files already in this repo define the vision:

| File | What it gives us |
|---|---|
| `my_model.md` | The full spec of the baseline statistical model ("My Model v1") — five weighted methods, win-prob logistic, pick gates. This becomes the **first model plugin**, not the only one. |
| `CLAUDE_CODE_PROMPT.md` | The Researcher Console UI spec (parameters editor, backtest screen, configs, live). The UI is **last** — but it tells us exactly what the backend must expose: saved configs, backtest runs, live promotion, graded picks. |
| `researcher-console-design.html` | Interactive design reference for that UI. |

The rest of this document is the backend design. Each numbered section is a work item we
will tackle one by one. **For how to run what's already built, see [`HOWTO.md`](HOWTO.md)**
— fetch, inspect, feature frames, predictions, configs, tests. Progress at a glance lives
in the §10 build-order table.

---

## 0. Guiding principles

1. **Everything is point-in-time.** Every feature value is computed *as of* a date, using
   only games completed before that date. This is what makes backtests honest — a backtest
   for June 1 must see exactly what the daily pipeline would have seen on June 1.
2. **Models are plugins, features are plugins.** Adding a feature (travel time, weather,
   rest days) or a whole new model must never require touching the pipeline — only
   registering a new component ("app factory" pattern).
3. **Sitting out is an output.** TOSS-UP / PASS / insufficient-data are first-class results,
   stored and counted, never silently dropped.
4. **One canonical dataframe** feeds both the daily run and the backtester. Same code path,
   different date range.
5. **Configs are data, not code.** Every tunable constant lives in a versioned config row
   (per sport, per model), matching the UI's save/promote/lock lifecycle.

---

## 1. Storage layer (SQLite first, swappable later)

A single SQLite file (`model.db`) is plenty for v1; the schema is designed so Postgres is a
drop-in swap later. Core tables:

| Table | Purpose | Key columns |
|---|---|---|
| `teams` | Canonical team registry + ID mapping to each stats API | `team_id, sport, name, abbrev, external_ids(json), venue_lat, venue_lon, timezone` |
| `games` | One row per scheduled game, updated through its lifecycle | `game_id, sport, date, start_time_utc, away_team_id, home_team_id, status(scheduled/final/postponed), away_score, home_score` |
| `game_logs` | Completed-game log per team (powers common opponents, H2H, form) | `sport, team_id, game_id, date, opponent_id, is_home, scored, allowed, result, ot_flag` |
| `standings_snapshots` | Daily point-in-time standings capture | `sport, team_id, as_of_date, wins, losses, gp, win_pct, spg, sapg, diff, home_rec, road_rec, last10, streak, rank` |
| `feature_values` | Computed feature outputs, keyed by game + feature + as-of date | `game_id, feature_name, feature_version, as_of_date, side(away/home/game), value(json)` |
| `model_configs` | Saved parameter sets (the UI's "configs") | `config_id, model_name, sport, name, params(json), status(draft/live/retired/archived), locked, created_at` |
| `live_map` | Which config is live per (sport, model) | `sport, model_name, config_id, live_since` |
| `promotion_history` | Audit log of set-live / rollback events | `date, sport, new_config_id, old_config_id, record_at_replacement` |
| `predictions` | Raw model output per game per config, pick or not | `game_id, config_id, run_date, pred_away, pred_home, pred_total, pred_spread, win_prob, pred_confidence, method_details(json)` |
| `picks` | Published picks only (the daily pick sheet) | `pick_id, game_id, config_id, run_date, bet_type(ML/TOTAL), selection, confidence(STRONG/LEAN/—), line_used, status(pending/graded)` |
| `grades` | Outcome of every pick | `pick_id, result(WIN/LOSS/PUSH/VOID), pnl_flat_100, graded_at` |
| `backtest_runs` | Backtest metadata + headline results | `run_id, config_id/draft_params, date_from, date_to, sports, bet_types, status, metrics(json), created_at` |
| `backtest_picks` | Full pick log of each backtest run | `run_id, game_id, bet_type, selection, result, pnl` |
| `ingest_log` | What was fetched when, and failures | `source, sport, date, status, error` |

**TODO:**
- [ ] Define schema as versioned migrations (plain SQL files + tiny migration runner).
- [x] Team registry with ID mapping — shipped as a flat file, `data/teams.csv` (275 teams:
      NFL, NBA, MLB, NHL, WNBA, CFB/FBS 2026), validated by `scripts/validate_teams.py`.
      The CSV is the source of truth; the future `teams` table seeds from it. Unmapped
      teams (empty `external_ids`) must be a loud error in adapters. Still open: CBB/WCBB
      rows and WNBA/CFB external ids — see `data/README.md` for the refresh path.
- [x] Venue lat/lon + timezone included from day one (free enabler for the future
      travel-time feature — no schema change needed later).

---

## 2. Data ingestion (source adapters)

One adapter per source, all conforming to a common interface so a new sport or source is a
new adapter, not a pipeline change:

```
class SourceAdapter(Protocol):
    sport: str
    def fetch_schedule(self, date) -> list[Game]         # upcoming + today's games
    def fetch_results(self, date) -> list[FinalScore]    # completed games
    def fetch_standings(self, as_of) -> list[TeamStanding]
    def fetch_game_logs(self, team, season) -> list[LogEntry]
```

**Built:** `wnba`, `mlb`, `nba`, `nhl` (`pipeline/ingest/*.py`) — each covers today's
slate, season scores, and common-opponent inputs, with raw-response caching and offline
tests. The shared Game record (one schema for every sport's games CSV, matching the
`games` table above), query layer, HTTP retry, and CLI live in `pipeline/ingest/core.py`;
an adapter owns only its feed URLs, team mapping, and feed parsing. **Also built:
`pipeline/odds/`** (The Odds API v4, key via `ODDS_API_KEY`): odds fetch (live +
historical snapshots) with raw caching, normalization to team_id-keyed rows, the durable
`game_id ↔ event_id` event map (matched by sport + home + away + date, start-time
tie-break for doubleheaders — works for future, live, and historic odds), consensus
median pricing, a model-vs-market edge report, and market-priced grading. The `events`
endpoint also surfaces upcoming games for sports without an ingester (e.g. World Cup).
Predictions stay odds-blind by design — odds join on afterward. Later: `weather_api`
(Open-Meteo or similar, keyed by venue lat/lon + start time).

**TODO:**
- [x] League adapters with retry/backoff and raw-response caching to disk — WNBA, MLB,
      NBA, NHL built on the shared `core.py`; standings ingestion still open below.
- [x] Backfill job — `python3 -m pipeline.backfill` (current + N past seasons into the
      same per-season games CSVs). **Contract:** every adapter implements
      `run_fetch(season)` + `past_seasons(back)`, so a new sport is backfillable the day
      it lands. MLB/NHL reuse their endpoints; NBA/WNBA use stats leaguegamelog for past
      seasons; defunct teams live as `active=0` registry rows (Arizona Coyotes).
      Still open: daily standings snapshots (below).
- [ ] Nightly ingest writes `standings_snapshots` keyed by `as_of_date` — never overwrite;
      snapshots are the point-in-time backbone.
- [ ] Weather adapter (wish list): temp, wind speed/direction, precipitation for outdoor
      MLB venues, stored as `feature_values` inputs.

---

## 3. Feature registry (the "add travel time in one file" requirement)

Every model input is a **Feature** — a small plugin registered by name:

```
@register_feature("travel_km_last_7d", sports=["MLB","NBA","NHL"], version=1)
def travel_km_last_7d(ctx: FeatureContext, game: Game, side: Side) -> float:
    # ctx exposes point-in-time reads ONLY: game logs, standings, venues
    # as of ctx.as_of_date. It cannot see the future by construction.
    ...
```

Key properties:

- **Declared dependencies** (which tables/other features it reads) so the pipeline knows
  ingestion must finish first.
- **Point-in-time context:** the `FeatureContext` only serves data with `date < as_of_date`.
  A feature literally cannot leak future information.
- **Versioned:** changing a feature's logic bumps its version; old backtests remain
  reproducible against old versions.
- **Cached:** computed values land in `feature_values`, so the backtester never recomputes
  a season of common-opponent tables per run.
- **Null-safe:** a feature may return `None` (no common opponents yet, no weather for a
  dome). Models declare per-feature fallback behavior (My Model's "drop and renormalize").

**Built** (`pipeline/features/` — registry + point-in-time FeatureContext + frame
builder + CLI; one file per feature under `pipeline/features/defs/`, auto-discovered):

- [x] `season_scoring` (SPG, SAPG, GP per team)
- [x] `common_opponents` (weighted scored/allowed vs shared opponents + count)
- [x] `last10_form` (L10 record; NHL points-based variant)
- [x] `home_advantage` (per-sport factory units; WNBA 2.5 provisional)
- [x] `head_to_head` (season H2H scores + count)
- [x] `games_played` (for the evidence/confidence score)
- [x] `travel_km` — **the canary shipped early and passed**: one new file, nothing
      else touched, powered by the venue coordinates seeded in item 1.

Wish-list features (each should be an afternoon of work once the registry exists):

- [ ] `rest_days` / `back_to_back` flag
- [ ] `travel_km_since_last_home_game`, `travel_km_last_7d`, `timezones_crossed` — computed
      from `game_logs` venue sequence + `teams.venue_lat/lon` (haversine). **This is the
      canary feature: if adding it requires touching anything but its own file, the design
      failed.**
- [ ] `weather_temp`, `weather_wind` (MLB outdoor only)
- [ ] `streak_length`, `home_road_split_diff`
- [ ] `starting_pitcher_era` (MLB, needs player-level ingestion — far future)
- [ ] `closing_line` / `market_total` (odds, if/when an odds adapter exists)

---

## 4. Model factory

A **Model** is a plugin that consumes named features and a config, and emits a standardized
prediction:

```
@register_model("my_model")
class MyModel:
    required_features = ["season_scoring", "common_opponents", "last10_form",
                         "home_advantage_units", "head_to_head", "games_played"]
    def predict(self, features: FeatureRow, params: dict) -> Prediction
    # Prediction = pred_away, pred_home, win_prob, pred_confidence, method_details
```

Separately, a **PickPolicy** turns a `Prediction` into zero, one, or two picks (ML gates,
totals thresholds). Splitting prediction from pick policy means we can backtest "same
predictions, different gates" cheaply.

- `my_model` v1 = the exact spec in `my_model.md`, with every constant (weights, HOME_ADV,
  form base/range, k, gates, totals thresholds) coming from `model_configs.params`. Factory
  defaults = the numbers in that document.
- Future models registered the same way: `my_model_plus_travel` (adds travel features to the
  blend), `elo`, `poisson_scores` (natural for NHL/MLB score distributions), `ensemble`
  (blend of other models' predictions). None require pipeline changes.

**TODO:**
- [x] `Prediction` and `Pick` dataclasses + the model registry (`pipeline/models/`,
      one file per model under `defs/`, auto-discovered) with pick policy split out
      (`picks.py`) so "same predictions, different gates" is cheap.
- [x] `my_model` v1 against the feature registry — reproduces `my_model.md` §2–§5
      on hand-computed fixture examples (blend, renormalization, win prob, gates,
      display tie-break, confidence label). WNBA constants are provisional
      (not in my_model.md): home_adv 2.5, k 0.15, totals 168/158 — tune via backtest.
- [x] Config loader with per-sport param validation (weights sum to 1.000, gates
      ordered, totals ordered) — factory defaults in `config.py`, JSON override files
      deep-merge, same rules the UI editor will surface.
- [ ] CLI pick sheet exists (`python3 -m pipeline.models predict`); wire into the
      daily orchestrator (§7) once grading lands.

---

## 5. The canonical dataframe (backtesting + daily, one shape)

The single most important artifact. One row per **(game, side-pair)**, wide format:

| Column group | Columns |
|---|---|
| Identity | `game_id, sport, date, season, away_team, home_team, start_time_utc` |
| Features (away/home pairs) | `away_spg, home_spg, away_sapg, home_sapg, away_l10, home_l10, common_opp_count, away_common_scored, ..., away_travel_km_7d, home_travel_km_7d, ...` — one column per registered feature per side, generated dynamically from the registry |
| Feature meta | `as_of_date, feature_versions(json)` |
| Model output (per config) | `pred_away, pred_home, pred_total, pred_spread, win_prob, pred_confidence` |
| Pick output | `ml_pick, ml_confidence, total_pick` |
| **Outcome** (null until final) | `final_away, final_home, actual_total, actual_margin, winner` |
| Grade (per pick) | `ml_result, ml_pnl, total_result, total_pnl` |

Rules:

- The daily run produces today's rows with outcomes null; the grader fills outcomes in
  tomorrow. History is therefore *accumulated by the pipeline itself* — after a month of
  running, we have a month of perfectly leak-free labeled rows for free.
- The backfill job (§2) synthesizes the same rows for past dates by replaying snapshots, so
  we can backtest before the pipeline has run for long.
- Exposed as `build_frame(sports, date_from, date_to, features="all"|list) -> DataFrame`
  (pandas) — this is also the future ML-training dataset if we ever fit weights instead of
  hand-tuning them.

**TODO:**
- [x] `build_frame()` — `pipeline/frame/`: one row per game, features as-of the game
      date (fresh point-in-time context per date), outcome columns on finals only.
      CLI writes `data/frames/<sport>/frame_<season>.csv`.
- [ ] Parquet export per season — deferred with the rest of the pandas question; the
      CSV columns are already flat and typed for the day it's worth it.

---

## 6. Backtester

Replays history through any model + config:

```
run_backtest(model="my_model", params=<draft or saved config>,
             sports=["MLB"], date_from=..., date_to=...,
             bet_types=["ML","TOTAL"]) -> BacktestResult
```

- Walks day by day over the canonical frame, calls `model.predict` + pick policy per game
  using only that day's as-of features, grades against stored outcomes at flat $100 stakes.
- **Always also runs the factory-default baseline over the same slice** — the UI's delta
  chips (candidate vs baseline) are computed here, not in the frontend.
- Outputs: record W–L–P, ROI, total P&L, picks made, pass rate; breakdowns by league, bet
  type, confidence tier; cumulative P&L series (candidate + baseline) for the chart; full
  pick log. All persisted to `backtest_runs` / `backtest_picks` so runs are comparable
  forever (the UI's history rail and 2-run compare read straight from these tables).
- Deterministic and cached: same config + same slice + same feature versions ⇒ same run_id
  can be served from storage instead of recomputed.
- Structured failure states (e.g. "standings snapshots missing for 14 NHL dates") map to the
  UI's error panel.

**TODO:**
- [x] Metrics module — `pipeline/backtest/metrics.py` (record/ROI/P&L/pass-rate,
      by-bet-type and by-tier groupbys, cumulative P&L series), built on the same Grade
      records live grading produces, so live-vs-backtest stays apples-to-apples.
- [x] CLI — `python3 -m pipeline.backtest run --sport WNBA --season 2025 [--config x.json]
      [--from --to] [--no-odds]`: day-by-day replay, candidate always compared against
      the factory baseline on the same slice, market-priced grading via the odds event
      map, deterministic run ids, results persisted to `data/backtests/<sport>/`.
- [ ] Parameter sweep helper (wish list): grid/random search over param ranges, ranked
      results table — the "tune k for NHL" workflow without clicking through a UI 40 times.
- [ ] Walk-forward validation helper (wish list): tune on month N, evaluate on month N+1,
      to catch overfit configs before they're promoted.

---

## 7. The daily pipeline (the centerpiece)

One orchestrated run, early morning (e.g. 10:00 UTC), as an ordered DAG of idempotent
stages — safe to re-run any stage for any date:

```
daily(date):
  1. INGEST RESULTS     yesterday's final scores → games, game_logs
  2. GRADE              match pending picks to finals → grades (WIN/LOSS/PUSH, ±$100/0)
                        void picks for postponed games; update live records
  3. INGEST SLATE       today's schedule, standings snapshot (as_of=today)
  4. COMPUTE FEATURES   every registered feature for every game on today's slate,
                        as_of=today → feature_values
  5. PREDICT            for each sport: run the LIVE config (live_map) → predictions
                        (optionally also shadow configs — see wish list)
  6. PUBLISH            assemble the pick sheet (non-TOSS-UP, non-PASS only) → picks,
                        plus a rendered daily report (markdown/JSON) with predicted
                        scores, win probs, confidence labels
  7. VERIFY & LOG       row counts, unmapped teams, missing snapshots, API failures
                        → ingest_log; alert (email/push) on anomalies
```

Implementation notes:

- Plain Python orchestrator with explicit stage functions + a state table — **not** Airflow;
  keep it runnable as `python -m pipeline daily [--date YYYY-MM-DD]` locally and via
  GitHub Actions cron (this repo is already on GitHub — free scheduler, artifacts, and run
  logs).
- Every stage takes `--date`, so rebuilding a missed day is
  `python -m pipeline daily --date 2026-07-03`.
- Off-season handling: a sport with no games short-circuits to "off-season" status (the UI's
  off-season card), not an error.
- Grading edge cases — **built** (`pipeline/grading/`, item 5): postponed/suspended/
  cancelled → VOID $0; not-final-yet → PENDING (re-run after the next fetch); MLB
  doubleheaders grade independently via unique game ids; NHL OT/SO counts as a plain ML
  win; totals landing exactly on a whole-number line push.

**Wish list for the daily run:**
- [ ] **Shadow mode:** run 1–3 non-live candidate configs every day alongside the live one,
      storing predictions + virtual grades but not publishing. Real out-of-sample evidence
      before promoting anything.
- [ ] Daily report artifact (markdown committed to the repo or posted somewhere) — pick
      sheet + yesterday's grade card, human-readable.
- [ ] Failure notifications (start with GitHub Actions failure emails; graduate later).

---

## 8. Config lifecycle & API (what the UI eventually sits on)

Thin service layer (FastAPI when we get there; plain functions until then) exposing exactly
what the Researcher Console needs — everything below already exists in the tables above:

- Configs: list/create/duplicate/update(if unlocked)/archive; field-level diff between two
  configs; validation endpoint (reuses §4 validators).
- Promotion: set-live per sport (locks the config, writes `live_map` + `promotion_history`),
  rollback = re-promote previous entry.
- Backtests: launch, poll progress, fetch results, list history, fetch two for compare.
- Live: today's pick sheet, since-promotion record/ROI/P&L, live-vs-backtest curve
  (both series from §6's shared metrics module), yesterday's grading summary.

**TODO:**
- [ ] Keep this layer boring: no logic in endpoints, everything delegated to the modules
      above, so the CLI and the API can never disagree.

---

## 9. Repo layout (current, with planned pieces marked)

```
HOWTO.md               # how to run everything that exists today
pipeline/
  ingest/
    core.py            # shared Game record, queries, CSV io, HTTP retry, CLI
    wnba.py mlb.py
    nba.py nhl.py      # one adapter per sport (§2) — feed URLs, mapping, parsing
  features/
    registry.py        # @register decorator, per-sport lookup (§3)
    context.py         # point-in-time FeatureContext — the anti-leakage boundary
    frame.py           # wide per-slate feature CSV (proto §5 dataframe)
    defs/              # ONE FILE PER FEATURE (7 shipped, incl. travel_km canary)
  models/
    registry.py        # @register_model (§4)
    base.py            # Prediction / Pick dataclasses, gather_features
    config.py          # factory defaults per sport, validation, JSON overrides
    picks.py           # pick policy: ML gates + totals thresholds
    defs/my_model.py   # ONE FILE PER MODEL (my_model.md v1 shipped)
  grading/             # (planned) grader + edge cases (§7)
  orchestrator.py      # (planned) the daily DAG (§7)
  backtest/            # (planned) runner, metrics, sweep (§6)
  db/                  # (planned) SQLite schema when flat files outgrow (§1)
  api/                 # (planned) FastAPI layer (§8)
scripts/
  validate_teams.py    # registry checks
tests/                 # 82 offline tests; fixtures/ holds frozen feeds
data/
  teams.csv            # canonical team registry (committed)
  raw/                 # timestamped API responses (gitignored)
  <sport>/ features/ predictions/   # regenerated artifacts
.github/workflows/     # (planned) daily.yml cron
```

---

## 10. Build order (tackle one by one)

| # | Status | Item | Depends on | Definition of done |
|---|---|---|---|---|
| 1 | 🟡 half | Schema + migrations + team registry | — | **Done:** `data/teams.csv` (275 teams, venue lat/lon, timezones, native ids) + validator. **Open:** SQLite schema/migrations, deferred until flat files pinch |
| 2 | ✅ | Sport adapters + raw cache | 1 | WNBA/MLB/NBA/NHL built on shared `ingest/core.py` (PRs #3–#7); schedule + results + game logs flowing. **Open:** standings snapshots, backfill job |
| 3 | ✅ | Feature registry + core features | 1 | `pipeline/features/` (PR #8): point-in-time context, 6 core features, frame CLI. Cache deferred until backtester needs it |
| 4 | ✅ | `my_model` v1 + pick policy + config loader | 3 | `pipeline/models/` (PR #9): reproduces `my_model.md` on hand-computed fixtures; pick sheet CLI works. WNBA constants provisional |
| 5 | ✅ | Grader | 2 | `pipeline/grading/`: WIN/LOSS/PUSH plus VOID (postponed/suspended/cancelled) and PENDING (not final yet); doubleheaders via unique game ids; NHL OT/SO = plain ML win; whole-number totals push |
| 5a | ✅ | Odds adapter + event matching + edge + real payouts | 2,4,5 | `pipeline/odds/` (moved up by request): The Odds API v4 fetch/historical, game↔event map, consensus closing prices, model-vs-market edge report, grading at market prices vs market total lines |
| 6 | ⬜ next | Daily orchestrator + GitHub Actions cron | 2,3,4,5 | Unattended morning run produces pick sheet + grade card |
| 7 | ✅ | Season backfill (all four sports) | 2,3 | `pipeline/backfill.py`: current + N past seasons per sport via the `run_fetch(season)` / `past_seasons(back)` adapter contract; features/predictions/odds read history through the same per-season CSVs. Historic odds join via `odds fetch --historical` |
| 8 | ✅ | `build_frame()` (parquet deferred) | 7 | `pipeline/frame/`: one call returns the leak-free modeling table (features as-of game date + outcomes); CSV per season |
| 9 | ✅ | Backtester + metrics + CLI | 8 | `pipeline/backtest/`: day-by-day replay through any config, graded at consensus market prices when odds cover the games, candidate vs factory baseline on the same slice, deterministic run ids, persisted JSON + pick-log CSV |
| 10 | ✅ | NBA + NHL adapters | 2 pattern | Shipped early alongside item 2 (PRs #5, #6) |
| 11 | ⬜ | Shadow mode + sweep + walk-forward | 9 | Candidate configs accumulate live evidence |
| 12 | ✅ | Travel-time feature (the canary) | 3 | Shipped early inside PR #8 as one new file — the design claim held. **Open:** backtest it in a model variant once item 9 lands |
| 13 | ⬜ | Weather adapter + features | 3 | MLB outdoor games get temp/wind features |
| 14 | ⬜ | API layer | 6,9 | Endpoints for everything in §8 |
| 15 | ⬜ | Researcher Console UI | 14 | Rebuild `researcher-console-design.html` against the real API |

---

## Open questions (decide as we go, none block item 1)

- ~~Odds ingestion: which source, and do we store closing lines from day one?~~
  **Resolved:** The Odds API v4 (`pipeline/odds/`); snapshots stored from day one and
  grading uses consensus closing prices against the market total line. Still open:
  de-vigging implied probabilities (currently raw, vig included) and stake sizing
  beyond flat $100 (Kelly fraction once edge estimates prove calibrated).
- Season boundaries: do features reset hard at season start, or blend in last season's data
  for the first N games (early-season cold start is when common-opponents is empty)?
- Where does the daily report go — committed markdown, email, or just the future UI?
