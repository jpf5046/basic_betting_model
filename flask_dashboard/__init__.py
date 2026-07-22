"""Flask utility dashboard — a plain, read-only window onto the pipeline's data.

A separate, additive front end (it does NOT touch ``pipeline/api``, the
FastAPI Researcher Console). Everything it shows is read straight off the
same artifacts under ``data/`` through the existing ``pipeline.api``
service/parsers layer, so the two front ends can never disagree.

Run it::

    pip install -r requirements-web.txt
    python3 -m flask_dashboard          # http://127.0.0.1:5000

or::

    FLASK_APP=flask_dashboard flask run
"""

from __future__ import annotations

from flask_dashboard.app import create_app

__all__ = ["create_app"]
