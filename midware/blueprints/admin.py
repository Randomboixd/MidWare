"""Write surfaces: first-run setup, routes, API keys, control panel."""

from __future__ import annotations

import json

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from .. import adminauth, signin
from ..auth import get_db
from ..balance import fetch_balance
from ..db import LIMIT_FLAG_FIELDS, LIMIT_VALUE_FIELDS
from ..models import probe_route, refresh_all
from ..premodels import (
    DESCRIPTION_MAX,
    MERGE_MODES,
    PROMPT_MODES,
    SAMPLING_KEYS,
    normalize_preset,
    normalize_prefix,
    normalize_slug,
    parse_params,
)
from ..workflow import WorkflowError, normalize_workflow

bp = Blueprint("admin", __name__, url_prefix="/admin")

_LIMIT_MAX = 1_000_000_000_000

# The sign-in helpers must answer before an account exists: the bootstrap setup
# page offers them too, and the OpenRouter callback arrives on a redirect from
# another tab. Their tokens are unguessable capabilities, so possession of one
# is the check until the instance is claimed.
_SIGNIN_ENDPOINTS = {
    "admin.signin_start",
    "admin.signin_poll",
    "admin.signin_openrouter_callback",
}


@bp.before_request
def _require_login():
    if request.endpoint in _SIGNIN_ENDPOINTS and not adminauth.credentials_configured():
        return None
    return adminauth.guard_admin()


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


def _form_provider() -> str:
    """The sign-in provider tag, or '' when the form was filled by hand.

    The value only ever arrives from a hidden field that signin.js fills after
    a successful sign-in, but it is still validated against the provider table
    so a forged submit cannot claim a service MidWare does not know.
    """
    raw = (request.form.get("provider") or "").strip().lower()
    return raw if raw in signin.PROVIDERS else ""


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
    auth_enabled = adminauth.enabled()
    credentials_ready = adminauth.credentials_configured()

    # A configured instance has nothing to do here: routes are managed on their
    # own page and the login is changed from Settings.
    if credentials_ready and routes:
        return redirect(url_for("admin.routes"))

    def _render(form):
        return render_template(
            "setup.html",
            routes=routes,
            defaults=current_app.config,
            form=form,
            auth_enabled=auth_enabled,
            credentials_ready=credentials_ready,
            admin_username=adminauth.current_username(),
        )

    if request.method == "POST":
        form = request.form
        username = (form.get("admin_username") or "").strip()
        password = form.get("admin_password") or ""
        # Credentials may only be *created* here, never replaced: an existing login
        # is changed from Settings (which demands the current password). Otherwise
        # any request carrying a cached Basic session could silently take the
        # account over.
        creating_credentials = auth_enabled and not credentials_ready

        # Bootstrap and migration only: no login exists yet, so one must be chosen
        # here, and the claim code proves the caller can read the server's console.
        if creating_credentials:
            if not adminauth.verify_setup_code((form.get("setup_code") or "").strip()):
                flash("Invalid setup code. Check the MidWare console/logs for the current code.", "error")
                return _render(form)
            if not username or not password:
                flash("Choose an admin username and password.", "error")
                return _render(form)

        # Already has an upstream: this visit is only about re-adding a route.
        if routes:
            if creating_credentials:
                adminauth.set_credentials(username, password)
                flash("Admin credentials saved. Sign in to continue.", "success")
            return redirect(url_for("dashboard.index"))

        target_host = (form.get("target_host") or "").strip()
        upstream_key = (form.get("upstream_key") or "").strip()
        name = (form.get("name") or "").strip() or "Default"
        key_name = (form.get("key_name") or "").strip() or "Default key"
        log_limit = _form_int("request_log_limit", current_app.config["DEFAULT_REQUEST_LOG_LIMIT"])

        if not target_host.startswith(("http://", "https://")):
            flash("Target host must start with http:// or https://", "error")
            return _render(form)

        test = _form_bool("test_connection", True)
        if test and not _connection_test(name, target_host, upstream_key):
            return _render(form)

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
            provider=_form_provider(),
        )
        db.set_setting("request_log_limit", str(log_limit))
        if creating_credentials:
            adminauth.set_credentials(username, password)
        db.mark_configured()
        # Unchecking the test means "do not touch the network"; leave the catalogue
        # for the background/stale refresh instead of calling /v1/models here.
        if test:
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

    return _render({})


