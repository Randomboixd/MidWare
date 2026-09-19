"""Write surfaces: first-run setup, routes, API keys, control panel."""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from ..auth import get_db
from ..models import refresh_all

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _form_int(name: str, default: int, minimum: int = 0, maximum: int = 100_000) -> int:
    raw = request.form.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _flash_refresh(result: dict) -> None:
    if result["errors"]:
        detail = "; ".join(f"{e['route_name']}: {e['error']}" for e in result["errors"])
        flash(f"Model fetch failed — {detail}", "error")
    else:
        flash(f"Fetched {result['total']} model slug(s) across {len(result['routes'])} route(s).", "success")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    db = get_db()
    routes = db.list_routes()

    if request.method == "POST":
        target_host = (request.form.get("target_host") or "").strip()
        upstream_key = (request.form.get("upstream_key") or "").strip()
        name = (request.form.get("name") or "").strip() or "Default"
        key_name = (request.form.get("key_name") or "").strip() or "Default key"
        log_limit = _form_int("request_log_limit", current_app.config["DEFAULT_REQUEST_LOG_LIMIT"])

        if not target_host.startswith(("http://", "https://")):
            flash("Target host must start with http:// or https://", "error")
            return render_template(
                "setup.html",
                routes=routes,
                defaults=current_app.config,
                form=request.form,
            )

        api_key_id = None
        new_token = None
        if not db.list_api_keys():
            new_token = db.create_api_key(key_name)
            row = db.get_api_key_by_token(new_token)
            api_key_id = row["id"] if row else None

        db.create_route(
            name=name,
            target_host=target_host,
            upstream_key=upstream_key,
            api_key_id=api_key_id,
            is_active=True,
        )
        db.set_setting("request_log_limit", str(log_limit))
        db.mark_configured()
        refresh_all(current_app)

        if new_token:
            flash(
                f"MidWare is ready. Your client key is {new_token} — copy it now, "
                "it is shown again on the Keys page.",
                "success",
            )
        else:
            flash("MidWare is ready. Upstream route saved.", "success")
        return redirect(url_for("dashboard.index"))

    return render_template(
        "setup.html",
        routes=routes,
        defaults=current_app.config,
        form={},
    )


@bp.route("/routes", methods=["GET", "POST"])
def routes():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            name = (request.form.get("name") or "").strip() or "Route"
            target_host = (request.form.get("target_host") or "").strip()
            upstream_key = (request.form.get("upstream_key") or "").strip()
            if not target_host.startswith(("http://", "https://")):
                flash("Target host must start with http:// or https://", "error")
            else:
                existing = db.list_routes()
                db.create_route(
                    name=name,
                    target_host=target_host,
                    upstream_key=upstream_key,
                    is_active=not existing,
                )
                db.mark_configured()
                flash(f"Route '{name}' created.", "success")
                _flash_refresh(refresh_all(current_app))
        elif action == "activate":
            route_id = request.form.get("route_id", type=int)
            for route in db.list_routes():
                db.update_route(route["id"], is_active=route["id"] == route_id)
            _flash_refresh(refresh_all(current_app))
        elif action == "refresh":
            _flash_refresh(refresh_all(current_app))
        elif action == "update":
            route_id = request.form.get("route_id", type=int)
            name = (request.form.get("name") or "").strip()
            target_host = (request.form.get("target_host") or "").strip()
            upstream_key = (request.form.get("upstream_key") or "").strip()
            if not target_host.startswith(("http://", "https://")):
                flash("Target host must start with http:// or https://", "error")
            elif not name:
                flash("Route name cannot be empty.", "error")
            else:
                updates = {"name": name, "target_host": target_host}
                if upstream_key:
                    updates["upstream_key"] = upstream_key
                db.update_route(route_id, **updates)
                flash(f"Route '{name}' updated.", "success")
                _flash_refresh(refresh_all(current_app))
        elif action == "delete":
            route_id = request.form.get("route_id", type=int)
            db.delete_route(route_id)
            flash("Route deleted.", "success")
        return redirect(url_for("admin.routes"))

    return render_template(
        "routes.html",
        routes=db.list_routes(),
        active_route_id=db.active_route()["id"] if db.active_route() else None,
        api_keys=db.list_api_keys(),
        model_summary={entry["route_id"]: entry for entry in db.model_summary()},
        model_slugs=db.list_models(),
        models_refreshed_at=db.get_meta("models_refreshed_at"),
        models_refresh_error=db.get_meta("models_refresh_error"),
    )


@bp.route("/keys", methods=["GET", "POST"])
def keys():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            name = (request.form.get("name") or "").strip() or "key"
            token = db.create_api_key(name)
            flash(f"Created key '{name}': {token}", "success")
        elif action == "delete":
            db.delete_api_key(request.form.get("key_id", type=int))
            flash("Key deleted.", "success")
        return redirect(url_for("admin.keys"))

    return render_template("keys.html", api_keys=db.list_api_keys())


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    db = get_db()
    if request.method == "POST":
        if request.form.get("action") == "purge":
            removed = db.purge_requests()
            flash(f"Purged {removed} logged request(s).", "success")
            return redirect(url_for("admin.settings"))

        log_limit = _form_int("request_log_limit", db.request_log_limit())
        db.set_setting("request_log_limit", str(log_limit))
        db.prune_requests()
        flash(f"Request log limit set to {log_limit}.", "success")
        return redirect(url_for("admin.settings"))

    return render_template(
        "settings.html",
        log_limit=db.request_log_limit(),
        configured=db.is_configured(),
        configured_at=db.get_meta("configured_at"),
        route_count=len(db.list_routes()),
        key_count=db.count_api_keys(),
        request_count=db.count_requests(),
        max_capture=current_app.config["MAX_CAPTURE_BYTES"],
        upstream_timeout=current_app.config["UPSTREAM_TIMEOUT"],
        verify_tls=current_app.config["UPSTREAM_VERIFY_TLS"],
    )
