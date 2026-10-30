"""Cookie-session login for the human-facing pages.

The proxy surface authenticates clients with their MidWare keys; this module
guards the human-facing pages instead. Credentials normally live in the ``meta``
table as a salted scrypt hash, set from the setup page. When
``MIDWARE_ADMIN_PASSWORD`` is set it takes precedence over the database, so a
headless deployment can be provisioned from the environment and a forgotten
password can be recovered without touching SQLite.

A login is a Flask session cookie marked permanent, so it survives closing the
browser and is refreshed on each visit. The session records a fingerprint of the
current credentials keyed by the app secret; changing the password invalidates
every session without needing a session table.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from functools import wraps
from urllib.parse import urlsplit, urlunsplit

from flask import (
    Flask,
    Response,
    current_app,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .auth import get_db
from .db import to_iso, utcnow

DEFAULT_USERNAME = "admin"

_SESSION_USER = "admin_user"
_SESSION_FINGERPRINT = "admin_fingerprint"

_FAILURES: dict[str, list[float]] = {}
_FAILURES_LOCK = threading.Lock()


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


def ensure_secret_key(app: Flask) -> None:
    """Guarantee a cookie-signing key without shipping a known default.

    An explicit ``MIDWARE_SECRET_KEY`` wins. Otherwise the key is generated once
    and kept in the database, so browser logins survive a restart.
    """
    if app.config.get("SECRET_KEY"):
        return
    app.config["SECRET_KEY"] = app.extensions["midware_db"].issue_session_secret()


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


def _fingerprint(username: str) -> str:
    """Bind a session to the current credentials.

    Keyed by the app secret and built from a hash (or the env password), so the
    cookie never carries a password derivative that is useful on its own.
    """
    if env_override_active():
        material = f"{username}\0{current_app.config.get('ADMIN_PASSWORD') or ''}"
    else:
        material = f"{username}\0{get_db().get_meta('admin_password_hash') or ''}"
    key = current_app.secret_key
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key or b"", material.encode("utf-8"), hashlib.sha256).hexdigest()


def login(username: str) -> None:
    session.clear()
    session.permanent = True
    session[_SESSION_USER] = username
    session[_SESSION_FINGERPRINT] = _fingerprint(username)


def logout() -> None:
    session.clear()


def is_authenticated() -> bool:
    if not enabled() or not credentials_configured():
        return False
    username = session.get(_SESSION_USER)
    fingerprint = session.get(_SESSION_FINGERPRINT)
    if not isinstance(username, str) or not isinstance(fingerprint, str):
        return False
    if not _equal(username, current_username()):
        return False
    return hmac.compare_digest(fingerprint, _fingerprint(username))


def messages_locked() -> bool:
    """Whether the request viewer must hide message bodies and raw payloads."""
    if not enabled():
        return False
    if not credentials_configured():
        return True
    return not is_authenticated()


def safe_next(candidate: str | None) -> str:
    """Only allow a relative path back into this site after signing in."""
    if not candidate or "\\" in candidate:
        return "/"
    parts = urlsplit(candidate)
    if (
        parts.scheme
        or parts.netloc
        or not parts.path.startswith("/")
        or parts.path.startswith("//")
    ):
        return "/"
    return urlunsplit(("", "", parts.path, parts.query, ""))


def signin_url(next_target: str | None = None) -> str:
    if next_target is None:
        return url_for("account.signin")
    return url_for("account.signin", next=next_target)


def _wants_json() -> bool:
    return (
        request.is_json
        or request.path.endswith(".json")
        or request.accept_mimetypes.best == "application/json"
    )


def _next_target() -> str:
    """Where to return after signing in; POST data cannot be replayed, so use its path."""
    if request.method in {"GET", "HEAD"}:
        target = request.full_path.rstrip("?")
    else:
        target = request.path
    return target if target.startswith("/") else "/"


def _auth_response() -> Response | tuple[Response, int]:
    if _wants_json():
        return (
            jsonify(
                {
                    "error": {
                        "message": "Authentication required.",
                        "type": "midware_auth_error",
                        "code": "authentication_required",
                    }
                }
            ),
            401,
        )
    return redirect(signin_url(_next_target()))


def _throttle_key(username: str) -> str:
    return f"{request.remote_addr or ''}\0{username.strip().lower()}"


def _throttle_limits() -> tuple[int, float]:
    attempts = int(current_app.config.get("ADMIN_LOGIN_MAX_ATTEMPTS", 5) or 0)
    window = float(current_app.config.get("ADMIN_LOGIN_WINDOW_SECONDS", 300) or 0)
    return attempts, window


def login_blocked(username: str) -> bool:
    attempts, window = _throttle_limits()
    if attempts <= 0 or window <= 0:
        return False
    now = time.monotonic()
    key = _throttle_key(username)
    with _FAILURES_LOCK:
        recent = [stamp for stamp in _FAILURES.get(key, []) if now - stamp < window]
        if recent:
            _FAILURES[key] = recent
        else:
            _FAILURES.pop(key, None)
        return len(recent) >= attempts


def record_login_failure(username: str) -> None:
    attempts, window = _throttle_limits()
    if attempts <= 0 or window <= 0:
        return
    now = time.monotonic()
    key = _throttle_key(username)
    with _FAILURES_LOCK:
        recent = [stamp for stamp in _FAILURES.get(key, []) if now - stamp < window]
        recent.append(now)
        _FAILURES[key] = recent[-attempts:]


def clear_login_failures(username: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.pop(_throttle_key(username), None)


def reset_login_throttle() -> None:
    """Test helper: forget every tracked failure."""
    with _FAILURES_LOCK:
        _FAILURES.clear()


def guard_admin() -> Response | tuple[Response, int] | None:
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
    return _auth_response()


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
        return _auth_response()

    return wrapper


def register_template_context(app: Flask) -> None:
    @app.context_processor
    def _auth_context():
        return {
            "admin_signed_in": is_authenticated(),
            "admin_auth_enabled": enabled(),
        }
