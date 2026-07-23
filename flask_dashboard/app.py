"""Flask app factory + routes for the utility dashboard.

Thin controllers: each route pulls a ready-made structure from
``DataAccess`` and hands it to a Bootstrap template. No model logic, no
writes, no JavaScript. Kept deliberately boring so it reads as a plain
window onto the data.
"""

from __future__ import annotations

from flask import Flask, abort, render_template, request

from pipeline.api.paths import valid_date, valid_sport
from flask_dashboard.data_access import DataAccess, sparkline


def create_app(data_dir=None) -> Flask:
    app = Flask(__name__)
    data = DataAccess(data_dir)

    # Make a couple of helpers available to every template.
    @app.context_processor
    def _inject():
        return {
            "today": data.today(),
            "all_sports": data.sports,
            "nav_items": _NAV,
        }

    @app.template_filter("money")
    def _money(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "—"
        return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"

    @app.template_filter("pct")
    def _pct(v):
        return "—" if v is None else f"{v:.1f}%"

    @app.template_filter("signed")
    def _signed(v):
        if v is None:
            return "—"
        return f"+{v}" if v > 0 else str(v)

    # ------------------------------------------------------------- overview

    @app.route("/")
    def overview():
        perf = data.performance()
        acc = data.winner_accuracy()
        return render_template(
            "overview.html",
            page="overview",
            counts=data.game_counts(),
            todays=data.todays_games(),
            perf=perf,
            accuracy=acc,
            spark=sparkline([p["cumulative"] for p in perf["curve"]]),
            runs=data.runs()[:5],
            db=data.db_status(),
        )

    # --------------------------------------------------------------- today

    @app.route("/today")
    def today():
        return render_template(
            "today.html",
            page="today",
            todays=data.todays_games(),
        )

    # --------------------------------------------------------------- games

    @app.route("/games")
    def games():
        sport = (request.args.get("sport") or "MLB").upper()
        when = request.args.get("when", "today")
        if when not in ("today", "past", "future", "all"):
            when = "today"
        if not valid_sport(sport):
            abort(404, f"unknown sport {sport}")
        return render_template(
            "games.html",
            page="games",
            counts=data.game_counts(),
            view=data.games_view(sport, when),
            sport=sport,
            when=when,
        )

    # ---------------------------------------------------------- performance

    @app.route("/performance")
    def performance():
        perf = data.performance()
        return render_template(
            "performance.html",
            page="performance",
            perf=perf,
            accuracy=data.winner_accuracy(),
            spark=sparkline([p["cumulative"] for p in perf["curve"]]),
        )

    # ---------------------------------------------------------- predictions

    @app.route("/predictions")
    def predictions():
        dates_all = data.prediction_dates_all()
        sport = (request.args.get("sport") or "").upper()
        date = request.args.get("date") or ""

        # Default to the sport/date that actually has data.
        if not sport or not valid_sport(sport):
            sport = next((s for s, ds in dates_all.items() if ds), "MLB")
        if not date or not valid_date(date):
            date = (dates_all.get(sport) or [""])[0]

        table = data.predictions(sport, date) if date else None
        return render_template(
            "predictions.html",
            page="predictions",
            dates_all=dates_all,
            sport=sport,
            date=date,
            table=table,
        )

    # -------------------------------------------------------------- results

    @app.route("/results")
    def results():
        graded_all = data.graded_dates_all()
        sport = (request.args.get("sport") or "").upper()
        date = request.args.get("date") or ""

        # Default to the sport that actually has settled results.
        if not sport or not valid_sport(sport):
            sport = next((s for s, ds in graded_all.items() if ds), "MLB")
        dates = graded_all.get(sport) or []

        # Default to yesterday when it has results, else the latest graded day.
        if not date or not valid_date(date):
            yday = data.yesterday()
            date = yday if yday in dates else (dates[0] if dates else "")

        table = data.results(sport, date) if date else None
        return render_template(
            "results.html",
            page="results",
            graded_all=graded_all,
            sport=sport,
            date=date,
            yesterday=data.yesterday(),
            table=table,
        )

    # ----------------------------------------------------------------- runs

    @app.route("/runs")
    def runs():
        on = request.args.get("date") or ""
        detail = data.run_detail(on) if on and valid_date(on) else None
        return render_template(
            "runs.html",
            page="runs",
            runs=data.runs(),
            detail=detail,
            selected=on if detail else "",
        )

    # ------------------------------------------------------------- database

    @app.route("/database")
    def database():
        return render_template(
            "database.html",
            page="database",
            db=data.db_status(),
            tables=data.db_tables(),
            datasets=data.csv_datasets(),
            relationships=data.relationships(),
        )

    @app.errorhandler(404)
    def _not_found(exc):
        return render_template("error.html", page="", message=str(exc)), 404

    return app


_NAV = [
    ("overview", "Overview", "/"),
    ("today", "Today's Games", "/today"),
    ("games", "Browse Games", "/games"),
    ("results", "Results", "/results"),
    ("performance", "Performance", "/performance"),
    ("predictions", "Predictions", "/predictions"),
    ("runs", "Daily Runs", "/runs"),
    ("database", "Database", "/database"),
]


app = create_app()


if __name__ == "__main__":
    app.run(debug=True)
