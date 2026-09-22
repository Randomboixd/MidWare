"""HTTP Basic authentication for the control panel.

The proxy surface authenticates clients with their MidWare keys; this module
guards the human-facing pages instead. Credentials normally live in the ``meta``
table as a salted scrypt hash, set from the setup page. When
``MIDWARE_ADMIN_PASSWORD`` is set it takes precedence over the database, so a
headless deployment can be provisioned from the environment and a forgotten
password can be recovered without touching SQLite.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from functools import wraps

from flask import Flask, Response, current_app, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .auth import get_db
from .db import to_iso, utcnow

REALM = "MidWare"
DEFAULT_USERNAME = "admin"
CHALLENGE = f'Basic realm="{REALM}", charset="UTF-8"'


def enabled() -> bool:
    return bool(current_app.config.get("ADMIN_AUTH_ENABLED", True))


def env_override_active() -> bool:
    return bool(current_app.config.get("ADMIN_PASSWORD"))


def credentials_configured() -> bool:
    if env_override_active():
        return True
    return bool(get_db().get_meta("admin_password_hash"))


def current_username() -> str:
    if env_override_active():
        return current_app.config.get("ADMIN_USERNAME") or DEFAULT_USERNAME
    return get_db().get_meta("admin_username") or DEFAULT_USERNAME


def credentials_set_at() -> str | None:
    return get_db().get_meta("admin_credentials_set_at")


def set_credentials(username: str, password: str) -> None:
    """Store a salted hash; the plaintext never touches the database."""
    db = get_db()
    db.set_meta("admin_username", username)
    db.set_meta("admin_password_hash", generate_password_hash(password))
    db.set_meta("admin_credentials_set_at", to_iso(utcnow()))
    db.clear_setup_code()


def ensure_setup_code(app: Flask) -> str | None:
    """Guarantee a claim code exists while no admin account is configured.

    Printing it at startup means the person who controls the host (and its logs)
    is the only one who can finish setup, not whoever reaches the setup page first.
    """
    if not app.config.get("ADMIN_AUTH_ENABLED", True):
        return None
    db = app.extensions["midware_db"]
    if app.config.get("ADMIN_PASSWORD") or db.get_meta("admin_password_hash"):
        return None
    code = db.get_meta("admin_setup_code") or db.issue_setup_code()
    if not app.config.get("TESTING"):
        print(
            "\n!!! MIDWARE ACCOUNT CREATION !!!\n"
            "Someone has requested to create a admin account on this midware instance!\n"
            f"Put this code: {code}\n"
            "to claim this instance!\n",
            flush=True,
        )
    return code


def verify_setup_code(candidate: str) -> bool:
    stored = get_db().get_meta("admin_setup_code")
    if not stored or not candidate:
        return False
    return hmac.compare_digest(stored.encode("utf-8"), candidate.encode("utf-8"))


def _equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def verify(username: str, password: str) -> bool:
    if not credentials_configured():
        return False
    if env_override_active():
        expected_user = current_app.config.get("ADMIN_USERNAME") or DEFAULT_USERNAME
        expected_pass = current_app.config.get("ADMIN_PASSWORD") or ""
        return _equal(username, expected_user) and _equal(password, expected_pass)
    stored_user = get_db().get_meta("admin_username") or DEFAULT_USERNAME
    stored_hash = get_db().get_meta("admin_password_hash") or ""
    return _equal(username, stored_user) and check_password_hash(stored_hash, password)


def _basic_credentials() -> tuple[str, str] | None:
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return None
    try:
        raw = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    username, sep, password = raw.partition(":")
    if not sep:
        return None
    return username, password


def is_authenticated() -> bool:
    parsed = _basic_credentials()
    if parsed is None:
        return False
    return verify(*parsed)


def messages_locked() -> bool:
    """Whether the request viewer must hide message bodies and raw payloads."""
    if not enabled():
        return False
    if not credentials_configured():
        return True
    return not is_authenticated()


def challenge(message: str | None = None) -> Response:
    """A 401 that makes the browser show its native login prompt."""
    response = Response(
        render_template("auth_required.html", message=message),
        status=401,
        mimetype="text/html",
    )
    response.headers["WWW-Authenticate"] = CHALLENGE
    return response


def guard_admin() -> Response | None:
    """``before_request`` hook for the admin blueprint.

    With no credentials yet configured every page but setup bounces to setup;
    once they exist, every admin page requires a valid login.
    """
    if not enabled():
        return None
    if not credentials_configured():
        if request.endpoint == "admin.setup":
            return None
        return redirect(url_for("admin.setup"))
    if is_authenticated():
        return None
    return challenge()


def admin_required(view):
    """Guard a standalone route (used by the dashboard message endpoint)."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not enabled():
            return view(*args, **kwargs)
        if not credentials_configured():
            return redirect(url_for("admin.setup"))
        if is_authenticated():
            return view(*args, **kwargs)
        return challenge()

    return wrapper
