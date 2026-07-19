#!/usr/bin/env python3
"""The console API layer (PLAN.md §8).

This is the ONLY package in the pipeline that may take third-party
dependencies (``fastapi`` + ``uvicorn``, pinned in ``requirements-api.txt``);
the rest of the pipeline stays stdlib-only. ``app`` is imported lazily so
the pure parsing/store submodules (``pipeline.api.parsers`` etc.) can be
imported — and unit-tested — without FastAPI installed.

    uvicorn pipeline.api:app --reload
"""

from __future__ import annotations


def __getattr__(name: str):  # PEP 562 lazy attribute
    if name == "app":
        from pipeline.api.app import app
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
