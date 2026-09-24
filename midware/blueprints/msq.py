"""MSQ control panel: list recorders, create/edit/delete them, run one now.

Everything here is behind the admin login (the same HTTP Basic challenge used by
the request viewer), because running a probe spends the upstream credential and
the results expose provider behaviour.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import adminauth
from ..auth import get_db
from ..db import dump_json_object, parse_json_object
from ..msq import (
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_USER_PROMPT,
    chart_point,
    evaluate_recorder,
    run_recorder,
)

bp = Blueprint("msq", __name__, url_prefix="/admin/msq")


@bp.before_request
def _require_login():
    return adminauth.guard_admin()


def _form_bool(name: str, default: bool = False) -> bool:
    raw = request.form.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_budget_seconds(raw: str | None, default_ms: int) -> int:
    """Seconds input (``-1`` disables) converted to milliseconds."""
    text = (raw or "").strip()
    if not text:
        return default_ms
    try:
        value = float(text)
    except ValueError:
        return default_ms
    if value < 0:
        return -1
    return int(value * 1000)


def _form_defaults() -> dict:
    config = current_app.config
    return {
        "name": "",
        "route_id": None,
        "model": "",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "user_prompt": DEFAULT_USER_PROMPT,
        "interval_minutes": round(config["MSQ_DEFAULT_INTERVAL"] / 60, 2),
        "enabled": True,
        "penalize_symbols": True,
        "slop_list": "",
        "max_ttft_seconds": int(config["MSQ_DEFAULT_MAX_TTFT_MS"] / 1000),
        "max_total_seconds": int(config["MSQ_DEFAULT_MAX_TOTAL_MS"] / 1000),
        "hc_stream_integrity": False,
        "hc_text_forensics": False,
        "hc_needle": False,
        "hc_collect_needle": False,
        "needle_context_chars": config["MSQ_NEEDLE_CONTEXT_CHARS"],
        "collect_requests": False,
        "ignore_thinking": True,
    }


def _form_from_row(row) -> dict:
    hardcore = parse_json_object(row["hardcore_json"])
    return {
        "name": row["name"],
        "route_id": row["route_id"],
        "model": row["model"],
        "system_prompt": row["system_prompt"],
        "user_prompt": row["user_prompt"],
        "interval_minutes": round((row["interval_seconds"] or 3600) / 60, 2),
        "enabled": bool(row["enabled"]),
        "penalize_symbols": bool(row["penalize_symbols"]),
        "slop_list": row["slop_list"] or "",
        "max_ttft_seconds": _ms_to_seconds(row["max_ttft_ms"]),
        "max_total_seconds": _ms_to_seconds(row["max_total_ms"]),
        "hc_stream_integrity": bool(hardcore.get("stream_integrity")),
        "hc_text_forensics": bool(hardcore.get("text_forensics")),
        "hc_needle": bool(hardcore.get("needle")),
        "hc_collect_needle": bool(hardcore.get("collect_needle")),
        "needle_context_chars": int(
            hardcore.get("needle_context_chars") or current_app.config["MSQ_NEEDLE_CONTEXT_CHARS"]
        ),
        "collect_requests": bool(row["collect_requests"]),
        "ignore_thinking": bool(row["ignore_thinking"]),
    }


def _ms_to_seconds(value: int | None) -> float | int:
    if value is None:
        return 0
    if value < 0:
        return -1
    seconds = value / 1000
    return int(seconds) if seconds.is_integer() else round(seconds, 2)


def _read_form(db) -> tuple[dict, list[str]]:
    form = request.form
    route_id = form.get("route_id", type=int)
    try:
        interval_minutes = float(form.get("interval_minutes") or "60")
    except (TypeError, ValueError):
        interval_minutes = 0.0

    hardcore: dict = {}
    if _form_bool("hc_stream_integrity"):
        hardcore["stream_integrity"] = True
    if _form_bool("hc_text_forensics"):
        hardcore["text_forensics"] = True
    if _form_bool("hc_needle"):
        hardcore["needle"] = True
        try:
            context = int(form.get("needle_context_chars") or "")
        except (TypeError, ValueError):
            context = current_app.config["MSQ_NEEDLE_CONTEXT_CHARS"]
        hardcore["needle_context_chars"] = max(200, min(context, 100_000))
        if _form_bool("hc_collect_needle"):
            hardcore["collect_needle"] = True

    fields = {
        "name": (form.get("name") or "").strip(),
        "route_id": route_id,
        "model": (form.get("model") or "").strip(),
        "system_prompt": form.get("system_prompt") or "",
        "user_prompt": form.get("user_prompt") or "",
        "interval_seconds": int(round(interval_minutes * 60)),
        "enabled": _form_bool("enabled", True),
        "penalize_symbols": _form_bool("penalize_symbols", True),
        "slop_list": (form.get("slop_list") or "").strip(),
        "max_ttft_ms": _parse_budget_seconds(
            form.get("max_ttft_seconds"), current_app.config["MSQ_DEFAULT_MAX_TTFT_MS"]
        ),
        "max_total_ms": _parse_budget_seconds(
            form.get("max_total_seconds"), current_app.config["MSQ_DEFAULT_MAX_TOTAL_MS"]
        ),
        "hardcore_json": dump_json_object(hardcore),
        "collect_requests": _form_bool("collect_requests", False),
        "ignore_thinking": _form_bool("ignore_thinking", True),
    }

    errors: list[str] = []
    if not fields["name"]:
        errors.append("Name is required.")
    if route_id is None or db.get_route(route_id) is None:
        errors.append("Choose a model provider.")
    if not fields["model"]:
        errors.append("Choose a model slug.")
    if interval_minutes < 1:
        errors.append("Check frequency must be at least 1 minute.")
    return fields, errors


def _render_form(db, form, recorder):
    return render_template(
        "msq_form.html",
        recorder=recorder,
        form=form,
        routes=db.list_routes(),
        models=db.list_models(),
    )


def _save(db, existing):
    fields, errors = _read_form(db)
    if errors:
        for message in errors:
            flash(message, "error")
        return _render_form(db, fields, existing)
    if existing is None:
        db.create_msq_recorder(**fields)
        flash(f"MSQ recorder '{fields['name']}' created.", "success")
    else:
        db.update_msq_recorder(existing["id"], **fields)
        flash(f"MSQ recorder '{fields['name']}' updated.", "success")
    return redirect(url_for("msq.index"))


def _dashboard(db) -> list[dict]:
    config = current_app.config
    cards: list[dict] = []
    for recorder in db.list_msq_recorders():
        checks = db.list_msq_checks(recorder["id"], limit=config["MSQ_HISTORY_LIMIT"])
        route = db.get_route(recorder["route_id"]) if recorder["route_id"] else None
        cards.append(
            {
                "recorder": recorder,
                "route_name": route["name"] if route else None,
                "hardcore": parse_json_object(recorder["hardcore_json"]),
                "evaluation": evaluate_recorder(
                    recorder,
                    checks,
                    examine_hours=config["MSQ_EXAMINE_HOURS"],
                    min_checks=config["MSQ_MIN_CHECKS"],
                    degraded_threshold=config["MSQ_DEGRADED_THRESHOLD"],
                ),
                "points": [chart_point(check, index) for index, check in enumerate(checks)],
            }
        )
    return cards


@bp.route("/", methods=["GET"])
def index():
    db = get_db()
    cards = _dashboard(db)
    charts = {str(card["recorder"]["id"]): card["points"] for card in cards}
    return render_template(
        "msq.html",
        cards=cards,
        charts=charts,
        route_count=len(db.list_routes()),
        threshold=current_app.config["MSQ_DEGRADED_THRESHOLD"],
    )


@bp.route("/new", methods=["GET", "POST"])
def recorder_new():
    db = get_db()
    if request.method == "POST":
        return _save(db, None)
    return _render_form(db, _form_defaults(), None)


@bp.route("/<int:recorder_id>/edit", methods=["GET", "POST"])
def recorder_edit(recorder_id: int):
    db = get_db()
    existing = db.get_msq_recorder(recorder_id)
    if existing is None:
        abort(404)
    if request.method == "POST":
        return _save(db, existing)
    return _render_form(db, _form_from_row(existing), existing)


@bp.route("/<int:recorder_id>/delete", methods=["POST"])
def recorder_delete(recorder_id: int):
    db = get_db()
    recorder = db.get_msq_recorder(recorder_id)
    if recorder is None:
        abort(404)
    db.delete_msq_recorder(recorder_id)
    flash(f"MSQ recorder '{recorder['name']}' deleted.", "success")
    return redirect(url_for("msq.index"))


@bp.route("/<int:recorder_id>/run", methods=["POST"])
def recorder_run(recorder_id: int):
    db = get_db()
    recorder = db.get_msq_recorder(recorder_id)
    if recorder is None:
        abort(404)
    result = run_recorder(current_app._get_current_object(), recorder)
    if result["ok"]:
        flash(
            f"'{recorder['name']}' checked: score {result['score']:.0f}/100 "
            f"(TTFT {result['ttft_ms']} ms, total {result['total_ms']} ms).",
            "success",
        )
    else:
        flash(f"'{recorder['name']}' check failed: {result['error']}", "error")
    return redirect(url_for("msq.index"))
