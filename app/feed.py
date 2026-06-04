"""Build the public RSS 2.0 output feed from stored matches (newest first)."""
from __future__ import annotations

import logging
from datetime import timezone

from feedgen.feed import FeedGenerator
from sqlalchemy import select

from .config import AppConfig
from .db import get_session
from .models import Match

log = logging.getLogger("rssphaser.feed")


def build_feed_xml(config: AppConfig, base_url: str) -> bytes:
    """Return RSS 2.0 XML bytes built from the newest stored matches."""
    fg = FeedGenerator()
    fg.title("RSSPhaser — Filtered Security Feed")
    fg.link(href=base_url.rstrip("/") + "/feed.xml", rel="self")
    fg.link(href=base_url.rstrip("/"), rel="alternate")
    fg.description("Keyword-filtered aggregation of security RSS/Atom feeds.")
    fg.language("en")
    fg.generator("RSSPhaser")

    with get_session() as session:
        # MariaDB sorts NULLs last under DESC, so undated entries naturally fall
        # to the bottom — no NULLS LAST clause (which MariaDB doesn't support).
        matches = list(
            session.scalars(
                select(Match)
                .order_by(Match.published_at.desc(), Match.fetched_at.desc())
                .limit(config.output_item_cap)
            ).all()
        )

    # feedgen prepends entries, so add oldest-first to keep newest at the top.
    for m in reversed(matches):
        fe = fg.add_entry()
        # A stable, unique guid per item.
        fe.id(m.dedup_id)
        fe.guid(m.dedup_id, permalink=False)
        fe.title(m.title or "(untitled)")
        if m.link:
            fe.link(href=m.link)
        if m.body:
            fe.description(m.body)
        if m.author:
            fe.author(name=m.author)
        # Use published date if present, else fetched time. feedgen needs tz-aware.
        when = m.published_at or m.fetched_at
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            fe.pubDate(when)
        if m.source_name:
            fe.category(term=m.source_name)

    return fg.rss_str(pretty=True)
