"""``python3 -m flask_dashboard`` — boot the dev server.

Host/port overridable via the usual env vars; defaults to 127.0.0.1:5000.
"""

from __future__ import annotations

import os

from flask_dashboard.app import create_app

if __name__ == "__main__":
    app = create_app()
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