@bp.route("/routes", methods=["GET", "POST"])
def routes():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "create":
            name = (request.form.get("name") or "").strip() or "Route"
            target_host = (request.form.get("target_host") or "").strip()
            upstream_key = (request.form.get("upstream_key") or "").strip()
            test = _form_bool("test_connection", True)
            if not target_host.startswith(("http://", "https://")):
                flash("Target host must start with http:// or https://", "error")
            elif test and not _connection_test(name, target_host, upstream_key):
                pass
            else:
                existing = db.list_routes()
                db.create_route(
                    name=name,
                    target_host=target_host,
                    upstream_key=upstream_key,
                    is_active=not existing,
                    provider=_form_provider(),
                )
                db.mark_configured()
                flash(f"Route '{name}' created.", "success")
                if test:
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
                    provider = _form_provider()
                    if provider:
                        # A fresh sign-in retags the route; a plain edit leaves
                        # the existing tag (and the stored key) alone.
                        updates["provider"] = provider
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
        signin_providers=signin.PROVIDERS,
        model_summary={entry["route_id"]: entry for entry in db.model_summary()},
        model_slugs=db.list_models(),
        models_refreshed_at=db.get_meta("models_refreshed_at"),
        models_refresh_error=db.get_meta("models_refresh_error"),
    )


# -- sign in with a service ---------------------------------------------------


def _form_route_id(payload: dict) -> int | None:
    try:
        return int(payload.get("route_id"))
    except (TypeError, ValueError):
        return None


def _suggest_route_name(db, base: str, exclude_route_id: int | None = None) -> str:
    """Pick a route name that collides with nothing.

    The route being edited is excluded from the collision set, so re-signing-in
    on the same route keeps its name instead of walking it up to " (2)".
    """
    taken = {
        str(row["name"]).strip().lower()
        for row in db.list_routes()
        if row["id"] != exclude_route_id
    }
    if base.lower() not in taken:
        return base
    number = 2
    while f"{base} ({number})".lower() in taken:
        number += 1
    return f"{base} ({number})"


@bp.route("/signin/start", methods=["POST"])
def signin_start():
    payload = request.get_json(silent=True) or {}
    provider = (payload.get("provider") or "").strip().lower()
    route_id = _form_route_id(payload)
    client = current_app.extensions["midware_http"]
    try:
        kind = signin.PROVIDERS.get(provider, {}).get("kind")
        if kind == "nanogpt":
            result = signin.start_nanogpt(
                client, current_app.config["NANOGPT_CLIENT_NAME"], route_id, provider
            )
        elif kind == "openrouter":
            callback = url_for("admin.signin_openrouter_callback", _external=True)
            result = signin.begin_openrouter(callback, route_id)
        else:
            return jsonify(ok=False, error="Unknown provider."), 400
    except signin.SignInError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    return jsonify(ok=True, **result)


@bp.route("/signin/poll", methods=["POST"])
def signin_poll():
    payload = request.get_json(silent=True) or {}
    client = current_app.extensions["midware_http"]
    try:
        result = signin.poll_nanogpt(client, (payload.get("token") or "").strip())
    except signin.SignInError as exc:
        return jsonify(ok=False, error=str(exc)), 502
    if result.get("status") == "approved":
        route_id = result.pop("route_id", None)
        result["name"] = _suggest_route_name(get_db(), result["name"], route_id)
    return jsonify(ok=True, **result)


