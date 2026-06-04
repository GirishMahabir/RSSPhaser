"""Single-admin session authentication.

Credentials come from env: ``ADMIN_USER`` and ``ADMIN_PASSWORD_HASH`` (bcrypt).
We never store a plaintext password. Login compares the submitted password
against the bcrypt hash; on success we mark the session authenticated.
"""
from __future__ import annotations

import functools
import logging

import bcrypt
from flask import flash, redirect, request, session, url_for

log = logging.getLogger("rssphaser.auth")

SESSION_KEY = "authenticated"


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time bcrypt verification. Returns False on any error."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        log.warning("Password verification error: %s", exc)
        return False


def check_login(username: str, password: str, admin_user: str, admin_hash: str) -> bool:
    # Compare username plainly but still run bcrypt to reduce timing signal.
    user_ok = (username or "") == admin_user
    pw_ok = verify_password(password or "", admin_hash)
    return user_ok and pw_ok


def login_required(view):
    """Decorator: redirect to the login page if not authenticated."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get(SESSION_KEY):
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped
