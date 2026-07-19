#!/usr/bin/env python3
"""Background job runner — triggers the real pipeline CLIs as subprocesses.

The console can both *view* and *drive* the pipeline. Drivers never
re-implement pipeline logic (console prompt): each action shells out to an
existing CLI with an argument list (never ``shell=True``, never a raw
client string on the command line). All parameters are whitelisted here
before a command line is built — dates must be real ``YYYY-MM-DD``, sports
must be known keys — so nothing the client sends reaches a shell.

Jobs run on background threads; the API exposes a poll endpoint. Output is
captured line-by-line so the Runs screen can show live stage output.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pipeline.api.paths import SPORTS, valid_date, valid_sport


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Job:
    def __init__(self, kind: str, argv: list[str], meta: dict):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.argv = argv           # the exact process argv (list, no shell)
        self.meta = meta           # {date, sports, ...} for UI auto-refresh
        self.status = "queued"     # queued | running | succeeded | failed
        self.returncode: int | None = None
        self.created = _now()
        self.started = ""
        self.finished = ""
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def output(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def as_dict(self, include_output: bool = True) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "command": " ".join(self.argv),
            "status": self.status,
            "returncode": self.returncode,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "meta": self.meta,
        }
        if include_output:
            d["output"] = self.output()
        return d


class JobManager:
    """In-memory registry of triggered jobs (single-user, one process)."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    # ---------------------------------------------------- command builders

    def _sports_arg(self, sports) -> str:
        keys = [s.strip().upper() for s in (sports or []) if s and s.strip()]
        bad = [s for s in keys if not valid_sport(s)]
        if bad:
            raise ValueError(f"unknown sport(s): {', '.join(bad)}")
        return ",".join(keys) if keys else ",".join(SPORTS)

    def build_run_daily(self, params: dict) -> tuple[str, list[str], dict]:
        argv = [sys.executable, "run_daily.py"]
        meta = {}
        if params.get("date"):
            if not valid_date(params["date"]):
                raise ValueError("date must be YYYY-MM-DD")
            argv += ["--date", params["date"]]
            meta["date"] = params["date"]
        sports_arg = self._sports_arg(params.get("sports"))
        argv += ["--sports", sports_arg]
        meta["sports"] = sports_arg.split(",")
        if params.get("skip_odds"):
            argv.append("--skip-odds")
        if params.get("offline"):
            argv.append("--offline")
        if params.get("allow_partial"):
            argv.append("--allow-partial")
        return "run-daily", argv, meta

    def build_predict(self, params: dict) -> tuple[str, list[str], dict]:
        sport = (params.get("sport") or "").upper()
        if not valid_sport(sport):
            raise ValueError("sport is required and must be a known key")
        argv = [sys.executable, "-m", "pipeline.models", "predict", "--sport", sport]
        meta = {"sports": [sport]}
        if params.get("date"):
            if not valid_date(params["date"]):
                raise ValueError("date must be YYYY-MM-DD")
            argv += ["--date", params["date"]]
            meta["date"] = params["date"]
        if params.get("config"):
            cfg = Path(params["config"])
            # only allow configs that live under the repo's data/configs tree
            allowed = (self.repo_root / "data" / "configs").resolve()
            if not str(cfg.resolve()).startswith(str(allowed)):
                raise ValueError("config must be a saved config file")
            argv += ["--config", str(cfg)]
        return "predict", argv, meta

    def build_backfill(self, params: dict) -> tuple[str, list[str], dict]:
        sports_arg = self._sports_arg(params.get("sports"))
        argv = [sys.executable, "-m", "pipeline.backfill", "--sports", sports_arg]
        if params.get("through"):
            if not valid_date(params["through"]):
                raise ValueError("through must be YYYY-MM-DD")
            argv += ["--through", params["through"]]
        return "backfill", argv, {"sports": sports_arg.split(",")}

    def build_db_load(self, params: dict) -> tuple[str, list[str], dict]:
        sports_arg = self._sports_arg(params.get("sports"))
        argv = [sys.executable, "-m", "pipeline.db", "load", "--sports", sports_arg]
        return "db-load", argv, {"sports": sports_arg.split(",")}

    # --------------------------------------------------------- run + query

    def start(self, kind: str, argv: list[str], meta: dict) -> Job:
        job = Job(kind, argv, meta)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.started = _now()
        job.append(f"$ {' '.join(job.argv)}")
        try:
            proc = subprocess.Popen(
                job.argv, cwd=str(self.repo_root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                job.append(line.rstrip("\n"))
            proc.wait()
            job.returncode = proc.returncode
            job.status = "succeeded" if proc.returncode == 0 else "failed"
        except Exception as exc:  # pragma: no cover - defensive
            job.append(f"[job error] {exc!r}")
            job.returncode = -1
            job.status = "failed"
        finally:
            job.finished = _now()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
            jobs = [self._jobs[i] for i in ids]
        return [j.as_dict(include_output=False) for j in jobs]
