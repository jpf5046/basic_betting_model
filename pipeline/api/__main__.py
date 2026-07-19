#!/usr/bin/env python3
"""Launch the console: ``python3 -m pipeline.api [--host H] [--port N]``.

A thin convenience wrapper over uvicorn so the console starts with one
command. Equivalent to ``uvicorn pipeline.api:app``.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python3 -m pipeline.api", description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = p.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "uvicorn is not installed — run `pip install -r requirements-api.txt` "
            "first (the console API layer's dependencies)."
        )
    uvicorn.run("pipeline.api:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
