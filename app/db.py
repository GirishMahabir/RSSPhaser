"""Database engine/session management, schema creation and first-boot seeding.

On startup we:
  1. Build the engine.
  2. Wait for MariaDB to accept connections (retry with backoff — on first boot
     the DB container may still be initializing).
  3. Auto-create the schema if absent.
  4. Seed sources/keywords/poll interval from config.yaml ONLY if the
     ``initialized`` flag is not set, then set it. This guarantees config never
     overwrites later UI edits.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .config import AppConfig
from .models import (
    STATE_INITIALIZED,
    STATE_POLL_INTERVAL,
    AppState,
    Base,
    Keyword,
    Source,
)

log = logging.getLogger("rssphaser.db")

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def init_engine(config: AppConfig) -> Engine:
    """Create (once) and return the global SQLAlchemy engine."""
    global _engine, _SessionFactory
    if _engine is None:
        _engine = create_engine(
            config.sqlalchemy_url,
            pool_pre_ping=True,   # transparently recover from dropped connections
            pool_recycle=1800,    # avoid MariaDB's wait_timeout killing idle conns
            future=True,
        )
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session() -> Session:
    """Return a new Session. Engine must be initialized first."""
    if _SessionFactory is None:
        raise RuntimeError("Database engine not initialized; call init_engine() first.")
    return _SessionFactory()


def wait_for_db(config: AppConfig, max_attempts: int = 30, base_delay: float = 1.0) -> None:
    """Block until the DB accepts a connection, with capped exponential backoff."""
    engine = init_engine(config)
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            log.info("Database is reachable (attempt %d).", attempt)
            return
        except OperationalError as exc:
            if attempt == max_attempts:
                log.error("Database not reachable after %d attempts.", max_attempts)
                raise
            log.warning(
                "Database not ready (attempt %d/%d): %s; retrying in %.1fs.",
                attempt, max_attempts, exc.orig if exc.orig else exc, delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 15.0)


def create_schema(config: AppConfig) -> None:
    """Create all tables if they do not already exist."""
    engine = init_engine(config)
    Base.metadata.create_all(engine)
    log.info("Schema ensured (create_all complete).")


def seed_if_needed(config: AppConfig) -> None:
    """Seed initial data from config on first boot only.

    Guarded by the ``initialized`` flag in ``app_state`` and run inside a single
    transaction so a concurrent second booter cannot double-seed (the flag row
    write + check are committed atomically; the unique-by-PK app_state row plus
    the early-return make this idempotent).
    """
    with get_session() as session:
        with session.begin():
            already = session.get(AppState, STATE_INITIALIZED)
            if already is not None:
                log.info("DB already initialized; skipping config seed (UI is source of truth).")
                # Ensure poll interval state row exists even on upgrades.
                _ensure_poll_interval_row(session, config)
                return

            log.info("First boot: seeding sources/keywords/poll interval from config.yaml.")
            for s in config.source_seeds:
                session.add(Source(name=s.name, url=s.url, enabled=s.enabled))
            for k in config.keyword_seeds:
                session.add(Keyword(term=k.term, mode=k.mode, enabled=k.enabled))

            session.add(AppState(key=STATE_POLL_INTERVAL, value=str(config.poll_interval_minutes)))
            session.add(AppState(key=STATE_INITIALIZED, value="1"))
        log.info(
            "Seed complete: %d sources, %d keywords.",
            len(config.source_seeds), len(config.keyword_seeds),
        )


def _ensure_poll_interval_row(session: Session, config: AppConfig) -> None:
    if session.get(AppState, STATE_POLL_INTERVAL) is None:
        session.add(AppState(key=STATE_POLL_INTERVAL, value=str(config.poll_interval_minutes)))


def get_poll_interval(config: AppConfig) -> int:
    """Return the live poll interval (DB is source of truth; config is fallback)."""
    with get_session() as session:
        row = session.get(AppState, STATE_POLL_INTERVAL)
        if row is not None:
            try:
                return max(1, int(row.value))
            except ValueError:
                pass
    return config.poll_interval_minutes


def set_poll_interval(minutes: int) -> None:
    with get_session() as session:
        with session.begin():
            row = session.get(AppState, STATE_POLL_INTERVAL)
            if row is None:
                session.add(AppState(key=STATE_POLL_INTERVAL, value=str(minutes)))
            else:
                row.value = str(minutes)


BOOTSTRAP_LOCK = "rssphaser_bootstrap"


def bootstrap(config: AppConfig) -> None:
    """Full startup sequence: wait for DB, then create schema and seed once.

    Several processes boot at the same time (each gunicorn worker plus the
    dedicated scheduler container), and they all race to create tables and seed.
    Concurrent ``CREATE TABLE`` / seed inserts would otherwise collide
    (duplicate primary key, "table already exists"). We serialize the whole
    create+seed step across processes with a MariaDB advisory lock so exactly
    one process does the work and the rest find it already done (both steps are
    idempotent: ``create_all`` skips existing tables and the seed checks the
    ``initialized`` flag).
    """
    wait_for_db(config)
    engine = init_engine(config)
    with engine.connect() as conn:
        got = conn.execute(text("SELECT GET_LOCK(:k, 60)"), {"k": BOOTSTRAP_LOCK}).scalar()
        if got != 1:
            log.warning("Could not acquire bootstrap lock in time; proceeding idempotently.")
        try:
            create_schema(config)
            seed_if_needed(config)
        finally:
            # Release only if we actually hold it (RELEASE_LOCK is a no-op otherwise).
            conn.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": BOOTSTRAP_LOCK})
