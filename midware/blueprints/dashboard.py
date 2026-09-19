"""Read-only UI: dashboard, heatmap, request log, request detail."""

from __future__ import annotations

import json

from flask import Blueprint, Response, abort, redirect, render_template, request, url_for

from ..auth import get_db
from ..conversation import build_conversation

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
    conversation = build_conversation(row["request_body"], row["response_body"])
    return render_template("request_detail.html", row=row, conversation=conversation)


@bp.get("/requests/<int:request_id>/messages.json")
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
