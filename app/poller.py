"""Feed polling: fetch, parse, match, store, prune.

Design goals (per spec):
  * Per-source HTTP connect+read timeout, retry with backoff (3 tries),
    descriptive User-Agent, conditional requests via stored ETag/Last-Modified
    (304 => no-op).
  * Concurrent fetch across sources with a bounded thread pool.
  * Fully fault-tolerant: a bad/unreachable/malformed feed records its error for
    the dashboard but never crashes the cycle. Regex compilation is guarded so a
    bad stored pattern cannot crash polling.
  * Insert-ignore dedup so retried/concurrent inserts never duplicate.
  * Retention pruning so the matches table stays bounded.
"""
from __future__ import annotations

import hashlib
import logging
import time
from calendar import timegm
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from .config import AppConfig
from .db import get_session
from .matching import entry_matches
from .models import Keyword, Match, Source

log = logging.getLogger("rssphaser.poller")

USER_AGENT = "RSSPhaser/1.0 (+https://github.com/GirishMahabir/RSSPhaser; RSS keyword filter)"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _struct_to_datetime(parsed) -> datetime | None:
    """Convert a feedparser time.struct_time (UTC) to an aware datetime."""
    if not parsed:
        return None
    try:
        return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)
    except (ValueError, OverflowError, TypeError):
        return None


def _entry_dedup_id(entry) -> str:
    """Stable per-entry identifier: prefer id/guid, else hash(link+title)."""
    raw_id = entry.get("id") or entry.get("guid")
    if raw_id:
        return hashlib.sha256(raw_id.encode("utf-8", "replace")).hexdigest()
    basis = (entry.get("link", "") + "\n" + entry.get("title", "")).encode("utf-8", "replace")
    return hashlib.sha256(basis).hexdigest()


def _entry_body(entry) -> str:
    """Extract the best available body/summary text from an entry."""
    if entry.get("content"):
        # content is a list of dicts with a 'value' key
        try:
            return entry["content"][0].get("value", "") or ""
        except (IndexError, AttributeError, TypeError):
            pass
    return entry.get("summary", "") or entry.get("description", "") or ""


def _fetch(source: Source, config: AppConfig) -> requests.Response | None:
    """Fetch a feed with retries/backoff and conditional headers.

    Returns the Response, or None if all retries failed (caller records error).
    Raises nothing for HTTP-level issues — those are turned into None/raised
    inside and handled by the caller via the returned response/status.
    """
    headers = {"User-Agent": USER_AGENT}
    if source.etag:
        headers["If-None-Match"] = source.etag
    if source.last_modified:
        headers["If-Modified-Since"] = source.last_modified

    timeout = (config.http_connect_timeout_seconds, config.http_read_timeout_seconds)
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, config.http_max_retries + 1):
        try:
            resp = requests.get(source.url, headers=headers, timeout=timeout)
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            log.warning(
                "Fetch failed for source %d (%s) attempt %d/%d: %s",
                source.id, source.url, attempt, config.http_max_retries, exc,
            )
            if attempt < config.http_max_retries:
                time.sleep(delay)
                delay = min(delay * 2, 10.0)
    if last_exc is not None:
        raise last_exc
    return None


def poll_source(source_id: int, config: AppConfig, keywords: list[tuple[int, str, str]]) -> dict:
    """Poll a single source. Always returns a status dict; never raises.

    ``keywords`` is a list of ``(id, term, mode)`` tuples. Updates the source
    row's poll bookkeeping and inserts new matches.
    """
    started = time.monotonic()
    status = {"source_id": source_id, "status": "ok", "matches": 0, "error": None}

    # Load a fresh copy of the source within its own session.
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            status["status"] = "error"
            status["error"] = "source disappeared"
            return status
        url = source.url
        name = source.name

    try:
        resp = _fetch_loaded(source_id, config)
    except Exception as exc:  # noqa: BLE001 - we must never crash the cycle
        duration_ms = int((time.monotonic() - started) * 1000)
        log.warning("Source %d (%s) fetch error after retries: %s", source_id, url, exc)
        _record_status(source_id, "error", duration_ms, str(exc)[:1000], 0)
        status.update(status="error", error=str(exc))
        return status

    # 304 Not Modified -> nothing to do.
    if resp.status_code == 304:
        duration_ms = int((time.monotonic() - started) * 1000)
        log.info("Source %d (%s): 304 Not Modified (%dms).", source_id, name, duration_ms)
        _record_status(source_id, "not_modified", duration_ms, None, 0)
        status["status"] = "not_modified"
        return status

    if resp.status_code >= 400:
        duration_ms = int((time.monotonic() - started) * 1000)
        msg = f"HTTP {resp.status_code}"
        log.warning("Source %d (%s): %s", source_id, name, msg)
        _record_status(source_id, "error", duration_ms, msg, 0)
        status.update(status="error", error=msg)
        return status

    # Parse (feedparser is tolerant; it won't raise on malformed input).
    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        duration_ms = int((time.monotonic() - started) * 1000)
        msg = f"malformed feed: {getattr(parsed, 'bozo_exception', 'unknown')}"
        log.warning("Source %d (%s): %s", source_id, name, msg)
        _record_status(source_id, "error", duration_ms, str(msg)[:1000], 0)
        status.update(status="error", error=str(msg))
        return status

    matched_rows: list[dict] = []

    for entry in parsed.entries:
        title = entry.get("title", "") or ""
        body = _entry_body(entry)
        # Truncate the body to bound memory on pathologically large payloads.
        if len(body) > config.body_truncate_bytes:
            body = body[: config.body_truncate_bytes]

        # Keep the entry if any keyword matches (title first, then body).
        if not any(entry_matches(term, mode, title, body) for _kid, term, mode in keywords):
            continue

        published = (
            _struct_to_datetime(entry.get("published_parsed"))
            or _struct_to_datetime(entry.get("updated_parsed"))
        )
        matched_rows.append(
            {
                "source_id": source_id,
                "dedup_id": _entry_dedup_id(entry),
                "title": title[:1024],
                "link": (entry.get("link", "") or "")[:2048],
                "body": body,
                "author": (entry.get("author") or None),
                "source_name": name,
                "published_at": published.replace(tzinfo=None) if published else None,
                "fetched_at": _utcnow().replace(tzinfo=None),
            }
        )

    inserted = _store_matches(matched_rows)

    # Persist conditional-request headers for next time.
    etag = resp.headers.get("ETag")
    last_modified = resp.headers.get("Last-Modified")
    duration_ms = int((time.monotonic() - started) * 1000)
    _record_status(source_id, "ok", duration_ms, None, inserted, etag=etag, last_modified=last_modified)

    log.info(
        "Source %d (%s): %d entries, %d new matches (%dms).",
        source_id, name, len(parsed.entries), inserted, duration_ms,
    )
    status["matches"] = inserted
    return status


