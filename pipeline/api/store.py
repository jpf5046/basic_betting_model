#!/usr/bin/env python3
"""Config lifecycle — the file-backed store behind the Configs screen.

Saved configs are real JSON override files the pipeline can consume with
``python3 -m pipeline.models predict --config <file>``. Each file is a pure
sport-keyed params dict (``{"MLB": {...}, "NBA": {...}}``) so it stays
pipeline-compatible; console-only metadata (note, created, locked,
archived) lives in a sidecar ``_index.json`` and never pollutes the params
the model reads. The live map and promotion history are their own small
JSON files.

Validation always goes through ``pipeline.models.config.validate_params``
— the rules are written once, in the backend (console prompt), never
re-implemented in JS.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from pipeline.api.paths import SPORTS, configs_dir, safe_name, valid_sport
from pipeline.models.config import (
    FACTORY_DEFAULTS,
    ConfigError,
    load_params,
    validate_params,
)

INDEX_FILE = "_index.json"
LIVE_FILE = "_live.json"
PROMOTIONS_FILE = "_promotions.json"
RESERVED = {INDEX_FILE, LIVE_FILE, PROMOTIONS_FILE}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ConfigStore:
    def __init__(self, data_dir: Path):
        self.dir = configs_dir(data_dir)

    # ---------------------------------------------------------- internals

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, name: str, default):
        path = self.dir / name
        if not path.exists():
            return copy.deepcopy(default)
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return copy.deepcopy(default)

    def _write_json(self, name: str, obj) -> None:
        self._ensure()
        (self.dir / name).write_text(json.dumps(obj, indent=2) + "\n")

    def _index(self) -> dict:
        return self._read_json(INDEX_FILE, {})

    def _config_file(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    # --------------------------------------------------------------- read

    def live_map(self) -> dict:
        live = self._read_json(LIVE_FILE, {})
        return {s: live.get(s) for s in SPORTS if live.get(s)}

    def promotions(self) -> list:
        return self._read_json(PROMOTIONS_FILE, [])

    def exists(self, name: str) -> bool:
        return safe_name(name) and self._config_file(name).exists()

    def get_params(self, name: str) -> dict:
        """The raw sport-keyed params dict stored for a config."""
        if not self.exists(name):
            raise KeyError(name)
        return json.loads(self._config_file(name).read_text())

    def config_path(self, name: str) -> Path:
        """The on-disk path, usable directly as ``--config`` for the CLI."""
        if not self.exists(name):
            raise KeyError(name)
        return self._config_file(name)

    def describe(self, name: str) -> dict:
        meta = self._index().get(name, {})
        params = self.get_params(name)
        live = self.live_map()
        return {
            "name": name,
            "note": meta.get("note", ""),
            "created": meta.get("created", ""),
            "updated": meta.get("updated", ""),
            "locked": bool(meta.get("locked", False)),
            "archived": bool(meta.get("archived", False)),
            "live_sports": sorted(s for s, n in live.items() if n == name),
            "params": params,
        }

    def list_configs(self) -> list:
        out = []
        for path in sorted(self.dir.glob("*.json")) if self.dir.exists() else []:
            if path.name in RESERVED:
                continue
            out.append(self.describe(path.stem))
        return out

    # -------------------------------------------------------------- write

    def _validate_all(self, params_by_sport: dict) -> dict:
        """Full params for every sport, defaults filled in, all validated."""
        full = {}
        for sport in SPORTS:
            merged = copy.deepcopy(FACTORY_DEFAULTS[sport])
            given = params_by_sport.get(sport)
            if given:
                merged = _deep_merge(merged, given)
            validate_params(sport, merged)
            full[sport] = merged
        return full

    def save(self, name: str, params_by_sport: dict, note: str = "",
             overwrite: bool = False) -> dict:
        if not safe_name(name):
            raise ConfigError(f"invalid config name {name!r}")
        index = self._index()
        meta = index.get(name)
        if self.exists(name):
            if not overwrite:
                raise ConfigError(f"config {name!r} already exists")
            if meta and meta.get("locked"):
                raise ConfigError(f"config {name!r} is locked (has been live) — immutable")
        full = self._validate_all(params_by_sport)
        self._write_json(f"{name}.json", full)
        now = _now()
        if meta is None:
            meta = {"note": note, "created": now, "updated": now,
                    "locked": False, "archived": False}
        else:
            meta = {**meta, "note": note or meta.get("note", ""), "updated": now}
        index[name] = meta
        self._write_json(INDEX_FILE, index)
        return self.describe(name)

    def duplicate(self, name: str, new_name: str, note: str = "") -> dict:
        if not self.exists(name):
            raise KeyError(name)
        params = self.get_params(name)
        return self.save(new_name, params, note=note or f"copy of {name}", overwrite=False)

    def archive(self, name: str, archived: bool = True) -> dict:
        if not self.exists(name):
            raise KeyError(name)
        index = self._index()
        meta = index.get(name, {"created": _now()})
        meta["archived"] = archived
        meta["updated"] = _now()
        index[name] = meta
        self._write_json(INDEX_FILE, index)
        return self.describe(name)

    def set_live(self, name: str, sport: str) -> dict:
        if not self.exists(name):
            raise KeyError(name)
        if not valid_sport(sport):
            raise ConfigError(f"unknown sport {sport!r}")
        sport = sport.upper()
        # validate the sport's params are loadable through the real loader.
        load_params(sport, str(self.config_path(name)))

        live = self._read_json(LIVE_FILE, {})
        previous = live.get(sport)
        live[sport] = name
        self._write_json(LIVE_FILE, live)

        # Promotion locks the config (matches the design's immutability rule).
        index = self._index()
        meta = index.get(name, {"created": _now()})
        meta["locked"] = True
        meta["updated"] = _now()
        index[name] = meta
        self._write_json(INDEX_FILE, index)

        promos = self.promotions()
        promos.insert(0, {
            "date": _now(), "sport": sport,
            "new": name, "old": previous or "",
        })
        self._write_json(PROMOTIONS_FILE, promos)
        return {"live_map": self.live_map(), "promotions": promos}

    def rollback(self, sport: str) -> dict:
        """Re-promote the config that the latest promotion replaced."""
        if not valid_sport(sport):
            raise ConfigError(f"unknown sport {sport!r}")
        sport = sport.upper()
        for entry in self.promotions():
            if entry["sport"] == sport and entry.get("old"):
                return self.set_live(entry["old"], sport)
        raise ConfigError(f"nothing to roll back for {sport}")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out
