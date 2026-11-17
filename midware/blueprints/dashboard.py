"""Read-only UI: dashboard, heatmap, request log, request detail."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from flask import (
    Blueprint,
    Response,
    abort,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import __version__
from .. import adminauth
from ..auth import get_db
from ..conversation import build_conversation
from ..db import to_iso
from ..inspector import TRUNCATION_MARKER
from ..inspector import build_page as build_inspector

bp = Blueprint("dashboard", __name__)

PAGE_SIZE = 25


def _filters() -> tuple[int | None, int | None]:
    api_key_id = request.args.get("key", type=int)
    route_id = request.args.get("route", type=int)
    return api_key_id, route_id


@bp.get("/")
def index():
    db = get_db()
    if not db.is_configured() and not db.list_routes():
        return redirect(url_for("admin.setup"))

    api_key_id, route_id = _filters()
    overview = db.overview(api_key_id, route_id)
    heatmap = db.heatmap(weeks=52, api_key_id=api_key_id, route_id=route_id)
    models = db.model_usage(api_key_id, route_id)
    hosts = db.host_usage(api_key_id, route_id)
    recent = db.list_requests(limit=8, api_key_id=api_key_id, route_id=route_id)

    return render_template(
        "dashboard.html",
        overview=overview,
        heatmap=heatmap,
        models=models,
        hosts=hosts,
        recent=recent,
        model_slugs=db.list_models(),
        api_keys=db.list_api_keys(),
        routes=db.list_routes(),
        selected_key=api_key_id,
        selected_route=route_id,
    )


@bp.get("/requests")
def requests_page():
    db = get_db()
    api_key_id, route_id = _filters()
    page = max(request.args.get("page", type=int, default=1), 1)
    total = db.count_requests(api_key_id, route_id)
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(page, pages)

    rows = db.list_requests(
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        api_key_id=api_key_id,
        route_id=route_id,
    )
    return render_template(
        "requests.html",
        requests=rows,
        page=page,
        pages=pages,
        total=total,
        api_keys=db.list_api_keys(),
        routes=db.list_routes(),
        selected_key=api_key_id,
        selected_route=route_id,
        log_limit=db.request_log_limit(),
    )


@bp.get("/requests/<int:request_id>")
def request_detail(request_id: int):
    db = get_db()
    row = db.get_request(request_id)
    if row is None:
        abort(404)

    locked = adminauth.messages_locked()
    # The sign-in button reloads with ``?messages=1``; send an unauthenticated
    # caller to the login page and bring them straight back afterwards.
    if request.args.get("messages") == "1" and locked:
        if adminauth.enabled() and not adminauth.credentials_configured():
            return redirect(url_for("admin.setup"))
        return redirect(
            adminauth.signin_url(
                url_for("dashboard.request_detail", request_id=request_id, messages=1)
            )
        )

    conversation = (
        {"messages": []}
        if locked
        else build_conversation(row["request_body"], row["response_body"])
    )
    route = db.get_route(row["route_id"]) if row["route_id"] else None
    return render_template(
        "request_detail.html",
        row=row,
        conversation=conversation,
        messages_locked=locked,
        page=build_inspector(row, route=route),
    )


@bp.get("/requests/<int:request_id>/messages.json")
@adminauth.admin_required
def request_messages(request_id: int):
    """The normalized conversation, for the client-side ``Fetch`` button."""
    db = get_db()
    row = db.get_request(request_id)
    if row is None:
        abort(404)
    conversation = build_conversation(row["request_body"], row["response_body"])
    payload = json.dumps(conversation, ensure_ascii=False)
    filename = f"midware-request-{request_id}-messages.json"
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


#: Columns copied verbatim into an archive: the row's own record, nothing that
#: is derived (the usage detail and the conversation are rebuilt below).
ARCHIVE_COLUMNS = (
    "id",
    "created_at",
    "request_path",
    "method",
    "model",
    "model_raw",
    "host_prefix",
    "status_code",
    "latency_ms",
    "streamed",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "token_source",
    "upstream_request_id",
    "error",
    "route_id",
    "api_key_id",
)


def _as_dict(row) -> dict[str, Any]:
    """sqlite3.Row to a plain dict; ``abort`` never returns but checkers can't know."""
    return dict(row) if row is not None else {}


@bp.get("/requests/<int:request_id>/archive.json")
@adminauth.admin_required
def request_archive(request_id: int):
    """Everything stored for one request, as one downloadable JSON file.

    Bodies are included whole: the archive is the copy an operator keeps when
    they need to hand a call to someone else or diff it later. What capture
    dropped stays dropped — a truncated body carries its own marker, and
    ``bodies.truncated`` says so without pretending the rest exists.
    """
    db = get_db()
    row = db.get_request(request_id)
    if row is None:
        abort(404)
    record = _as_dict(row)
    route = db.get_route(record["route_id"]) if record["route_id"] else None
    inspector = build_inspector(record, route=route)
    request_body = record["request_body"] or ""
    response_body = record["response_body"] or ""
    payload = {
        "midware": {"version": __version__, "exported_at": to_iso(datetime.now(timezone.utc))},
        "request": {column: record[column] for column in ARCHIVE_COLUMNS if column in record},
        "route": (
            {
                "id": route["id"],
                "name": route["name"],
                "provider": route["provider"],
                "target_host": route["target_host"],
                "is_active": bool(route["is_active"]),
            }
            if route is not None
            else None
        ),
        "api_key": (
            {"id": record["api_key_id"], "name": record["api_key_name"]} if record["api_key_id"] else None
        ),
        # Headers are not captured by this version. The key is stated rather
        # than omitted so a reader knows the difference between "empty" and
        # "never recorded" — and so a future capture can fill it in place.
        "headers": {
            "request": None,
            "response": None,
            "captured": False,
            "note": "This MidWare version does not record headers.",
        },
        "usage": inspector["usage"],
        "cache_split": inspector["cache_split"],
        "request_facts": inspector["request"],
        "response_facts": inspector["response"],
        "conversation": build_conversation(request_body, response_body),
        "bodies": {
            "request": request_body,
            "response": response_body,
            "truncated": {
                "request": TRUNCATION_MARKER in request_body,
                "response": TRUNCATION_MARKER in response_body,
            },
        },
    }
    filename = f"midware-request-{request_id}.json"
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/activity")
def activity():
    db = get_db()
    api_key_id, route_id = _filters()
    heatmap = db.heatmap(weeks=52, api_key_id=api_key_id, route_id=route_id)
    daily = db.daily_usage(days=30, api_key_id=api_key_id, route_id=route_id)
    series = sorted(daily.items(), key=lambda item: item[0], reverse=True)
    return render_template(
        "activity.html",
        heatmap=heatmap,
        series=series,
        api_keys=db.list_api_keys(),
        routes=db.list_routes(),
        selected_key=api_key_id,
        selected_route=route_id,
    )
