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
    get_poll_interval,
    get_session,
    init_engine,
    set_poll_interval,
)
from .feed import build_feed_xml
from .logging_config import configure_logging
from .models import Keyword, Match, Source

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
            problematic = list(
                s.scalars(select(Keyword).where(Keyword.problematic.is_(True))).all()
            )
            keyword_count = s.scalar(select(func.count()).select_from(Keyword)) or 0
        return render_template(
            "dashboard.html",
            sources=sources,
            total_matches=total_matches,
            problematic=problematic,
            keyword_count=keyword_count,
            poll_interval=get_poll_interval(config),
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
                    src = s.get(Source, source_id)
                    if src is None:
                        flash("Source not found.", "error")
                    else:
                        src.name = name[:255]
                        src.url = url[:1024]
                        src.enabled = enabled
                        flash("Source updated.", "success")
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
            rows = list(s.scalars(select(Keyword).order_by(Keyword.pattern)).all())
        return render_template("keywords.html", keywords=rows)

    @app.route("/keywords/add", methods=["POST"])
    @login_required
    def add_keyword():
        pattern = (request.form.get("pattern") or "").strip()
        enabled = request.form.get("enabled") == "on"
        err = _validate_regex(pattern)
        if err:
            flash(err, "error")
        else:
            with get_session() as s:
                with s.begin():
                    s.add(Keyword(pattern=pattern[:512], enabled=enabled))
            flash("Keyword added.", "success")
        return redirect(url_for("keywords"))

    @app.route("/keywords/<int:keyword_id>/edit", methods=["POST"])
    @login_required
    def edit_keyword(keyword_id: int):
        pattern = (request.form.get("pattern") or "").strip()
        enabled = request.form.get("enabled") == "on"
        err = _validate_regex(pattern)
        if err:
            flash(err, "error")
        else:
            with get_session() as s:
                with s.begin():
                    kw = s.get(Keyword, keyword_id)
                    if kw is None:
                        flash("Keyword not found.", "error")
                    else:
                        kw.pattern = pattern[:512]
                        kw.enabled = enabled
                        # Editing clears the problematic flag so it can be re-evaluated.
                        kw.problematic = False
                        kw.problem_note = None
                        flash("Keyword updated.", "success")
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


def _validate_regex(pattern: str) -> str | None:
    """Return an error message if the pattern is empty or doesn't compile."""
    if not pattern:
        return "Pattern is required."
    if len(pattern) > 512:
        return "Pattern is too long (max 512 characters)."
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"Invalid regular expression: {exc}"
    return None