@bp.route("/signin/openrouter/callback")
def signin_openrouter_callback():
    meta = signin.PROVIDERS["openrouter"]
    error = request.args.get("error")
    code = request.args.get("code") or ""
    state = request.args.get("state") or ""
    if error or not code or not state:
        message = (
            "OpenRouter denied the authorization."
            if error
            else "The callback is missing the authorization code."
        )
        return _render_signin_result(meta, error=message, flow=state), 400
    client = current_app.extensions["midware_http"]
    try:
        result = signin.finish_openrouter(client, code, state)
    except signin.SignInError as exc:
        return _render_signin_result(meta, error=str(exc), flow=state), 400
    name = _suggest_route_name(get_db(), result["name"], result.get("route_id"))
    return _render_signin_result(
        meta, key=result["key"], name=name, flow=state, provider=result.get("provider")
    )


def _render_signin_result(
    meta: dict,
    key: str | None = None,
    error: str | None = None,
    flow: str = "",
    name: str | None = None,
    provider: str | None = None,
):
    """The popup page that confirms the sign-in; the key itself is never shown.

    Two channels, in order of reliability: a localStorage entry keyed by the
    flow's state (survives a severed ``window.opener``, which OpenRouter's
    isolation does) and a postMessage to the opener when one still exists. The
    key rides only inside that payload — the rendered page is a close-me note,
    since the form auto-submits as soon as it lands.
    """
    if key:
        payload = {
            "type": "midware-signin",
            "key": key,
            "provider": provider or "",
            "label": meta["label"],
            "name": name or meta["name"],
            "host": meta["host"],
        }
    else:
        payload = {"type": "midware-signin", "error": error or "Sign-in failed."}
    if flow:
        payload["flow"] = flow
    return render_template("signin_result.html", meta=meta, key=key, error=error, payload=payload)


@bp.route("/routes/<int:route_id>/balance")
def route_balance(route_id: int):
    """One route's provider-side balance; the routes page fetches these async."""
    route = get_db().get_route(route_id)
    if route is None:
        return jsonify(ok=False), 404
    text = fetch_balance(
        current_app.extensions["midware_http"],
        route["provider"] or "",
        route["upstream_key"] or "",
    )
    if not text:
        return jsonify(ok=False)
    return jsonify(ok=True, text=text)


@bp.route("/keys", methods=["GET", "POST"])
def keys():
    db = get_db()
    if request.method == "POST":
        if request.form.get("action") == "delete":
            db.delete_api_key(request.form.get("key_id", type=int))
            flash("Key deleted.", "success")
        return redirect(url_for("admin.keys"))

    return render_template("keys.html", api_keys=db.list_api_keys())


def _form_str(name: str, default: str = "") -> str:
    if name not in request.form:
        return default
    return (request.form.get(name) or "").strip()


def _form_ids(name: str) -> list[int]:
    ids: list[int] = []
    for raw in request.form.getlist(name):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def _form_model_ids(name: str) -> list[str]:
    return [value.strip() for value in request.form.getlist(name) if value.strip()]


def _key_form_from_row(row) -> dict:
    from ..db import parse_id_list, parse_model_list

    form = {
        "name": row["name"],
        "description": row["description"] or "",
        "cors_allow_origin": row["cors_allow_origin"],
        "allow_premodels": bool(row["allow_premodels"]),
        "restrict_premodels": bool(row["restrict_premodels"]),
        "allowed_premodel_ids": sorted(parse_id_list(row["allowed_premodel_ids"])),
        "restrict_providers": bool(row["restrict_providers"]),
        "allowed_route_ids": sorted(parse_id_list(row["allowed_route_ids"])),
        "restrict_models": bool(row["restrict_models"]),
        "allowed_model_ids": sorted(parse_model_list(row["allowed_model_ids"])),
    }
    for flag in LIMIT_FLAG_FIELDS:
        form[flag] = bool(row[flag])
    for value in LIMIT_VALUE_FIELDS:
        form[value] = int(row[value] or 0)
    return form


