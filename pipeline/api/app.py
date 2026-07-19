#!/usr/bin/env python3
"""FastAPI app for the My Model console (PLAN.md §8).

Endpoints are deliberately thin: they read real artifacts through
``pipeline.api.service`` / ``store`` and delegate every rule to the
existing pipeline modules (the config validator, the CLIs) so the console
and the command line can never disagree. No model logic lives here.

Run it:  ``uvicorn pipeline.api:app --reload``  (then open http://127.0.0.1:8000)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from pipeline.api import service
from pipeline.api.jobs import JobManager
from pipeline.api.paths import (
    SPORTS,
    default_data_dir,
    valid_date,
    valid_sport,
)
from pipeline.api.store import ConfigStore
from pipeline.api.teams import TeamRegistry
from pipeline.ingest.core import EASTERN, REPO_ROOT
from pipeline.models.config import (
    FACTORY_DEFAULTS,
    ConfigError,
    load_params,
    validate_params,
)

DATA_DIR = default_data_dir()
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="My Model — Console", version="1.0")
jobs = JobManager(REPO_ROOT)


def _teams() -> TeamRegistry:
    return TeamRegistry.load(DATA_DIR)


def _store() -> ConfigStore:
    return ConfigStore(DATA_DIR)


def _live_params(sport: str) -> dict:
    """Loaded params for a sport: its live config if one is set, else
    factory defaults. Always through the real ``load_params`` loader."""
    store = _store()
    name = store.live_map().get(sport)
    if name:
        try:
            return load_params(sport, str(store.config_path(name)))
        except (KeyError, ConfigError):
            pass
    return load_params(sport)


# ------------------------------------------------------------------- meta

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/meta")
def meta():
    store = _store()
    today = datetime.now(EASTERN).date().isoformat()
    return {
        "sports": list(SPORTS),
        "today": today,
        "live_map": store.live_map(),
        "run_dates": service.list_report_dates(DATA_DIR),
        "pick_dates": {s: service.available_dates(DATA_DIR, s) for s in SPORTS},
    }


# ---------------------------------------------------------------- configs

@app.get("/api/config/defaults")
def config_defaults():
    return FACTORY_DEFAULTS


@app.get("/api/config/live")
def config_live_all():
    return {s: _live_params(s) for s in SPORTS}


@app.get("/api/config/live/{sport}")
def config_live(sport: str):
    if not valid_sport(sport):
        raise HTTPException(404, f"unknown sport {sport}")
    return _live_params(sport.upper())


@app.post("/api/config/validate")
def config_validate(body: dict = Body(...)):
    sport = (body.get("sport") or "").upper()
    if not valid_sport(sport):
        raise HTTPException(400, "unknown sport")
    params = body.get("params") or {}
    try:
        validate_params(sport, params)
    except ConfigError as exc:
        # split the joined message back into per-rule errors
        msg = str(exc).split("invalid: ", 1)[-1]
        return {"ok": False, "errors": [e.strip() for e in msg.split(";")]}
    return {"ok": True, "errors": []}


@app.get("/api/configs")
def configs_list():
    store = _store()
    return {
        "configs": store.list_configs(),
        "live_map": store.live_map(),
        "promotions": store.promotions(),
    }


@app.post("/api/configs")
def configs_save(body: dict = Body(...)):
    store = _store()
    try:
        return store.save(
            body.get("name", ""), body.get("params", {}),
            note=body.get("note", ""), overwrite=bool(body.get("overwrite", False)),
        )
    except ConfigError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/configs/{name}/duplicate")
def configs_duplicate(name: str, body: dict = Body(...)):
    store = _store()
    try:
        return store.duplicate(name, body.get("new_name", ""), note=body.get("note", ""))
    except KeyError:
        raise HTTPException(404, f"config {name} not found")
    except ConfigError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/configs/{name}/archive")
def configs_archive(name: str, body: dict = Body(default={})):
    store = _store()
    try:
        return store.archive(name, archived=bool(body.get("archived", True)))
    except KeyError:
        raise HTTPException(404, f"config {name} not found")


@app.post("/api/configs/{name}/set-live")
def configs_set_live(name: str, body: dict = Body(...)):
    store = _store()
    try:
        return store.set_live(name, body.get("sport", ""))
    except KeyError:
        raise HTTPException(404, f"config {name} not found")
    except ConfigError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/configs/rollback")
def configs_rollback(body: dict = Body(...)):
    store = _store()
    try:
        return store.rollback(body.get("sport", ""))
    except ConfigError as exc:
        raise HTTPException(400, str(exc))


# ------------------------------------------------------------------- runs

@app.get("/api/runs")
def runs_list():
    return {"runs": service.list_runs(DATA_DIR)}


@app.get("/api/runs/{on}")
def run_detail(on: str):
    if not valid_date(on):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    parsed = service.read_report(DATA_DIR, on)
    if parsed is None:
        raise HTTPException(404, f"no report for {on}")
    return parsed


# ---------------------------------------------------- picks & predictions

@app.get("/api/picks")
def picks(sport: str = Query(...), date: str = Query(...)):
    if not valid_sport(sport):
        raise HTTPException(400, "unknown sport")
    if not valid_date(date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return service.read_predictions(DATA_DIR, sport.upper(), date, _teams())


@app.get("/api/reasoning")
def reasoning(sport: str = Query(...), date: str = Query(...),
              game_id: str = Query(...)):
    if not valid_sport(sport):
        raise HTTPException(400, "unknown sport")
    if not valid_date(date):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    params = _live_params(sport.upper())
    result = service.read_reasoning(DATA_DIR, sport.upper(), date, game_id,
                                    params, _teams())
    if result is None:
        raise HTTPException(404, "no prediction for that game")
    return result


# --------------------------------------------------------------- live view

@app.get("/api/live")
def live():
    store = _store()
    teams = _teams()
    meta_dates = {s: service.available_dates(DATA_DIR, s) for s in SPORTS}
    out = {}
    for sport in SPORTS:
        dates = meta_dates[sport]
        latest = dates[0] if dates else None
        card = {
            "sport": sport,
            "live_config": store.live_map().get(sport),
            "latest_date": latest,
            "picks": [],
            "predictions": [],
        }
        if latest:
            data = service.read_predictions(DATA_DIR, sport, latest, teams)
            card["picks"] = data["picks"]
            card["predictions"] = data["predictions"]
            card["counts"] = data["counts"]
        out[sport] = card
    return out


# ----------------------------------------------------------- performance

@app.get("/api/performance")
def performance():
    return service.performance(DATA_DIR)


# ------------------------------------------------------------------ model

@app.get("/api/model-weights")
def model_weights():
    from pipeline.api.reasoning import weight_blend
    out = {}
    for sport in SPORTS:
        params = _live_params(sport)
        out[sport] = {
            "live_config": _store().live_map().get(sport),
            "params": params,
            "blend": weight_blend(params),
        }
    return out


# --------------------------------------------------------------- database

_DB_TABLES = ("teams", "games", "frames")


def _connect_db():
    from pipeline.db.core import Db
    db = Db.connect()
    db.init_schema()
    return db


@app.get("/api/db/status")
def db_status():
    try:
        with _connect_db() as db:
            target = ("postgres (DATABASE_URL)" if db.dialect == "postgres"
                      else "sqlite data/model.db")
            return {"target": target, "counts": db.counts()}
    except SystemExit as exc:  # missing psycopg, etc.
        raise HTTPException(500, str(exc))


@app.get("/api/db/table/{table}")
def db_table(table: str, limit: int = Query(50, ge=1, le=500),
             offset: int = Query(0, ge=0)):
    if table not in _DB_TABLES:
        raise HTTPException(404, f"unknown table {table}")
    with _connect_db() as db:
        total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # table name is whitelisted above; limit/offset are parameterized.
        cur = db.execute(
            f"SELECT * FROM {table} ORDER BY 1 LIMIT ? OFFSET ?", (limit, offset)
        )
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
    return {"table": table, "columns": columns, "rows": rows,
            "total": total, "limit": limit, "offset": offset}


# ------------------------------------------------------------- backtest

@app.get("/api/backtest/status")
def backtest_status():
    """The backtester CLI is planned (PLAN.md §6/§9) but not yet built —
    the console gates the screen on this rather than faking numbers."""
    return {
        "available": False,
        "reason": "The backtester (`python -m pipeline backtest`) is not yet "
                  "implemented (PLAN.md §6/§9). This screen unlocks the moment "
                  "it lands — no fabricated results until then.",
    }


# ------------------------------------------------------------------- jobs

_JOB_BUILDERS = {
    "run-daily": "build_run_daily",
    "predict": "build_predict",
    "backfill": "build_backfill",
    "db-load": "build_db_load",
}


@app.post("/api/jobs/{kind}")
def jobs_start(kind: str, body: dict = Body(default={})):
    builder = _JOB_BUILDERS.get(kind)
    if not builder:
        raise HTTPException(404, f"unknown job kind {kind}")
    try:
        job_kind, argv, meta = getattr(jobs, builder)(body or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    job = jobs.start(job_kind, argv, meta)
    return job.as_dict()


@app.get("/api/jobs")
def jobs_list():
    return {"jobs": jobs.list()}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job.as_dict()


# --------------------------------------------------------- static SPA last

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="spa")


@app.exception_handler(404)
def spa_fallback(request, exc):
    # API 404s stay JSON; unknown non-API paths fall back to the SPA shell.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=404)
    index = STATIC_DIR / "index.html"
    if index.exists():
        return JSONResponse(status_code=404, content={"detail": "not found"})
    return JSONResponse({"detail": "not found"}, status_code=404)
