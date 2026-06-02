"""Structured-ish logging to stdout.

We keep the format human-readable but machine-greppable: every line carries an
ISO-8601 timestamp, level, logger name and message. All logs go to stdout so
they are captured by Docker/`docker compose logs`.
"""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def configure_logging() -> None:
    """Configure root logging once. Safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down noisy third-party loggers a touch.
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    _CONFIGURED = True