def _read_key_form() -> tuple[dict, list[str]]:
    fields = {
        "name": (request.form.get("name") or "").strip(),
        "description": (request.form.get("description") or "").strip()[:DESCRIPTION_MAX],
        "cors_allow_origin": _form_str("cors_allow_origin", "*"),
        "allow_premodels": _form_bool("allow_premodels"),
        "restrict_premodels": _form_bool("restrict_premodels"),
        "allowed_premodel_ids": _form_ids("allowed_premodel_ids"),
        "restrict_providers": _form_bool("restrict_providers"),
        "allowed_route_ids": _form_ids("allowed_route_ids"),
        "restrict_models": _form_bool("restrict_models"),
        "allowed_model_ids": _form_model_ids("allowed_model_ids"),
    }
    # Children survive a master toggle in the form; enforcement is gated on the
    # master flag alone, so re-enabling restores the previous configuration.
    for flag in LIMIT_FLAG_FIELDS:
        fields[flag] = _form_bool(flag)
    for value in LIMIT_VALUE_FIELDS:
        fields[value] = _form_int(value, 0, maximum=_LIMIT_MAX)
    if not fields["restrict_models"]:
        fields["allowed_model_ids"] = []
    errors: list[str] = []
    if not fields["name"]:
        errors.append("Name is required.")
    return fields, errors


def _render_key_form(db, form, api_key):
    return render_template(
        "key_form.html",
        key=api_key,
        form=form,
        routes=db.list_routes(),
        premodels=db.list_premodels(),
        models=db.list_models(),
        premodel_prefix=_premodel_prefix(db),
    )


def _save_key(db, existing):
    fields, errors = _read_key_form()
    if errors:
        for message in errors:
            flash(message, "error")
        return _render_key_form(db, fields, existing)
    if existing is None:
        token = db.create_api_key(**fields)
        flash(f"Created key '{fields['name']}': {token}", "success")
    else:
        db.update_api_key(existing["id"], **fields)
        flash(f"Key '{fields['name']}' updated.", "success")
    return redirect(url_for("admin.keys"))


@bp.route("/keys/new", methods=["GET", "POST"])
def key_new():
    db = get_db()
    if request.method == "POST":
        return _save_key(db, None)
    return _render_key_form(db, {}, None)


@bp.route("/keys/<int:key_id>/edit", methods=["GET", "POST"])
def key_edit(key_id: int):
    db = get_db()
    existing = db.get_api_key(key_id)
    if existing is None:
        abort(404)
    if request.method == "POST":
        return _save_key(db, existing)
    return _render_key_form(db, _key_form_from_row(existing), existing)


def _premodel_prefix(db) -> str:
    """The live premodel addressing prefix, falling back to the config default."""
    return db.premodel_prefix(current_app.config["DEFAULT_PREMODEL_PREFIX"])


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "purge":
            removed = db.purge_requests()
            flash(f"Purged {removed} logged request(s).", "success")
            return redirect(url_for("admin.settings"))
        if action == "credentials":
            return _update_credentials()
        if action == "reset_premodel_prefix":
            default = current_app.config["DEFAULT_PREMODEL_PREFIX"]
            db.set_setting("premodel_prefix", default)
            flash(f"Premodel prefix reset to '{default}'.", "success")
            return redirect(url_for("admin.settings"))

        log_limit = _form_int("request_log_limit", db.request_log_limit())
        db.set_setting("request_log_limit", str(log_limit))
        db.prune_requests()
        saved = f"Settings saved. Request log limit set to {log_limit}."
        if "premodel_prefix" in request.form:
            prefix = normalize_prefix(request.form.get("premodel_prefix"))
            if prefix is None:
                flash("Premodel prefix must be 1-32 characters without spaces.", "error")
            else:
                db.set_setting("premodel_prefix", prefix)
                saved += f" Premodel prefix is now '{prefix}'."
        flash(saved, "success")
        return redirect(url_for("admin.settings"))

    return render_template(
        "settings.html",
        log_limit=db.request_log_limit(),
        premodel_prefix=_premodel_prefix(db),
        default_premodel_prefix=current_app.config["DEFAULT_PREMODEL_PREFIX"],
        configured=db.is_configured(),
        configured_at=db.get_meta("configured_at"),
        route_count=len(db.list_routes()),
        key_count=db.count_api_keys(),
        request_count=db.count_requests(),
        max_capture=current_app.config["MAX_CAPTURE_BYTES"],
        upstream_timeout=current_app.config["UPSTREAM_TIMEOUT"],
        verify_tls=current_app.config["UPSTREAM_VERIFY_TLS"],
        admin_username=adminauth.current_username(),
        admin_auth_enabled=adminauth.enabled(),
        admin_env_override=adminauth.env_override_active(),
        admin_credentials_at=adminauth.credentials_set_at(),
    )