def _fetch_loaded(source_id: int, config: AppConfig) -> requests.Response:
    """Reload source then fetch (kept separate so _fetch works on a live row)."""
    with get_session() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise RuntimeError("source disappeared")
        # Detach a lightweight copy of the fields _fetch needs.
        session.expunge(source)
    return _fetch(source, config)


def _store_matches(rows: list[dict]) -> int:
    """Insert matches with INSERT IGNORE semantics; return count actually added."""
    if not rows:
        return 0
    inserted = 0
    with get_session() as session:
        with session.begin():
            for row in rows:
                stmt = mysql_insert(Match).values(**row).prefix_with("IGNORE")
                result = session.execute(stmt)
                # rowcount is 1 for an inserted row, 0 when ignored as duplicate.
                if result.rowcount and result.rowcount > 0:
                    inserted += 1
    return inserted


def _record_status(
    source_id: int,
    status: str,
    duration_ms: int,
    error: str | None,
    match_count: int,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    with get_session() as session:
        with session.begin():
            source = session.get(Source, source_id)
            if source is None:
                return
            source.last_poll_at = _utcnow().replace(tzinfo=None)
            source.last_duration_ms = duration_ms
            source.last_status = status
            source.last_error = error
            source.last_match_count = match_count
            if etag is not None:
                source.etag = etag[:512]
            if last_modified is not None:
                source.last_modified = last_modified[:255]


def prune_matches(config: AppConfig) -> int:
    """Apply retention policy. Returns number of rows pruned."""
    pruned = 0
    with get_session() as session:
        with session.begin():
            # Age-based pruning.
            if config.retention_max_age_days and config.retention_max_age_days > 0:
                cutoff = (_utcnow() - timedelta(days=config.retention_max_age_days)).replace(tzinfo=None)
                res = session.execute(
                    delete(Match).where(Match.fetched_at < cutoff)
                )
                pruned += res.rowcount or 0

            # Count-based pruning: keep newest N.
            if config.retention_max_matches and config.retention_max_matches > 0:
                total = session.scalar(select(func.count()).select_from(Match)) or 0
                excess = total - config.retention_max_matches
                if excess > 0:
                    # Find the id cutoff: ids of the oldest `excess` rows.
                    old_ids = session.scalars(
                        select(Match.id)
                        .order_by(Match.fetched_at.asc(), Match.id.asc())
                        .limit(excess)
                    ).all()
                    if old_ids:
                        res = session.execute(delete(Match).where(Match.id.in_(old_ids)))
                        pruned += res.rowcount or 0
    if pruned:
        log.info("Retention: pruned %d old matches.", pruned)
    return pruned


def run_poll_cycle(config: AppConfig) -> dict:
    """Poll all enabled sources concurrently, then prune. Never raises."""
    cycle_start = time.monotonic()
    log.info("Poll cycle starting.")

    # Load enabled sources and keywords once per cycle.
    with get_session() as session:
        source_ids = list(
            session.scalars(select(Source.id).where(Source.enabled.is_(True))).all()
        )
        keywords: list[tuple[int, str, str]] = [
            (kid, term, mode)
            for kid, term, mode in session.execute(
                select(Keyword.id, Keyword.term, Keyword.mode).where(Keyword.enabled.is_(True))
            ).all()
        ]

    if not source_ids:
        log.info("Poll cycle: no enabled sources.")
        return {"sources": 0, "matches": 0}
    if not keywords:
        log.info("Poll cycle: no enabled keywords; nothing will match this cycle.")

    total_matches = 0
    results = []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=config.poll_max_workers, thread_name_prefix="poll") as ex:
        futures = {ex.submit(poll_source, sid, config, keywords): sid for sid in source_ids}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 - defensive; poll_source shouldn't raise
                log.exception("Unexpected error polling source %d: %s", sid, exc)
                res = {"source_id": sid, "status": "error", "matches": 0, "error": str(exc)}
            results.append(res)
            total_matches += res.get("matches", 0)

    try:
        prune_matches(config)
    except Exception as exc:  # noqa: BLE001
        log.exception("Retention pruning failed: %s", exc)

    duration = time.monotonic() - cycle_start
    ok = sum(1 for r in results if r["status"] in ("ok", "not_modified"))
    log.info(
        "Poll cycle complete in %.2fs: %d/%d sources ok, %d new matches.",
        duration, ok, len(results), total_matches,
    )
    return {"sources": len(results), "matches": total_matches, "duration_s": duration}
