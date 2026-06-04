"""Dedicated scheduler process entrypoint.

Runs as its own container in docker-compose (a single replica). This is the
ONLY place APScheduler is started, which structurally guarantees exactly-once
scheduling regardless of how many gunicorn web workers run.

Handles SIGTERM/SIGINT for graceful shutdown so APScheduler stops cleanly.
"""
from __future__ import annotations

import logging
import signal
import threading

from app.config import load_config
from app.db import bootstrap, init_engine
from app.logging_config import configure_logging
from app.scheduler import PollScheduler


def main() -> None:
    configure_logging()
    log = logging.getLogger("rssphaser.scheduler_main")

    config = load_config()
    init_engine(config)
    # Ensure DB is up and schema/seed exist before scheduling (idempotent).
    bootstrap(config)

    scheduler = PollScheduler(config)
    scheduler.start()

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("Received signal %s; shutting down.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("Scheduler process running. Waiting for shutdown signal.")
    try:
        stop_event.wait()
    finally:
        scheduler.shutdown()
        log.info("Scheduler process exited cleanly.")


if __name__ == "__main__":
    main()
