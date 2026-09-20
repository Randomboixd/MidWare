"""Write surfaces: first-run setup, routes, API keys, control panel."""

from __future__ import annotations

import json

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

from ..auth import get_db
from ..models import probe_route, refresh_all
from ..premodels import (
    DESCRIPTION_MAX,
    MERGE_MODES,
    PROMPT_MODES,
    SAMPLING_KEYS,
    normalize_preset,
    normalize_slug,
    parse_params,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _form_int(name: str, default: int, minimum: int = 0, maximum: int = 100_000) -> int:
    raw = request.form.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _form_bool(name: str, default: bool = False) -> bool:
    raw = request.form.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _flash_refresh(result: dict) -> None:
    if result["errors"]:
        detail = "; ".join(f"{e['route_name']}: {e['error']}" for e in result["errors"])
        flash(f"Model fetch failed — {detail}", "error")
    else:
        flash(f"Fetched {result['total']} model slug(s) across {len(result['routes'])} route(s).", "success")


def _connection_test(name: str, target_host: str, upstream_key: str) -> bool:
    """Probe a prospective route; flash the outcome and report whether to keep it."""
    client = current_app.extensions["midware_http"]
    try:
        result = probe_route(client, name, target_host, upstream_key)
    except Exception as exc:  # a probe must never 500 the admin page
        flash(f"Connection test errored: {type(exc).__name__}: {exc}", "error")
        return False
    if not result["ok"]:
        flash(f"Connection test failed for '{name}' — {result['error']}", "error")
        return False
    if result.get("warning"):
        flash(
            f"Connected to '{name}', but {result['url']} did not return a model list.",
            "warning",
        )
    else:
        flash(f"Connection test passed for '{name}': {len(result['models'])} model(s) found.", "success")
    return True


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

        test = _form_bool("test_connection", True)
        if test and not _connection_test(name, target_host, upstream_key):
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
            elif _form_bool("test_connection", True) and not _connection_test(name, target_host, upstream_key):
                pass
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
                current = db.get_route(route_id)
                probe_key = upstream_key or (current["upstream_key"] if current else "")
                if _form_bool("test_connection", False) and not _connection_test(name, target_host, probe_key):
                    pass
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
        if "cors_allow_origin" in request.form:
            db.set_setting("cors_allow_origin", (request.form.get("cors_allow_origin") or "").strip())
        flash(f"Settings saved. Request log limit set to {log_limit}.", "success")
        return redirect(url_for("admin.settings"))

    return render_template(
        "settings.html",
        log_limit=db.request_log_limit(),
        cors_allow_origin=db.cors_allow_origin(current_app.config["CORS_ALLOW_ORIGIN"]),
        configured=db.is_configured(),
        configured_at=db.get_meta("configured_at"),
        route_count=len(db.list_routes()),
        key_count=db.count_api_keys(),
        request_count=db.count_requests(),
        max_capture=current_app.config["MAX_CAPTURE_BYTES"],
        upstream_timeout=current_app.config["UPSTREAM_TIMEOUT"],
        verify_tls=current_app.config["UPSTREAM_VERIFY_TLS"],
    )


# -- premodels ---------------------------------------------------------------


def _read_premodel_form(db, existing):
    form = request.form
    name = (form.get("name") or "").strip()
    description = (form.get("description") or "").strip()[:DESCRIPTION_MAX]
    slug = normalize_slug(form.get("slug") or "", name)
    route_id = form.get("route_id", type=int)
    model = (form.get("model") or "").strip()
    merge_mode = form.get("merge_mode") or "append"
    if merge_mode not in MERGE_MODES:
        merge_mode = "append"
    accept_mwvars = _form_bool("accept_mwvars")
    prompt_mode = form.get("prompt_mode") or "simple"
    if prompt_mode not in PROMPT_MODES:
        prompt_mode = "simple"
    system_prompt = form.get("system_prompt") or ""
    params_enabled = _form_bool("params_enabled")
    params: dict[str, float | int] = {}
    param_errors: list[str] = []
    for key in SAMPLING_KEYS:
        raw_value = (form.get(key) or "").strip()
        if not raw_value:
            continue
        try:
            number = float(raw_value)
        except ValueError:
            param_errors.append(f"'{key}' must be a number.")
            continue
        params[key] = int(number) if key == "max_tokens" and number.is_integer() else number
    params_json = json.dumps(params, ensure_ascii=False) if params else None

    preset_json = existing["preset_json"] if existing is not None else None
    preset_raw = (form.get("preset_json") or "").strip()
    if prompt_mode == "simple":
        preset_json = None
    elif preset_raw:
        preset_json = None
        try:
            normalized = normalize_preset(json.loads(preset_raw))
        except (ValueError, TypeError):
            normalized = None
        if normalized and normalized["prompts"]:
            preset_json = json.dumps(normalized, ensure_ascii=False)

    fields = {
        "name": name,
        "slug": slug,
        "description": description,
        "route_id": route_id,
        "model": model,
        "merge_mode": merge_mode,
        "accept_mwvars": accept_mwvars,
        "prompt_mode": prompt_mode,
        "system_prompt": system_prompt,
        "preset_json": preset_json,
        "params_enabled": params_enabled,
        "params_json": params_json,
    }

    errors: list[str] = list(param_errors)
    if not name:
        errors.append("Name is required.")
    if not slug:
        errors.append("Slug is required.")
    if route_id is None or db.get_route(route_id) is None:
        errors.append("Choose a connection.")
    if not model:
        errors.append("Choose or type a model.")
    if prompt_mode == "sillytavern" and not preset_json:
        errors.append("Upload a SillyTavern preset with at least one prompt.")
    clash = db.get_premodel_by_slug(slug) if slug else None
    if clash is not None and (existing is None or clash["id"] != existing["id"]):
        errors.append(f"Slug '{slug}' is already in use.")
    return fields, errors


def _render_premodel_form(db, form, premodel):
    preset_data = {"prompts": []}
    raw = form.get("preset_json") if isinstance(form, dict) else None
    if raw:
        try:
            preset_data = json.loads(raw)
        except (ValueError, TypeError):
            preset_data = {"prompts": []}
    params = parse_params(form.get("params_json")) if isinstance(form, dict) else {}
    return render_template(
        "premodel_form.html",
        premodel=premodel,
        routes=db.list_routes(),
        models=db.list_models(),
        form=form,
        preset_data=preset_data,
        params=params,
        sampling_keys=SAMPLING_KEYS,
    )


def _save_premodel(db, existing):
    fields, errors = _read_premodel_form(db, existing)
    if errors:
        for message in errors:
            flash(message, "error")
        return _render_premodel_form(db, fields, existing)
    if existing is None:
        db.create_premodel(**fields)
        flash(f"Premodel '{fields['name']}' created.", "success")
    else:
        db.update_premodel(existing["id"], **fields)
        flash(f"Premodel '{fields['name']}' updated.", "success")
    return redirect(url_for("admin.premodels"))


@bp.route("/premodels", methods=["GET", "POST"])
def premodels():
    db = get_db()
    if request.method == "POST":
        if request.form.get("action") == "delete":
            db.delete_premodel(request.form.get("premodel_id", type=int))
            flash("Premodel deleted.", "success")
        return redirect(url_for("admin.premodels"))

    routes = db.list_routes()
    return render_template(
        "premodels.html",
        premodels=db.list_premodels(),
        route_names={route["id"]: route["name"] for route in routes},
    )


@bp.route("/premodels/new", methods=["GET", "POST"])
def premodel_new():
    db = get_db()
    if request.method == "POST":
        return _save_premodel(db, None)
    return _render_premodel_form(db, {}, None)


@bp.route("/premodels/<int:premodel_id>/edit", methods=["GET", "POST"])
def premodel_edit(premodel_id: int):
    db = get_db()
    existing = db.get_premodel(premodel_id)
    if existing is None:
        abort(404)
    if request.method == "POST":
        return _save_premodel(db, existing)
    return _render_premodel_form(db, dict(existing), existing)
