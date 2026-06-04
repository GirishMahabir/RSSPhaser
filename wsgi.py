"""Gunicorn entrypoint for the web application.

Run with:  gunicorn --bind 0.0.0.0:8000 wsgi:app

This serves ONLY the web UI, the public feed and health checks. It deliberately
does NOT start APScheduler — polling runs in the separate scheduler container
(see scheduler_main.py) so that multiple gunicorn workers can never each spawn
their own scheduler.
"""
from __future__ import annotations

from app.web import create_app

# Gunicorn imports this module in each worker. create_app() is cheap and the DB
# bootstrap (wait/create/seed) is idempotent and guarded by the initialized flag.
app = create_app()


if __name__ == "__main__":
    # Convenience for local debugging only — production uses gunicorn.
    app.run(host="0.0.0.0", port=8000, debug=False)
