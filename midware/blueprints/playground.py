"""The Playground: a browser chat surface that calls this instance's own ``/v1``.

Dogfooding is the design. Every generate the page fires is a real MidWare request
with a real key, forwarded by the normal proxy, logged like any other — so the
snippets the page hands out are provably the ones it just ran. Nothing here
proxies model traffic itself; the browser talks to ``/v1`` directly.

The key it runs on is temporary. One row, reused by name, and its expiry slides
forward every time the page is opened, so it stops working on its own instead of
becoming a permanent credential sitting inside a web page.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, jsonify, render_template, request, url_for

from .. import adminauth
from ..auth import get_db
from ..db import utcnow

bp = Blueprint("playground", __name__)

KEY_NAME = "Midware Playground Temporary API Key"
KEY_DESCRIPTION = (
    "Managed by the Playground page and renewed every time it opens. "
    "Delete it here to cut the playground off; the next visit mints a fresh one."
)
KEY_TTL_SECONDS = 2 * 60 * 60
KEY_MAX_REQUESTS_PER_HOUR = 120


@bp.before_request
def _require_login():
    return adminauth.guard_admin()


def _api_base() -> str:
    """The base URL a client should point at, derived from this very request.

    Built server-side (not in JS from ``window.location``) so a reverse proxy or
    a path prefix produces the URL that actually works, and so the docs on the
    page cannot disagree with what the page itself calls.
    """
    return request.url_root.rstrip("/") + "/v1"


def _ensure_key(db):
    """Reuse the playground's temporary key, or mint it, and slide its expiry.

    Returns ``(row, token)``. Reuse keeps one row forever: a new row per visit
    would scatter the request log across hundreds of dead keys.
    """
    expires_at = utcnow() + timedelta(seconds=KEY_TTL_SECONDS)
    limit = dict(
        limit_requests_enabled=True,
        limit_requests_hour=True,
        max_requests_hour=KEY_MAX_REQUESTS_PER_HOUR,
    )
    row = db.find_api_key_by_name(KEY_NAME)
    if row is None:
        db.create_api_key(
            KEY_NAME, description=KEY_DESCRIPTION, expires_at=expires_at, **limit
        )
        row = db.find_api_key_by_name(KEY_NAME)
    if row is None:  # pragma: no cover - the row was found or just written
        raise RuntimeError("playground key disappeared right after creation")

    db.update_api_key(row["id"], **limit)
    db.set_api_key_expiry(row["id"], expires_at)
    if not row["enabled"]:
        # Opening the Playground *is* the request to use it, so a playground key
        # disabled from the Keys page comes back on. Deleting the row is the way
        # to get a new token; the page says so.
        db.set_api_key_enabled(row["id"], True)
    refreshed = db.get_api_key(row["id"]) or row
    return refreshed, row["token"]


@bp.get("/admin/playground")
def index():
    return render_template("playground.html", api_base=_api_base())


@bp.post("/admin/playground/key")
def key():
    """Hand the browser its temporary key (page-scoped, admin-session gated)."""
    row, token = _ensure_key(get_db())
    return jsonify(
        {
            "token": token,
            "key_id": int(row["id"]),
            "expires_at": row["expires_at"],
            "ttl_minutes": KEY_TTL_SECONDS // 60,
            "hour_limit": KEY_MAX_REQUESTS_PER_HOUR,
            "requests_url": url_for("dashboard.requests_page", key=int(row["id"])),
            "keys_url": url_for("admin.keys"),
        }
    )
