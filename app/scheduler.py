"""APScheduler wiring for the dedicated scheduler process.

IMPORTANT — single-scheduler guarantee:
    This module is started ONLY by ``scheduler_main.py``, which runs as its own
    dedicated container/process in docker-compose. The gunicorn web workers do
    NOT import or start this. That is the structural guard against the classic
    "every gunicorn worker spawns its own scheduler -> double polling" bug.

The polling job is configured with ``max_instances=1`` and ``coalesce=True`` so
a slow poll can never overlap or stack with the next tick. The interval is read
live from the DB so the web UI can reschedule it without a restart.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import AppConfig
from .db import get_poll_interval
from .poller import run_poll_cycle

log = logging.getLogger("rssphaser.scheduler")

POLL_JOB_ID = "poll-all-sources"


class PollScheduler:
    """Thin wrapper around APScheduler managing the single poll job."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self._current_interval: int | None = None

    def start(self) -> None:
        interval = get_poll_interval(self.config)
        self._current_interval = interval
        self.scheduler.add_job(
            self._run,
            trigger="interval",
            minutes=interval,
            id=POLL_JOB_ID,
            max_instances=1,   # never overlap
            coalesce=True,     # collapse missed runs into one
            next_run_time=None,
            replace_existing=True,
        )
        # A lightweight watcher job keeps the interval in sync with the DB so UI
        # changes take effect without a restart.
        self.scheduler.add_job(
            self._sync_interval,
            trigger="interval",
            seconds=30,
            id="interval-sync",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self.scheduler.start()
        log.info("Scheduler started; poll interval = %d minute(s).", interval)

        # Kick off an immediate first poll so the feed populates promptly.
        self.scheduler.add_job(self._run, id="initial-poll", replace_existing=True)

    def _run(self) -> None:
        try:
            run_poll_cycle(self.config)
        except Exception as exc:  # noqa: BLE001 - the job must never die
            log.exception("Poll cycle raised unexpectedly: %s", exc)

    def _sync_interval(self) -> None:
        """Reschedule the poll job if the DB interval changed."""
        try:
            desired = get_poll_interval(self.config)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read poll interval: %s", exc)
            return
        if desired != self._current_interval:
            log.info("Poll interval changed %s -> %d min; rescheduling.", self._current_interval, desired)
            self.scheduler.reschedule_job(POLL_JOB_ID, trigger="interval", minutes=desired)
            self._current_interval = desired

    def shutdown(self) -> None:
        if self.scheduler.running:
            log.info("Shutting down scheduler gracefully.")
            self.scheduler.shutdown(wait=True)