def _update_credentials():
    if not adminauth.enabled():
        flash("Admin authentication is disabled.", "error")
        return redirect(url_for("admin.settings"))
    if adminauth.env_override_active():
        flash("MIDWARE_ADMIN_PASSWORD is set; change the login there instead.", "error")
        return redirect(url_for("admin.settings"))

    current = request.form.get("current_password") or ""
    username = (request.form.get("admin_username") or "").strip()
    password = request.form.get("admin_password") or ""
    confirm = request.form.get("admin_password_confirm") or ""

    if not adminauth.verify(adminauth.current_username(), current):
        flash("Current password is incorrect.", "error")
    elif not username or not password:
        flash("New username and password are required.", "error")
    elif password != confirm:
        flash("New passwords do not match.", "error")
    else:
        adminauth.set_credentials(username, password)
        flash("Admin credentials updated.", "success")
    return redirect(url_for("admin.settings"))


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

    use_workflow = _form_bool("use_workflow")
    workflow_json = existing["workflow_json"] if existing is not None else None
    workflow_error = None
    if use_workflow:
        raw_workflow = (form.get("workflow_json") or "").strip()
        try:
            workflow_json = json.dumps(normalize_workflow(raw_workflow), ensure_ascii=False)
        except WorkflowError as exc:
            workflow_error = str(exc)
            # Keep the raw blob so the editor re-opens with the user's work.
            workflow_json = raw_workflow or None

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
        "use_workflow": use_workflow,
        "workflow_json": workflow_json,
    }
    # Children survive a master toggle in the form; enforcement is gated on the
    # master flag alone, so re-enabling restores the previous configuration.
    for flag in LIMIT_FLAG_FIELDS:
        fields[flag] = _form_bool(flag)
    for value in LIMIT_VALUE_FIELDS:
        fields[value] = _form_int(value, 0, maximum=_LIMIT_MAX)

    errors: list[str] = list(param_errors)
    if not name:
        errors.append("Name is required.")
    if not slug:
        errors.append("Slug is required.")
    if route_id is None or db.get_route(route_id) is None:
        errors.append("Choose a connection.")
    if not model:
        errors.append("Choose or type a model.")
    if not use_workflow and prompt_mode == "sillytavern" and not preset_json:
        errors.append("Upload a SillyTavern preset with at least one prompt.")
    if workflow_error:
        errors.append(workflow_error)
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
    workflow_data = None
    raw_workflow = form.get("workflow_json") if isinstance(form, dict) else None
    if raw_workflow:
        try:
            workflow_data = json.loads(raw_workflow)
        except (ValueError, TypeError):
            workflow_data = None
    params = parse_params(form.get("params_json")) if isinstance(form, dict) else {}
    return render_template(
        "premodel_form.html",
        premodel=premodel,
        routes=db.list_routes(),
        models=db.list_models(),
        form=form,
        preset_data=preset_data,
        workflow_data=workflow_data,
        params=params,
        sampling_keys=SAMPLING_KEYS,
        premodel_prefix=_premodel_prefix(db),
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
        premodel_prefix=_premodel_prefix(db),
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
