"""SQLAlchemy ORM models (MariaDB).

Tables:
  - sources:   feed definitions + per-source poll bookkeeping.
  - keywords:  regex patterns to match, with a "problematic" runtime flag.
  - matches:   stored matched entries; serves the output feed.
  - app_state: key/value bag for the "initialized" flag and the live poll interval.

The dedup identifier lives on ``matches.dedup_id`` with a UNIQUE constraint so
concurrent/retried inserts cannot create duplicates (we use INSERT IGNORE
semantics at the data-access layer).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Conditional-request bookkeeping.
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Poll status for the dashboard.
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # ok|error|not_modified
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The literal term to match (no regex). e.g. "ransomware", "CVE-".
    term: Mapped[str] = mapped_column(String(512), nullable=False)
    # Match mode: "contains" (substring) or "word" (whole-word). See app.matching.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="word")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("dedup_id", name="uq_matches_dedup_id"),
        Index("ix_matches_published_at", "published_at"),
        Index("ix_matches_fetched_at", "fetched_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stable per-entry identifier: entry id/guid, else hash(link+title).
    dedup_id: Mapped[str] = mapped_column(String(255), nullable=False)

    title: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    link: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class AppState(Base):
    """Tiny key/value store for app-level flags (e.g. the initialized flag,
    the live poll interval). Values are stored as strings."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(512), nullable=False)


# Well-known app_state keys.
STATE_INITIALIZED = "initialized"
STATE_POLL_INTERVAL = "poll_interval_minutes"
