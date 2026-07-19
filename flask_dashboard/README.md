# `flask_dashboard/` — the utility dashboard

A plain, read-only Flask + Bootstrap front end that describes exactly what the
pipeline has on disk and stitches the pieces together so you can see how the
model is doing. **It is additive:** it does not touch — and does not depend on —
the existing FastAPI "Researcher Console" under `pipeline/api/`. The two front
ends can run side by side.

No custom JavaScript (only Bootstrap's own bundle for the responsive navbar),
no writes, no model logic. Every number is read straight off the same artifacts
under `data/` through the existing `pipeline.api` service/parsers layer, so the
dashboard and the pipeline can never disagree.

## Run it

```bash
pip install -r requirements-web.txt          # just Flask
python3 -m flask_dashboard                    # http://127.0.0.1:5000
```

Or with the Flask CLI / a custom host+port:

```bash
FLASK_APP=flask_dashboard flask run
HOST=0.0.0.0 PORT=8080 python3 -m flask_dashboard
```

Run it from the repo root so `pipeline` is importable.

## Pages

| Page | What it answers |
|---|---|
| **Overview** | Today's game count, overall pick record / ROI / net P&L, winner accuracy, the P&L curve, per-sport game totals, recent runs, DB status — one screen. |
| **Today's Games** | Today's full slate per sport, each game annotated with the model's prediction and whether it was published as a pick. |
| **Browse Games** | The whole schedule/score feed: filter by sport and by Today / Upcoming / Past / All. Answers "what games existed, what games are in the future". |
| **Performance** | How well the model did — record, ROI and P&L broken down by sport, bet type, and confidence tier, plus straight-up winner accuracy. |
| **Predictions** | The full model output (predicted score, win %, ML/TOTAL leans) for any sport + date that has a predictions file. |
| **Daily Runs** | The morning pipeline's report history: the stage grid (ingest / grade / predict / odds / edge) and each run's grade card, pick sheet, edge report, and any failures. |
| **Database** | An entity-relationship diagram plus every table and CSV dataset, column by column, and the one-to-many links between them. A friendly UI wrapper over the schema. |

## How it's wired

```
flask_dashboard/
  app.py            Flask factory + routes (thin controllers)
  data_access.py    the read layer: reuses pipeline.api.service, reads games
                    CSVs, computes winner accuracy, builds the P&L sparkline
  schema_meta.py    parses pipeline.db.core.SCHEMA + describes CSV datasets
                    and the relationships shown on the Database page
  templates/        Bootstrap 5 templates (Jinja), plus _macros.html
  static/style.css  a few small tweaks on top of Bootstrap
```

Data sources, all existing:

- `data/teams.csv` — team names/abbrevs (via `pipeline.api.teams`)
- `data/<sport>/games_*.csv` — the schedule/score feed (read here directly)
- `data/predictions/<sport>/{predictions,picks,grades}_<date>.csv` — model output
  and grades (via `pipeline.api.service`)
- `data/reports/daily_<date>.md` — the daily run reports (via `pipeline.api.service`)
- The optional SQLite/Postgres mirror (via `pipeline.db.core`) for live row counts

Nothing here is invented storage; if an artifact isn't on disk yet, the page says
so plainly rather than faking numbers.
