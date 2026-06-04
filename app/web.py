"""Flask application: admin UI, auth, health, and the public output feed.

This module is imported by the gunicorn entrypoint (``wsgi.py``). It does NOT
start the scheduler — that lives in the dedicated scheduler process. The app
factory wires routes, validates config, and ensures the DB schema exists.
"""
from __future__ import annotations

import logging
import re

from flask import (
    Flask,
    Response,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, select, text

from .auth import SESSION_KEY, check_login, login_required
from .config import AppConfig, load_config
from .db import (
    bootstrap,
    get_matches_table_bytes,
    get_poll_interval,
    get_retention,
    get_session,
    init_engine,
    set_poll_interval,
    set_retention,
)
from .feed import build_feed_xml
from .logging_config import configure_logging
from .matching import MATCH_MODES, normalize_mode
from .models import Keyword, Match, Source
from .poller import prune_matches

log = logging.getLogger("rssphaser.web")


def create_app(config: AppConfig | None = None, run_bootstrap: bool = True) -> Flask:
    configure_logging()
    config = config or load_config()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.secret_key
    app.config["APP_CONFIG"] = config
    # Harden the session cookie.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )

    init_engine(config)
    if run_bootstrap:
        # Ensure DB is reachable and schema/seed exist before serving.
        bootstrap(config)

    _register_routes(app, config)
    log.info("Flask app created.")
    return app


def _register_routes(app: Flask, config: AppConfig) -> None:
    # ---------------- Auth ----------------
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get(SESSION_KEY):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if check_login(username, password, config.admin_user, config.admin_password_hash):
                session[SESSION_KEY] = True
                session.permanent = True
                log.info("Admin '%s' logged in.", username)
                nxt = request.args.get("next") or url_for("dashboard")
                # Only allow relative redirects.
                if not nxt.startswith("/"):
                    nxt = url_for("dashboard")
                return redirect(nxt)
            flash("Invalid username or password.", "error")
            log.warning("Failed login attempt for user %r.", username)
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("Logged out.", "success")
        return redirect(url_for("login"))

    # ---------------- Dashboard ----------------
    @app.route("/")
    @login_required
    def dashboard():
        with get_session() as s:
            sources = list(s.scalars(select(Source).order_by(Source.name)).all())
            total_matches = s.scalar(select(func.count()).select_from(Match)) or 0
            keyword_count = s.scalar(select(func.count()).select_from(Keyword)) or 0
            enabled_keywords = s.scalar(
                select(func.count()).select_from(Keyword).where(Keyword.enabled.is_(True))
            ) or 0
            newest = s.scalar(select(func.max(Match.fetched_at)))
        enabled_sources = sum(1 for x in sources if x.enabled)
        max_matches, max_age_days = get_retention(config)
        return render_template(
            "dashboard.html",
            sources=sources,
            total_matches=total_matches,
            keyword_count=keyword_count,
            enabled_keywords=enabled_keywords,
            enabled_sources=enabled_sources,
            newest_match=newest,
            poll_interval=get_poll_interval(config),
            retention_max_matches=max_matches,
            retention_max_age_days=max_age_days,
            store_size=_human_bytes(get_matches_table_bytes(config)),
        )

    # ---------------- Sources CRUD ----------------
    @app.route("/sources", methods=["GET"])
    @login_required
    def sources():
        with get_session() as s:
            rows = list(s.scalars(select(Source).order_by(Source.name)).all())
        return render_template("sources.html", sources=rows)

    @app.route("/sources/add", methods=["POST"])
    @login_required
    def add_source():
        name = (request.form.get("name") or "").strip()
        url = (request.form.get("url") or "").strip()
        enabled = request.form.get("enabled") == "on"
        if not name or not url:
            flash("Name and URL are required.", "error")
        elif not re.match(r"^https?://", url, re.IGNORECASE):
            flash("URL must start with http:// or https://.", "error")
        else:
            with get_session() as s:
                with s.begin():
                    s.add(Source(name=name[:255], url=url[:1024], enabled=enabled))
            flash(f"Source '{name}' added.", "success")
        return redirect(url_for("sources"))

    @app.route("/sources/<int:source_id>/edit", methods=["POST"])
    @login_required
    def edit_source(source_id: int):
        # Note: "enabled" is handled by the dedicated toggle route, not here.
        name = (request.form.get("name") or "").strip()
        url = (request.form.get("url") or "").strip()
        if not name or not url:
            flash("Name and URL are required.", "error")
        elif not re.match(r"^https?://", url, re.IGNORECASE):
            flash("URL must start with http:// or https://.", "error")
        else:
            with get_session() as s:
                with s.begin():
                    src = s.get(Source, source_id)
                    if src is None:
                        flash("Source not found.", "error")
                    else:
                        src.name = name[:255]
                        src.url = url[:1024]
                        flash("Source updated.", "success")
        return redirect(url_for("sources"))

    @app.route("/sources/<int:source_id>/toggle", methods=["POST"])
    @login_required
    def toggle_source(source_id: int):
        # Flip enabled and persist immediately (auto-saved by the UI toggle).
        with get_session() as s:
            with s.begin():
                src = s.get(Source, source_id)
                if src is not None:
                    src.enabled = not src.enabled
        return redirect(url_for("sources"))

    @app.route("/sources/<int:source_id>/delete", methods=["POST"])
    @login_required
    def delete_source(source_id: int):
        with get_session() as s:
            with s.begin():
                src = s.get(Source, source_id)
                if src is not None:
                    s.delete(src)
                    flash("Source deleted.", "success")
        return redirect(url_for("sources"))

    # ---------------- Keywords CRUD ----------------
    @app.route("/keywords", methods=["GET"])
    @login_required
    def keywords():
        with get_session() as s:
            rows = list(s.scalars(select(Keyword).order_by(Keyword.term)).all())
        return render_template("keywords.html", keywords=rows, modes=MATCH_MODES)

    @app.route("/keywords/add", methods=["POST"])
    @login_required
    def add_keyword():
        term = (request.form.get("term") or "").strip()
        mode = request.form.get("mode") or ""
        enabled = request.form.get("enabled") == "on"
        err = _validate_keyword(term, mode)
        if err:
            flash(err, "error")
        else:
            with get_session() as s:
                with s.begin():
                    s.add(Keyword(term=term[:512], mode=normalize_mode(mode), enabled=enabled))
            flash("Keyword added.", "success")
        return redirect(url_for("keywords"))

    @app.route("/keywords/<int:keyword_id>/edit", methods=["POST"])
    @login_required
    def edit_keyword(keyword_id: int):
        # Note: "enabled" is handled by the dedicated toggle route, not here.
        term = (request.form.get("term") or "").strip()
        mode = request.form.get("mode") or ""
        err = _validate_keyword(term, mode)
        if err:
            flash(err, "error")
        else:
            with get_session() as s:
                with s.begin():
                    kw = s.get(Keyword, keyword_id)
                    if kw is None:
                        flash("Keyword not found.", "error")
                    else:
                        kw.term = term[:512]
                        kw.mode = normalize_mode(mode)
                        flash("Keyword updated.", "success")
        return redirect(url_for("keywords"))

    @app.route("/keywords/<int:keyword_id>/toggle", methods=["POST"])
    @login_required
    def toggle_keyword(keyword_id: int):
        # Flip enabled and persist immediately (auto-saved by the UI toggle).
        with get_session() as s:
            with s.begin():
                kw = s.get(Keyword, keyword_id)
                if kw is not None:
                    kw.enabled = not kw.enabled
        return redirect(url_for("keywords"))

    @app.route("/keywords/<int:keyword_id>/delete", methods=["POST"])
    @login_required
    def delete_keyword(keyword_id: int):
        with get_session() as s:
            with s.begin():
                kw = s.get(Keyword, keyword_id)
                if kw is not None:
                    s.delete(kw)
                    flash("Keyword deleted.", "success")
        return redirect(url_for("keywords"))

    # ---------------- Poll interval ----------------
    @app.route("/settings/interval", methods=["POST"])
    @login_required
    def update_interval():
        raw = request.form.get("poll_interval_minutes", "")
        try:
            minutes = int(raw)
        except ValueError:
            flash("Poll interval must be a whole number of minutes.", "error")
            return redirect(url_for("dashboard"))
        if minutes < 1:
            flash("Poll interval must be at least 1 minute.", "error")
            return redirect(url_for("dashboard"))
        set_poll_interval(minutes)
        flash(
            f"Poll interval set to {minutes} minute(s). "
            "The scheduler picks up the change within ~30s (no restart needed).",
            "success",
        )
        return redirect(url_for("dashboard"))

    # ---------------- Retention / cleanup ----------------
    @app.route("/settings/retention", methods=["POST"])
    @login_required
    def update_retention():
        def parse(name: str) -> int | None:
            raw = request.form.get(name, "")
            try:
                v = int(raw)
            except (TypeError, ValueError):
                return None
            return v if v >= 0 else None

        max_matches = parse("retention_max_matches")
        max_age_days = parse("retention_max_age_days")
        if max_matches is None or max_age_days is None:
            flash("Retention limits must be whole numbers ≥ 0 (0 = unlimited).", "error")
            return redirect(url_for("dashboard"))
        set_retention(max_matches, max_age_days)
        flash(
            "Retention limits saved. Cleanup runs automatically after each poll cycle.",
            "success",
        )
        return redirect(url_for("dashboard"))

    @app.route("/maintenance/prune", methods=["POST"])
    @login_required
    def prune_now():
        try:
            pruned = prune_matches(config)
            flash(f"Cleanup complete — removed {pruned} old match(es).", "success")
        except Exception as exc:  # noqa: BLE001
            log.exception("Manual prune failed: %s", exc)
            flash("Cleanup failed; see logs.", "error")
        return redirect(url_for("dashboard"))

    # ---------------- Public output feed ----------------
    @app.route("/feed.xml")
    def feed_xml():
        base_url = request.url_root
        try:
            xml = build_feed_xml(config, base_url)
        except Exception as exc:  # noqa: BLE001 - never 500 the public feed silently
            log.exception("Failed to build feed: %s", exc)
            return Response("Feed temporarily unavailable.", status=503, mimetype="text/plain")
        return Response(xml, mimetype="application/rss+xml")

    # ---------------- Health ----------------
    @app.route("/healthz")
    def healthz():
        try:
            with get_session() as s:
                s.execute(text("SELECT 1"))
            return Response("ok", status=200, mimetype="text/plain")
        except Exception as exc:  # noqa: BLE001
            log.warning("Health check failed: %s", exc)
            return Response(f"db error: {exc}", status=503, mimetype="text/plain")


def _human_bytes(num: int | None) -> str:
    """Render a byte count as a short human-readable string."""
    if not num or num <= 0:
        return "—"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _validate_keyword(term: str, mode: str) -> str | None:
    """Return an error message if the keyword term or mode is invalid."""
    if not term:
        return "Keyword is required."
    if len(term) > 512:
        return "Keyword is too long (max 512 characters)."
    if mode not in MATCH_MODES:
        return f"Match mode must be one of: {', '.join(MATCH_MODES)}."
    return None
