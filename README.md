# basic_betting_model

A daily, automated sports-betting pipeline: it ingests schedules and scores for
**WNBA, MLB, NBA, and NHL**, computes point-in-time features, predicts the winner
and final score of every game on today's slate, publishes a pick sheet, compares
the model to live market odds, and grades yesterday's picks once the finals are in.

```bash
python3 run_daily.py          # the whole morning loop, one command
```

## Requirements

- **Python 3.11+, standard library only** — there is nothing to `pip install`
  for the pipeline itself. All storage is flat CSVs under `data/` (see
  [Storage & databases](#storage--databases) below); no database is required
  to run locally.
- Internet access for the `fetch` stages (the leagues' public, key-less APIs).
- Optional: `export ODDS_API_KEY=...` ([The Odds API](https://the-odds-api.com))
  for the odds-fetch and edge-report stages — without it those stages skip
  themselves and the pipeline runs odds-blind.
- Optional: `export DATABASE_URL=postgresql://...` to mirror the flat files
  into PostgreSQL instead of local SQLite — needs `pip install "psycopg[binary]"`,
  the one optional dependency. See [Storage & databases](#storage--databases).

## Quickstart

```bash
python3 -m unittest discover -s tests      # 144 offline tests, no network needed
python3 run_daily.py --sports WNBA,MLB     # daily run for the in-season sports
python3 run_daily.py --date 2026-07-03     # rebuild a missed day
```

Each run writes a human-readable report — stage log, yesterday's grade card,
today's pick sheet, model-vs-market edge — to `data/reports/daily_<date>.md`.

The GitHub Actions cron version of the daily run exists at
`.github/workflows/daily.yml` but is **intentionally disabled** (fully commented
out); its header explains how to enable it.

## What's built

| Piece | Where | What it does |
|---|---|---|
| Sport adapters | `pipeline/ingest/` | Schedule/score feeds → one shared games CSV schema, with raw-response caching and retry |
| Feature registry | `pipeline/features/` | Point-in-time features (scoring, common opponents, form, H2H, travel, …) — one file per feature, leak-proof by construction |
| Model + pick policy | `pipeline/models/` | `my_model` v1 (the [`my_model.md`](my_model.md) spec) → predictions, win prob, ML/TOTAL picks with confidence gates |
| Odds adapter | `pipeline/odds/` | The Odds API v4: consensus prices, game↔event matching, model-vs-market edge report |
| Grader | `pipeline/grading/` | WIN/LOSS/PUSH/VOID/PENDING at market prices, doubleheader- and OT-aware |
| Daily orchestrator | `pipeline/orchestrator.py` | Chains all of the above per sport; `run_daily.py` / `python3 -m pipeline daily` |
| Season backfill | `pipeline/backfill.py` | Replays a season through the feature registry (point-in-time) to synthesize the labeled canonical frame |
| Storage layer | `pipeline/db/` | Optional queryable mirror of the CSVs — PostgreSQL via `DATABASE_URL`, else local SQLite |

## Docs

| File | Read it for |
|---|---|
| [`HOWTO.md`](HOWTO.md) | Running everything that exists today, command by command, plus troubleshooting |
| [`PLAN.md`](PLAN.md) | The backend architecture, build order, and what's next (backfill, backtester, API, UI) |
| [`my_model.md`](my_model.md) | The full spec of the baseline statistical model |
| [`CLAUDE_CODE_PROMPT.md`](CLAUDE_CODE_PROMPT.md) / `researcher-console-design.html` | The future Researcher Console UI this backend will serve |

## Storage & databases

Flat CSVs under `data/` remain the source of truth (games, feature frames,
predictions, picks, grades, odds snapshots) — reviewable, diffable, and
sufficient on their own; **you do not need a database to run the pipeline
locally.**

A database mirror (PLAN.md §1) is available on top of that for anyone who
wants to query the data with SQL — e.g. for the backtester, ad hoc analysis,
or feeding the future API layer. It follows the CSVs, not the other way
around:

```bash
# No setup: local SQLite file at data/model.db
python3 -m pipeline.backfill --sports WNBA          # build the labeled frame CSV
python3 -m pipeline.db load --sports WNBA           # mirror teams/games/frames into it
python3 -m pipeline.db status                       # row counts

# With a PostgreSQL instance: same commands, one env var, one extra package
pip install "psycopg[binary]"
export DATABASE_URL=postgresql://user:pass@host:5432/betting_model
python3 -m pipeline.db load --sports WNBA
```

`DATABASE_URL` set → PostgreSQL; unset → the local SQLite file — same schema,
same SQL, same CLI either way (`pipeline/db/core.py`). `load` is a full
idempotent upsert, so it's always safe to re-run after a fresh `fetch` or
`backfill`; the database can be deleted and rebuilt from the CSVs at any time.
Three tables today: `teams`, `games`, and `frames` (the canonical
identity+features+outcome row from `pipeline.backfill`, features stored as a
JSON column so new features never need a migration). See `PLAN.md` §1 for the
rest of the planned schema (configs, predictions, picks, grades) as those
land.
