#!/usr/bin/env python3
"""Parse the pipeline's real artifacts into console-ready structures.

Everything here is a pure function over text / CSV rows — no network, no
FastAPI, no globals — so the whole file-parsing layer is unit-testable
against fixtures (console prompt's test requirement). The API layer reads
files and hands the bytes/rows to these functions.

Sources parsed:
  * ``data/reports/daily_<date>.md`` — the orchestrator's daily report
    (stage-log table, per-section fenced output, Failures tracebacks).
  * ``predictions_<date>.csv`` / ``picks_<date>.csv`` / ``grades_<date>.csv``
    — the model + grader outputs.
"""

from __future__ import annotations

import re
from datetime import date

STAGES = ("ingest", "grade", "predict", "odds", "edge")

# "wrote 2453 games", "10 published picks", "17 predictions"
_COUNT_RES = [
    re.compile(r"wrote\s+(\d+)\s+games"),
    re.compile(r"(\d+)\s+published picks"),
    re.compile(r"(\d+)\s+predictions"),
]
# "(1460 final, 17 today)"
_FINAL_TODAY_RE = re.compile(r"\((\d+)\s+final,\s+(\d+)\s+today\)")


def _split_table_row(line: str) -> list[str] | None:
    line = line.strip()
    if not line.startswith("|"):
        return None
    cells = [c.strip() for c in line.strip("|").split("|")]
    return cells


def _extract_count(detail: str) -> int | None:
    for rx in _COUNT_RES:
        m = rx.search(detail or "")
        if m:
            return int(m.group(1))
    return None


def parse_report(text: str, on: str = "") -> dict:
    """Parse one daily report markdown into a structured dict.

    Returns date, generated stamp, the stage log (flat + a per-sport grid),
    the fenced section outputs (grade / predict / edge), and the Failures
    tracebacks. Tolerant of missing sections — an empty report yields empty
    collections, never an exception.
    """
    lines = text.splitlines()
    result: dict = {
        "date": on,
        "generated": "",
        "stages": [],
        "grid": {},
        "sections": {"grade": [], "predict": [], "edge": []},
        "failures": [],
    }

    # Title + generated stamp.
    for line in lines:
        m = re.match(r"^#\s+Daily run\s+[—-]\s+(\S+)", line)
        if m and not result["date"]:
            result["date"] = m.group(1)
        if line.startswith("Generated "):
            result["generated"] = line.strip()
            break

    # Stage-log table: rows after the header/separator until a blank/heading.
    in_table = False
    for line in lines:
        if line.strip().startswith("## Stage log"):
            in_table = True
            continue
        if in_table:
            cells = _split_table_row(line)
            if cells is None:
                if line.strip().startswith("## "):
                    break
                continue
            if cells[:2] == ["stage", "sport"] or set("".join(cells)) <= {"-", " "}:
                continue
            if len(cells) >= 4:
                stage, sport, status, detail = cells[0], cells[1], cells[2], cells[3]
                if stage not in STAGES:
                    continue
                result["stages"].append(
                    {"stage": stage, "sport": sport, "status": status,
                     "detail": detail, "rows": _extract_count(detail)}
                )

    # Per-sport grid of stage -> {status, detail, rows, final, today}.
    for st in result["stages"]:
        cell = {"status": st["status"], "detail": st["detail"], "rows": st["rows"]}
        m = _FINAL_TODAY_RE.search(st["detail"])
        if m:
            cell["final"] = int(m.group(1))
            cell["today"] = int(m.group(2))
        result["grid"].setdefault(st["sport"], {})[st["stage"]] = cell

    # Fenced-code sections and the Failures blocks.
    result["sections"] = _parse_sections(lines)
    result["failures"] = _parse_failures(lines)
    return result


def _iter_fenced_blocks(lines: list[str], start: int, end: int):
    """Yield (heading, code) for each ``### heading`` + ```` ``` `` block
    between line indices [start, end)."""
    i = start
    heading = ""
    while i < end:
        line = lines[i]
        if line.startswith("### "):
            heading = line[4:].strip()
        elif line.strip() == "```":
            body = []
            i += 1
            while i < end and lines[i].strip() != "```":
                body.append(lines[i])
                i += 1
            yield heading, "\n".join(body)
        i += 1


def _heading_bounds(lines: list[str]) -> list[tuple[str, int, int]]:
    """(title, start, end) for each top-level ``## `` section."""
    marks = [i for i, l in enumerate(lines) if l.startswith("## ")]
    out = []
    for idx, i in enumerate(marks):
        end = marks[idx + 1] if idx + 1 < len(marks) else len(lines)
        out.append((lines[i][3:].strip(), i, end))
    return out


def _parse_sections(lines: list[str]) -> dict:
    wanted = {
        "Yesterday's grade card": "grade",
        "Today's pick sheet": "predict",
        "Model vs market (edge)": "edge",
    }
    out: dict[str, list] = {"grade": [], "predict": [], "edge": []}
    for title, start, end in _heading_bounds(lines):
        key = wanted.get(title)
        if not key:
            continue
        for sport, code in _iter_fenced_blocks(lines, start, end):
            out[key].append({"sport": sport, "output": code})
    return out


def _parse_failures(lines: list[str]) -> list[dict]:
    out = []
    for title, start, end in _heading_bounds(lines):
        if title != "Failures":
            continue
        for heading, code in _iter_fenced_blocks(lines, start, end):
            parts = heading.split()
            stage = parts[0] if parts else ""
            sport = parts[1] if len(parts) > 1 else ""
            out.append({"title": heading, "stage": stage, "sport": sport, "output": code})
    return out


def summarize_report(parsed: dict) -> dict:
    """A one-line-per-run summary for the Runs list."""
    tally = {"ok": 0, "failed": 0, "skipped": 0}
    picks = 0
    predictions = 0
    for st in parsed["stages"]:
        tally[st["status"]] = tally.get(st["status"], 0) + 1
        if st["stage"] == "predict" and st["status"] == "ok" and st["rows"] is not None:
            picks += st["rows"]
    # predictions counts live in the predict section fenced output.
    for block in parsed["sections"]["predict"]:
        m = re.search(r"(\d+)\s+predictions", block["output"])
        if m:
            predictions += int(m.group(1))
    sports = sorted({st["sport"] for st in parsed["stages"]})
    return {
        "date": parsed["date"],
        "generated": parsed["generated"],
        "sports": sports,
        "tally": tally,
        "picks": picks,
        "predictions": predictions,
        "failures": len(parsed["failures"]),
    }


# --------------------------------------------------------- CSV row shaping

def _num(value, cast=float):
    if value is None or value == "":
        return None
    try:
        return cast(value)
    except (ValueError, TypeError):
        return None


def shape_prediction(row: dict) -> dict:
    """One predictions CSV row -> typed dict (team ids left raw; the API
    layer resolves names). Non-`ok` rows (no_prediction) pass through with
    nulls so the table can flag couldn't-run games honestly."""
    return {
        "status": row.get("status", ""),
        "ml_lean": row.get("ml_lean", ""),
        "total_lean": row.get("total_lean", ""),
        "game_id": row.get("game_id", ""),
        "sport": row.get("sport", ""),
        "date": row.get("date", ""),
        "away_team_id": row.get("away_team_id", ""),
        "home_team_id": row.get("home_team_id", ""),
        "pred_away": _num(row.get("pred_away")),
        "pred_home": _num(row.get("pred_home")),
        "disp_away": _num(row.get("disp_away"), int),
        "disp_home": _num(row.get("disp_home"), int),
        "pred_total": _num(row.get("pred_total")),
        "pred_spread": _num(row.get("pred_spread")),
        "win_prob": _num(row.get("win_prob")),
        "winner_team_id": row.get("winner_team_id", ""),
        "pred_confidence": row.get("pred_confidence", ""),
        "method_details": row.get("method_details", ""),
    }


def shape_grade(row: dict) -> dict:
    return {
        "game_id": row.get("game_id", ""),
        "sport": row.get("sport", ""),
        "date": row.get("date", ""),
        "bet_type": row.get("bet_type", ""),
        "selection": row.get("selection", ""),
        "confidence": row.get("confidence", ""),
        "line": row.get("line", ""),
        "game_status": row.get("game_status", ""),
        "actual_away": row.get("actual_away", ""),
        "actual_home": row.get("actual_home", ""),
        "result": row.get("result", ""),
        "pnl": _num(row.get("pnl")) or 0.0,
        "price_american": row.get("price_american", ""),
    }


# --------------------------------------------------------------- ROI math

CONF_TIERS = ("STRONG", "LEAN", "")  # "" = totals (no ML confidence)


def _bucket(rows: list[dict]) -> dict:
    wins = sum(1 for g in rows if g["result"] == "WIN")
    losses = sum(1 for g in rows if g["result"] == "LOSS")
    pushes = sum(1 for g in rows if g["result"] == "PUSH")
    voids = sum(1 for g in rows if g["result"] == "VOID")
    pending = sum(1 for g in rows if g["result"] == "PENDING")
    pnl = round(sum(g["pnl"] for g in rows), 2)
    graded = wins + losses + pushes
    staked = (wins + losses + pushes) * 100.0  # flat $100, pushes stake returned
    roi = round(100.0 * pnl / staked, 2) if staked else None
    return {
        "wins": wins, "losses": losses, "pushes": pushes,
        "voids": voids, "pending": pending, "graded": graded,
        "pnl": pnl, "roi": roi,
    }


def performance(grades: list[dict]) -> dict:
    """Cumulative P&L series + record/ROI breakdowns from graded picks.

    Only WIN/LOSS/PUSH count toward the record and ROI; VOID/PENDING are
    surfaced but never distort the numbers (grader semantics)."""
    settled = [g for g in grades if g["result"] in ("WIN", "LOSS", "PUSH")]
    settled.sort(key=lambda g: (g["date"], g["game_id"], g["bet_type"]))

    curve = []
    running = 0.0
    for g in settled:
        running = round(running + g["pnl"], 2)
        curve.append({"date": g["date"], "pnl": g["pnl"], "cumulative": running})

    def group(key_fn):
        keys = sorted({key_fn(g) for g in grades})
        return {k: _bucket([g for g in grades if key_fn(g) == k]) for k in keys}

    passes = 0  # non-picks aren't in grades; pass rate is reported from predictions elsewhere
    return {
        "overall": _bucket(grades),
        "curve": curve,
        "by_sport": group(lambda g: g["sport"]),
        "by_bet_type": group(lambda g: g["bet_type"]),
        "by_confidence": group(lambda g: g["confidence"] or "TOTAL"),
    }
