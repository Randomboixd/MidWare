"""HTTP surfaces for System Management Accounts.

Two blueprints share this module: the machine-facing ``/_midware/api/v1`` and the
human-facing consent page under ``/_midware/authorize``. They are separate so the
admin guard can wrap the browser side without touching the signed JSON surface.

The guard here is deliberately strict: every endpoint except enrollment and the
server-key discovery must present a bearer token, a matching account header, a
fresh timestamp, an unused nonce and a valid signature, and every endpoint must
declare the scope it needs. An endpoint added without a scope fails closed.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Callable
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException

from .. import adminauth, images, sma
from ..auth import get_db
from ..db import dump_id_list, dump_model_list, parse_iso, to_iso, utcnow
from ..msq import run_recorder

api_bp = Blueprint("sma", __name__, url_prefix="/_midware/api/v1")
consent_bp = Blueprint("sma_consent", __name__, url_prefix="/_midware/authorize")

PUBLIC_ENDPOINTS = frozenset({"sma.server_key", "sma.enroll"})

# Endpoints an account may call without a grantable scope: activation handshakes,
# reading its own identity, and shutting itself off.
UNSCOPED_ENDPOINTS = frozenset(
    {
        "sma.activate",
        "sma.status",
        "sma.exchange",
        "sma.self_info",
        "sma.disable_self",
    }
)

# Which account states may use which signed endpoint. Anything unlisted is
# active-only, so a pending account cannot reach the interact surface.
ENDPOINT_STATES: dict[str, frozenset[str]] = {
    "sma.activate": frozenset({"pending", "approved"}),
    # Status is the one call a client makes to learn its fate, so every state is
    # allowed; the token itself still gates it.
    "sma.status": frozenset({"pending", "approved", "active", "denied", "revoked"}),
    "sma.exchange": frozenset({"approved"}),
}

_CSRF_SESSION_KEY = "sma_csrf"
_MAX_NAME = sma.MAX_NAME_LENGTH


def scoped(scope: str) -> Callable:
    """Declare the permission an endpoint needs; the guard enforces it."""

    def decorator(view: Callable) -> Callable:
        view.__sma_scope__ = scope  # type: ignore[attr-defined]
        return view

    return decorator


# -- shared helpers ----------------------------------------------------------


def _error(code: str, message: str, status: int) -> tuple[Response, int]:
    return jsonify(sma.SmaError(code, message, status).payload()), status


def _signed_path() -> str:
    """The path component of the signature, query string included.

    Signing the query string stops a client (or an interceptor) from dropping or
    rewriting parameters that the view later reads.
    """
    query = request.query_string.decode("utf-8", "replace")
    return f"{request.path}?{query}" if query else request.path


def _bearer() -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise sma.SmaError(
            "missing_token",
            "Send the account token as 'Authorization: Bearer <token>'.",
            401,
        )
    return value.strip()


def _json_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise sma.SmaError("invalid_body", "Send a JSON object.")
    return body


def _str_field(body: dict[str, Any], key: str, *, required: bool = False, default: str = "") -> str:
    value = body.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise sma.SmaError("invalid_body", f"'{key}' must be a string.")
    value = value.strip() if key in {"name", "model", "target_host", "provider"} else value
    if required and not value:
        raise sma.SmaError("invalid_body", f"'{key}' is required.")
    return value


def _bool_field(body: dict[str, Any], key: str, default: bool) -> bool:
    if key not in body:
        return default
    value = body[key]
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_field(body: dict[str, Any], key: str, default: int) -> int:
    if key not in body or body[key] is None:
        return default
    try:
        return int(body[key])
    except (TypeError, ValueError) as exc:
        raise sma.SmaError("invalid_body", f"'{key}' must be an integer.") from exc


def _int_list(body: dict[str, Any], key: str) -> list[int]:
    raw = body.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise sma.SmaError("invalid_body", f"'{key}' must be a list of integers.")
    values: list[int] = []
    for item in raw:
        try:
            values.append(int(item))
        except (TypeError, ValueError) as exc:
            raise sma.SmaError("invalid_body", f"'{key}' must be a list of integers.") from exc
    return values


def _str_list(body: dict[str, Any], key: str) -> list[str]:
    raw = body.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise sma.SmaError("invalid_body", f"'{key}' must be a list of strings.")
    return [item.strip() for item in raw if item.strip()]


def _client_ip() -> str:
    return (request.remote_addr or "")[:64]


def _transport() -> str:
    return "tls" if request.is_secure else "plain"


def _transport_blocked(db: Any) -> bool:
    """Plain HTTP is off unless the server explicitly opts in."""
    return not request.is_secure and not db.sma_allow_plain_http(
        current_app.config["SMA_ALLOW_PLAIN_HTTP"]
    )


# -- API guard ---------------------------------------------------------------


@api_bp.before_request
def _guard() -> Any:
    db = get_db()
    config = current_app.config

    if not db.sma_enabled(config["SMA_ENABLED"]):
        return _error(
            "sma_disabled",
            "The System Management Accounts API is disabled on this MidWare server.",
            503,
        )

    if _transport_blocked(db):
        return _error(
            "insecure_transport",
            "The System Management Accounts API requires HTTPS. Serve MidWare "
            "over TLS, or enable 'Allow over plain HTTP' in Settings if this "
            "instance only ever runs on a trusted network.",
            403,
        )

    max_body = int(config["SMA_MAX_BODY_BYTES"])
    if request.content_length and request.content_length > max_body:
        return _error("body_too_large", f"Request bodies are capped at {max_body} bytes.", 413)

    db.expire_system_accounts()

    body = request.get_data(cache=True)
    if len(body) > max_body:
        return _error("body_too_large", f"Request bodies are capped at {max_body} bytes.", 413)

    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    try:
        account = _authenticate(db, body)

        allowed = ENDPOINT_STATES.get(request.endpoint or "", frozenset({"active"}))
        if account["state"] not in allowed:
            raise sma.SmaError(
                "wrong_account_state",
                f"This account is '{account['state']}' and cannot use that endpoint.",
                403,
            )

        view = current_app.view_functions.get(request.endpoint or "")
        required = getattr(view, "__sma_scope__", None)
        if required is None:
            if request.endpoint not in UNSCOPED_ENDPOINTS:
                # A route without a declared scope must never become an open door.
                raise sma.SmaError("endpoint_unscoped", "This endpoint is not available.", 403)
        elif required not in sma.parse_scopes(account["scopes"]):
            raise sma.SmaError(
                "scope_denied",
                f"This account does not have the '{required}' permission.",
                403,
            )

        limits = {"minute": max(1, int(config["SMA_REQUESTS_PER_MINUTE"]))}
        _, ok = db.system_rate_usage_claim(int(account["id"]), limits)
        if not ok:
            response, status = _error(
                "rate_limit_exceeded",
                "This account is calling the System Management API too quickly.",
                429,
            )
            response.headers["Retry-After"] = "60"
            return response, status
    except sma.SmaError as exc:
        return jsonify(exc.payload()), exc.status
    except HTTPException as exc:
        return jsonify(_http_payload(exc)), exc.code or 500

    g.sma_account = account
    g.sma_touch = True
    return None


def _authenticate(db: Any, body: bytes) -> Any:
    """Resolve and verify the signed request, returning the account row."""
    token = _bearer()
    kind = sma.token_kind(token)
    if kind is None:
        raise sma.SmaError("invalid_token", "That is not a System Management token.", 401)

    account = db.get_system_account_by_token(sma.hash_token(token), kind)
    if account is None:
        raise sma.SmaError("invalid_token", "Unknown or retired account token.", 401)

    header_id = request.headers.get("X-Midware-Account", "").strip()
    if header_id != str(account["id"]):
        raise sma.SmaError(
            "account_mismatch",
            "X-Midware-Account does not match the presented token.",
            401,
        )

    timestamp = request.headers.get("X-Midware-Timestamp", "").strip()
    nonce = request.headers.get("X-Midware-Nonce", "").strip()
    signature = request.headers.get("X-Midware-Signature", "").strip()

    # Remember the caller before verifying, so even a rejected request gets a
    # signed error response (the client can then trust *why* it was rejected).
    g.sma_account = account
    g.sma_nonce = nonce
    g.sma_timestamp = timestamp

    window = int(current_app.config["SMA_NONCE_WINDOW_SECONDS"])
    if not sma.clock_skew(timestamp, window):
        raise sma.SmaError(
            "stale_timestamp",
            f"X-Midware-Timestamp must be within {window} seconds of the server clock.",
            401,
        )

    sma.verify_request_signature(
        account["public_key"],
        int(account["id"]),
        request.method,
        _signed_path(),
        timestamp,
        nonce,
        body,
        signature,
    )

    # Only now, after the signature proves possession, is the nonce burned.
    if not db.consume_system_nonce(int(account["id"]), nonce, sma.expiry(window + 60)):
        raise sma.SmaError("replayed_nonce", "That request was already used.", 401)

    g.sma_nonce = nonce
    g.sma_timestamp = timestamp
    g.sma_account = account
    return account


def sign_api_response(response: Response) -> Response:
    """Sign every ``/_midware/api`` response, including errors.

    Registered app-wide rather than on the blueprint so a routing 404 (which
    never matches the blueprint) is still a JSON, signed answer. A request that
    carries no usable account id or nonce cannot be signed; the client sees a
    normal unsigned error in that case.
    """
    if not request.path.startswith("/_midware/api/"):
        return response
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"

    account = g.get("sma_account")
    account_id: int | None
    if account is not None:
        account_id = int(account["id"])
    else:
        try:
            account_id = int(request.headers.get("X-Midware-Account", "") or 0) or None
        except ValueError:
            account_id = None

    nonce = g.get("sma_nonce") or request.headers.get("X-Midware-Nonce", "").strip()
    timestamp = g.get("sma_timestamp") or request.headers.get(
        "X-Midware-Timestamp", ""
    ).strip()
    if account_id is not None and 0 < len(nonce) <= 128:
        headers = sma.response_signature(
            get_db(),
            account_id,
            request.method,
            _signed_path(),
            timestamp,
            nonce,
            response.get_data(),
        )
        response.headers.update(headers)

    if account is not None and g.get("sma_touch"):
        try:
            get_db().update_system_account(int(account["id"]), last_used_at=to_iso(utcnow()))
        except Exception:  # bookkeeping must never break a response
            current_app.logger.debug("sma last_used_at update failed", exc_info=True)
    return response


def _http_payload(exc: HTTPException) -> dict[str, Any]:
    return {
        "error": {
            "message": exc.description or exc.name,
            "type": "midware_system_account_error",
            "code": (exc.name or "http_error").lower().replace(" ", "_"),
        }
    }


@api_bp.errorhandler(sma.SmaError)
def _handle_sma_error(exc: sma.SmaError) -> tuple[Response, int]:
    return jsonify(exc.payload()), exc.status


@api_bp.errorhandler(HTTPException)
def _handle_http_error(exc: HTTPException) -> tuple[Response, int]:
    return jsonify(_http_payload(exc)), exc.code or 500


def _audit(action: str, *, target: Any = "", status: int = 200, detail: str = "") -> None:
    account = g.get("sma_account")
    get_db().record_system_event(
        int(account["id"]) if account is not None else None,
        action,
        method=request.method,
        path=request.path,
        target_id=target,
        status=status,
        ip=_client_ip(),
        detail=detail,
    )


# -- discovery and enrollment ------------------------------------------------


@api_bp.get("/server-key")
def server_key() -> Any:
    db = get_db()
    return jsonify(
        {
            "api_version": "v1",
            "server": sma.server_key_payload(db),
            "enabled": True,
            "transport": _transport(),
        }
    )


@api_bp.post("/EnrollSystemAccount")
def enroll() -> Any:
    db = get_db()
    config = current_app.config
    body = _json_body()

    name = sma.normalize_name(body.get("name"))
    public_key = body.get("public_key")
    redirect_uri = sma.normalize_redirect_uri(body.get("redirect_uri"))
    scopes = sma.normalize_scopes(body.get("scopes"))
    description = _str_field(body, "description")[:500]

    # Validating the key before touching the database also rejects PEM blobs and
    # wrong-length strings with a clear message.
    sma.decode_public_key(public_key)
    finger = sma.key_fingerprint(public_key)

    ip = _client_ip()
    db.expire_system_accounts()

    if db.count_system_accounts_in_state("pending") >= int(config["SMA_MAX_PENDING"]):
        raise sma.SmaError(
            "too_many_pending",
            "This server already has the maximum number of pending accounts.",
            429,
        )
    since = sma.since(3600)
    if db.count_recent_enrollments(ip, since) >= int(config["SMA_ENROLL_PER_HOUR"]):
        raise sma.SmaError(
            "enrollment_throttled",
            "Too many enrollment attempts from this address. Try again later.",
            429,
        )
    if db.system_account_name_holder(name) is not None:
        raise sma.SmaError("name_taken", f"An account named '{name}' already exists.", 409)
    if db.system_account_key_holder(public_key) is not None:
        raise sma.SmaError(
            "public_key_already_enrolled",
            "That public key already belongs to a pending or active account.",
            409,
        )

    frozen = {
        "name": name,
        "public_key": public_key,
        "redirect_uri": redirect_uri,
        "scopes": sorted(scopes),
    }
    pending_token = sma.new_token(sma.TOKEN_PENDING_PREFIX)
    account_id = db.create_system_account(
        name=name,
        description=description,
        public_key=public_key,
        key_fingerprint=finger,
        redirect_uri=redirect_uri,
        scopes=json.dumps(sorted(scopes)),
        payload_hash=sma.enrollment_payload_hash(frozen),
        pending_token_hash=sma.hash_token(pending_token),
        requested_ip=ip,
        requested_transport=_transport(),
        expires_at=sma.expiry(int(config["SMA_PENDING_TTL_SECONDS"])),
    )
    db.record_system_event(
        account_id,
        "enroll",
        method=request.method,
        path=request.path,
        status=201,
        ip=ip,
        detail=";".join(sorted(scopes)),
    )

    return (
        jsonify(
            {
                "account_id": account_id,
                "name": name,
                "state": "pending",
                "scopes": sorted(scopes),
                "key_fingerprint": finger,
                "pending_token": pending_token,
                "expires_at": db.get_system_account(account_id)["expires_at"],
                "server": sma.server_key_payload(db),
                "transport": _transport(),
            }
        ),
        201,
    )


# -- activation --------------------------------------------------------------


@api_bp.post("/ActivateSystemAccount")
def activate() -> Any:
    db = get_db()
    account = g.sma_account
    expires = sma.expiry(int(current_app.config["SMA_ACTIVATION_TTL_SECONDS"]))
    token = secrets.token_urlsafe(32)
    db.update_system_account(
        int(account["id"]),
        activation_token_hash=sma.hash_token(token),
        activation_expires_at=expires,
    )
    _audit("activate", status=200)
    return jsonify(
        {
            "state": account["state"],
            "authorization_url": url_for("sma_consent.consent", token=token, _external=True),
            "expires_at": expires,
        }
    )


@api_bp.get("/ActivateSystemAccount/status")
def status() -> Any:
    account = g.sma_account
    _audit("status", status=200)
    return jsonify({"state": account["state"], "account_id": int(account["id"])})


@api_bp.post("/ActivateSystemAccount/exchange")
def exchange() -> Any:
    db = get_db()
    account = g.sma_account
    expires = parse_iso(account["activation_expires_at"])
    if expires is None or expires <= utcnow():
        raise sma.SmaError(
            "activation_expired",
            "The consent link expired. Call ActivateSystemAccount again.",
            410,
        )

    token = sma.new_token(sma.TOKEN_LIVE_PREFIX)
    db.update_system_account(
        int(account["id"]),
        state="active",
        token_hash=sma.hash_token(token),
        pending_token_hash=None,
        activation_token_hash=None,
        activation_expires_at=None,
        activated_at=to_iso(utcnow()),
        expires_at=None,
    )
    _audit("exchange", status=200)
    return jsonify(
        {
            "account_id": int(account["id"]),
            "name": account["name"],
            "state": "active",
            "token": token,
            "scopes": sma.parse_scopes(account["scopes"]),
            "server": sma.server_key_payload(db),
        }
    )


# -- account self-service ----------------------------------------------------


@api_bp.get("/interact/self")
def self_info() -> Any:
    _audit("self", status=200)
    return jsonify(sma.public_account(g.sma_account))


@api_bp.post("/DisableSystemAccount")
def disable_self() -> Any:
    db = get_db()
    account = g.sma_account
    deleted = sma.revoke_account(db, int(account["id"]), "self-disabled")
    _audit("disable", status=200, detail=f"{deleted} key(s) deleted")
    return jsonify({"state": "revoked", "account_id": int(account["id"])})


# -- routes ------------------------------------------------------------------


def _route_payload(db: Any, route_id: int) -> dict[str, Any]:
    row = db.get_route(route_id)
    if row is None:
        raise sma.SmaError("route_not_found", "No such route.", 404)
    return sma.redact_route(row)


def _validate_host(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise sma.SmaError("invalid_target_host", "target_host must be an absolute http(s) URL.")
    if parts.username or parts.password or parts.fragment or parts.query:
        raise sma.SmaError(
            "invalid_target_host",
            "target_host must be a bare http(s) base URL.",
        )
    return candidate


@api_bp.get("/interact/routes")
@scoped(sma.READ_ROUTES)
def list_routes() -> Any:
    _audit("routes.list", status=200)
    return jsonify({"data": [sma.redact_route(row) for row in get_db().list_routes()]})


@api_bp.get("/interact/routes/<int:route_id>")
@scoped(sma.READ_ROUTES)
def get_route(route_id: int) -> Any:
    _audit("routes.get", target=route_id, status=200)
    return jsonify(_route_payload(get_db(), route_id))


@api_bp.post("/interact/routes/create")
@scoped(sma.WRITE_ROUTES)
def create_route() -> Any:
    db = get_db()
    body = _json_body()
    name = _str_field(body, "name", required=True)[:_MAX_NAME]
    target_host = _validate_host(_str_field(body, "target_host", required=True))
    upstream_key = _str_field(body, "upstream_key")
    is_active = _bool_field(body, "is_active", True)
    provider = _str_field(body, "provider")
    api_key_id = body.get("api_key_id")
    route_id = db.create_route(
        name=name,
        target_host=target_host,
        upstream_key=upstream_key,
        api_key_id=int(api_key_id) if api_key_id is not None else None,
        is_active=is_active,
        provider=provider,
    )
    _audit("routes.create", target=route_id, status=201, detail=name)
    return jsonify(_route_payload(db, route_id)), 201


@api_bp.post("/interact/routes/<int:route_id>/update")
@scoped(sma.EDIT_ROUTES)
def update_route(route_id: int) -> Any:
    db = get_db()
    if db.get_route(route_id) is None:
        raise sma.SmaError("route_not_found", "No such route.", 404)
    body = _json_body()
    fields: dict[str, Any] = {}
    if "name" in body:
        fields["name"] = _str_field(body, "name", required=True)[:_MAX_NAME]
    if "target_host" in body:
        fields["target_host"] = _validate_host(_str_field(body, "target_host", required=True))
    if "upstream_key" in body:
        fields["upstream_key"] = _str_field(body, "upstream_key")
    if "provider" in body:
        fields["provider"] = _str_field(body, "provider")
    if "is_active" in body:
        fields["is_active"] = _bool_field(body, "is_active", True)
    if "api_key_id" in body:
        value = body.get("api_key_id")
        fields["api_key_id"] = int(value) if value is not None else None
    if not fields:
        raise sma.SmaError("invalid_body", "No editable route fields were supplied.")
    db.update_route(route_id, **fields)
    _audit("routes.update", target=route_id, status=200)
    return jsonify(_route_payload(db, route_id))


@api_bp.post("/interact/routes/<int:route_id>/delete")
@scoped(sma.WRITE_ROUTES)
def delete_route(route_id: int) -> Any:
    db = get_db()
    row = db.get_route(route_id)
    if row is None:
        raise sma.SmaError("route_not_found", "No such route.", 404)
    db.delete_route(route_id)
    _audit("routes.delete", target=route_id, status=200, detail=row["name"])
    return jsonify({"deleted": route_id})


# -- owned API keys ----------------------------------------------------------


def _owned_key(db: Any, key_id: int) -> Any:
    row = db.get_owned_api_key(int(g.sma_account["id"]), key_id)
    if row is None:
        # Deliberately identical to a missing key: the account may not learn
        # whether some other owner's key exists.
        raise sma.SmaError("api_key_not_found", "No such API key owned by this account.", 404)
    return row


def _require_reachable_scope(db: Any, body: dict[str, Any]) -> None:
    """Refuse to mint (or widen into) a proxy key outside the account's reach.

    An API key is not scoped by SMA permissions, so an unrestricted key would be
    a way around every other limit. Keys must name route or model ids, and naming
    a route requires ``read:routes`` — an account that cannot see routes has no
    business handing them out.
    """
    restrict_providers = _bool_field(body, "restrict_providers", False)
    restrict_models = _bool_field(body, "restrict_models", False)
    route_ids = _int_list(body, "allowed_route_ids")
    model_ids = _str_list(body, "allowed_model_ids")
    scoped_by_routes = restrict_providers and bool(route_ids)
    scoped_by_models = restrict_models and bool(model_ids)
    if not (scoped_by_routes or scoped_by_models):
        raise sma.SmaError(
            "apikey_scope_too_broad",
            "A key created this way must name route ids (restrict_providers) "
            "or model ids (restrict_models); unrestricted keys are not allowed.",
        )

    account_scopes = sma.parse_scopes(g.sma_account["scopes"])
    if route_ids:
        if sma.READ_ROUTES not in account_scopes:
            raise sma.SmaError(
                "scope_denied",
                "Naming route ids requires the 'midware.read:routes' permission.",
                403,
            )
        for route_id in route_ids:
            if db.get_route(route_id) is None:
                raise sma.SmaError("route_not_found", f"Route {route_id} does not exist.", 404)

    for entry in model_ids:
        route_part, separator, model_part = entry.partition(":")
        if not separator or not model_part:
            raise sma.SmaError(
                "invalid_body",
                "allowed_model_ids entries must be '<route_id>:<model_id>'.",
            )
        try:
            route_id = int(route_part)
        except ValueError as exc:
            raise sma.SmaError(
                "invalid_body",
                "allowed_model_ids entries must be '<route_id>:<model_id>'.",
            ) from exc
        if sma.READ_ROUTES not in account_scopes:
            raise sma.SmaError(
                "scope_denied",
                "Naming model ids requires the 'midware.read:routes' permission.",
                403,
            )
        if db.get_route(route_id) is None:
            raise sma.SmaError("route_not_found", f"Route {route_id} does not exist.", 404)


@api_bp.get("/interact/apikeys")
@scoped(sma.READ_APIKEYS_OWNED)
def list_owned_keys() -> Any:
    db = get_db()
    _audit("apikeys.list", status=200)
    return jsonify({"data": [sma.redact_api_key(row) for row in db.list_api_keys_owned(int(g.sma_account["id"]))]})


@api_bp.get("/interact/apikeys/<int:key_id>")
@scoped(sma.READ_APIKEYS_OWNED)
def get_owned_key(key_id: int) -> Any:
    db = get_db()
    _audit("apikeys.get", target=key_id, status=200)
    return jsonify(sma.redact_api_key(_owned_key(db, key_id)))


@api_bp.post("/interact/apikeys/create")
@scoped(sma.WRITE_APIKEYS)
def create_owned_key() -> Any:
    db = get_db()
    body = _json_body()
    name = _str_field(body, "name", required=True)[:_MAX_NAME]
    _require_reachable_scope(db, body)
    token = db.create_api_key(
        name=name,
        description=_str_field(body, "description")[:500],
        cors_allow_origin=_str_field(body, "cors_allow_origin", default="*") or "*",
        allow_premodels=_bool_field(body, "allow_premodels", True),
        restrict_premodels=_bool_field(body, "restrict_premodels", False),
        allowed_premodel_ids=_int_list(body, "allowed_premodel_ids"),
        restrict_providers=_bool_field(body, "restrict_providers", False),
        allowed_route_ids=_int_list(body, "allowed_route_ids"),
        restrict_models=_bool_field(body, "restrict_models", False),
        allowed_model_ids=_str_list(body, "allowed_model_ids"),
        limit_tokens_enabled=_bool_field(body, "limit_tokens_enabled", False),
        limit_tokens_hour=_bool_field(body, "limit_tokens_hour", False),
        max_tokens_hour=_int_field(body, "max_tokens_hour", 0),
        limit_tokens_day=_bool_field(body, "limit_tokens_day", False),
        max_tokens_day=_int_field(body, "max_tokens_day", 0),
        limit_tokens_week=_bool_field(body, "limit_tokens_week", False),
        max_tokens_week=_int_field(body, "max_tokens_week", 0),
        limit_requests_enabled=_bool_field(body, "limit_requests_enabled", False),
        limit_requests_minute=_bool_field(body, "limit_requests_minute", False),
        max_requests_minute=_int_field(body, "max_requests_minute", 0),
        limit_requests_hour=_bool_field(body, "limit_requests_hour", False),
        max_requests_hour=_int_field(body, "max_requests_hour", 0),
        limit_requests_day=_bool_field(body, "limit_requests_day", False),
        max_requests_day=_int_field(body, "max_requests_day", 0),
        limit_requests_week=_bool_field(body, "limit_requests_week", False),
        max_requests_week=_int_field(body, "max_requests_week", 0),
        image_response_mode=_str_field(body, "image_response_mode", default="b64") or "b64",
        image_retention_days=_int_field(body, "image_retention_days", 0),
        image_url_https=_bool_field(body, "image_url_https", False),
        log_requests=_bool_field(body, "log_requests", True),
        log_message_contents=_bool_field(body, "log_message_contents", True),
        owner_system_account_id=int(g.sma_account["id"]),
    )
    key = db.get_api_key_by_token(token)
    payload = sma.redact_api_key(key) if key is not None else {}
    payload["token"] = token
    _audit("apikeys.create", target=payload.get("id", ""), status=201, detail=name)
    return jsonify(payload), 201


@api_bp.post("/interact/apikeys/<int:key_id>/update")
@scoped(sma.EDIT_APIKEYS_OWNED)
def update_owned_key(key_id: int) -> Any:
    db = get_db()
    existing = _owned_key(db, key_id)
    body = _json_body()

    merged = {
        "restrict_providers": existing["restrict_providers"],
        "allowed_route_ids": _split_ids(existing["allowed_route_ids"]),
        "restrict_models": existing["restrict_models"],
        "allowed_model_ids": _split_models(existing["allowed_model_ids"]),
    }
    for key in ("restrict_providers", "restrict_models"):
        if key in body:
            merged[key] = _bool_field(body, key, bool(existing[key]))
    for key in ("allowed_route_ids", "allowed_model_ids"):
        if key in body:
            merged[key] = _int_list(body, key) if key == "allowed_route_ids" else _str_list(body, key)
    _require_reachable_scope(db, merged)

    fields: dict[str, Any] = {}
    for key in ("name", "description", "cors_allow_origin", "image_response_mode"):
        if key in body:
            fields[key] = _str_field(body, key)
    if "name" in fields:
        fields["name"] = fields["name"][:_MAX_NAME]
        if not fields["name"]:
            raise sma.SmaError("invalid_body", "'name' cannot be empty.")
    for key in (
        "allow_premodels",
        "restrict_premodels",
        "restrict_providers",
        "restrict_models",
        "image_url_https",
        "log_requests",
        "log_message_contents",
        "limit_tokens_enabled",
        "limit_tokens_hour",
        "limit_tokens_day",
        "limit_tokens_week",
        "limit_requests_enabled",
        "limit_requests_minute",
        "limit_requests_hour",
        "limit_requests_day",
        "limit_requests_week",
    ):
        if key in body:
            fields[key] = _bool_field(body, key, False)
    for key in (
        "image_retention_days",
        "max_tokens_hour",
        "max_tokens_day",
        "max_tokens_week",
        "max_requests_minute",
        "max_requests_hour",
        "max_requests_day",
        "max_requests_week",
    ):
        if key in body:
            fields[key] = max(0, _int_field(body, key, 0))
    for key in ("allowed_premodel_ids", "allowed_route_ids"):
        if key in body:
            fields[key] = dump_id_list(_int_list(body, key))
    if "allowed_model_ids" in body:
        fields["allowed_model_ids"] = dump_model_list(_str_list(body, "allowed_model_ids"))
    if not fields:
        raise sma.SmaError("invalid_body", "No editable key fields were supplied.")

    db.update_api_key(key_id, **fields)
    _audit("apikeys.update", target=key_id, status=200)
    return jsonify(sma.redact_api_key(_owned_key(db, key_id)))


def _split_ids(raw: Any) -> list[int]:
    try:
        return [int(item) for item in json.loads(raw or "[]")]
    except (TypeError, ValueError):
        return []


def _split_models(raw: Any) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [item for item in values if isinstance(item, str)]


# -- key on/off switch -------------------------------------------------------


def _switchable_key(db: Any, key_id: int) -> Any:
    """Any real key, regardless of owner; the hidden system key is off limits."""
    row = db.get_api_key(key_id)
    if row is None or row["is_system"]:
        raise sma.SmaError("api_key_not_found", "No such API key.", 404)
    return row


def _key_status(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "owner_system_account_id": row["owner_system_account_id"],
    }


@api_bp.get("/interact/apikeys/status")
@scoped(sma.READ_APIKEYS_STATUS)
def list_key_status() -> Any:
    """Every real key's on/off state, names only.

    The status scopes are global, so the listing has to be too: an account that
    may disable any key needs to find it by more than a guessable id.
    """
    db = get_db()
    _audit("apikeys.status.list", status=200)
    return jsonify(
        {
            "data": [
                {
                    **_key_status(row),
                    "last_used_at": row["last_used_at"],
                }
                for row in db.list_api_keys()
            ]
        }
    )


@api_bp.get("/interact/apikeys/<int:key_id>/status")
@scoped(sma.READ_APIKEYS_STATUS)
def get_key_status(key_id: int) -> Any:
    db = get_db()
    row = _switchable_key(db, key_id)
    _audit("apikeys.status", target=key_id, status=200)
    return jsonify(_key_status(row))


@api_bp.post("/interact/apikeys/<int:key_id>/enable")
@scoped(sma.WRITE_APIKEYS_STATUS)
def enable_key(key_id: int) -> Any:
    return _set_key_enabled(key_id, True)


@api_bp.post("/interact/apikeys/<int:key_id>/disable")
@scoped(sma.WRITE_APIKEYS_STATUS)
def disable_key(key_id: int) -> Any:
    return _set_key_enabled(key_id, False)


def _set_key_enabled(key_id: int, enabled: bool) -> Any:
    db = get_db()
    row = _switchable_key(db, key_id)
    db.set_api_key_enabled(key_id, enabled)
    _audit(
        "apikeys.enable" if enabled else "apikeys.disable",
        target=key_id,
        status=200,
        detail=row["name"],
    )
    return jsonify({**_key_status(row), "enabled": enabled})


@api_bp.post("/interact/apikeys/<int:key_id>/delete")
@scoped(sma.EDIT_APIKEYS_OWNED)
def delete_owned_key(key_id: int) -> Any:
    db = get_db()
    row = _owned_key(db, key_id)
    for image in db.delete_api_key(key_id):
        images.unlink_image(image["id"], image["ext"])
    _audit("apikeys.delete", target=key_id, status=200, detail=row["name"])
    return jsonify({"deleted": key_id})


# -- MSQ ---------------------------------------------------------------------


def _msq_payload(row: Any) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    return data


def _msq_recorder(db: Any, recorder_id: int) -> Any:
    row = db.get_msq_recorder(recorder_id)
    if row is None:
        raise sma.SmaError("msq_not_found", "No such MSQ recorder.", 404)
    return row


def _msq_fields(db: Any, body: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if creating or "name" in body:
        fields["name"] = _str_field(body, "name", required=True)[:_MAX_NAME]
    if creating or "model" in body:
        fields["model"] = _str_field(body, "model", required=True)
    if creating or "route_id" in body:
        route_id = body.get("route_id")
        if route_id is None:
            fields["route_id"] = None
        else:
            try:
                route_id = int(route_id)
            except (TypeError, ValueError) as exc:
                raise sma.SmaError("invalid_body", "'route_id' must be an integer.") from exc
            if db.get_route(route_id) is None:
                raise sma.SmaError("route_not_found", "No such route.", 404)
            fields["route_id"] = route_id
    if "interval_seconds" in body:
        fields["interval_seconds"] = max(1, _int_field(body, "interval_seconds", 3600))
    for key in ("system_prompt", "user_prompt", "slop_list"):
        if key in body:
            fields[key] = _str_field(body, key)
    for key in ("enabled", "penalize_symbols", "collect_requests", "ignore_thinking"):
        if key in body:
            fields[key] = _bool_field(body, key, True)
    for key in ("max_ttft_ms", "max_total_ms"):
        if key in body:
            fields[key] = _int_field(body, key, -1)
    if "hardcore_json" in body:
        raw = body["hardcore_json"]
        fields["hardcore_json"] = (
            json.dumps(raw, sort_keys=True) if isinstance(raw, dict) else _str_field(body, "hardcore_json")
        )
    return fields


@api_bp.get("/interact/msq")
@scoped(sma.READ_MSQ)
def list_msq() -> Any:
    db = get_db()
    _audit("msq.list", status=200)
    return jsonify({"data": [_msq_payload(row) for row in db.list_msq_recorders()]})


@api_bp.get("/interact/msq/<int:recorder_id>")
@scoped(sma.READ_MSQ)
def get_msq(recorder_id: int) -> Any:
    db = get_db()
    _audit("msq.get", target=recorder_id, status=200)
    return jsonify(_msq_payload(_msq_recorder(db, recorder_id)))


@api_bp.post("/interact/msq/create")
@scoped(sma.WRITE_MSQ)
def create_msq() -> Any:
    db = get_db()
    body = _json_body()
    fields = _msq_fields(db, body, creating=True)
    recorder_id = db.create_msq_recorder(**fields)
    _audit("msq.create", target=recorder_id, status=201, detail=fields.get("name", ""))
    return jsonify(_msq_payload(_msq_recorder(db, recorder_id))), 201


@api_bp.post("/interact/msq/<int:recorder_id>/update")
@scoped(sma.EDIT_MSQ)
def update_msq(recorder_id: int) -> Any:
    db = get_db()
    _msq_recorder(db, recorder_id)
    fields = _msq_fields(db, _json_body(), creating=False)
    if not fields:
        raise sma.SmaError("invalid_body", "No editable recorder fields were supplied.")
    db.update_msq_recorder(recorder_id, **fields)
    _audit("msq.update", target=recorder_id, status=200)
    return jsonify(_msq_payload(_msq_recorder(db, recorder_id)))


@api_bp.post("/interact/msq/<int:recorder_id>/delete")
@scoped(sma.WRITE_MSQ)
def delete_msq(recorder_id: int) -> Any:
    db = get_db()
    row = _msq_recorder(db, recorder_id)
    db.delete_msq_recorder(recorder_id)
    _audit("msq.delete", target=recorder_id, status=200, detail=row["name"])
    return jsonify({"deleted": recorder_id})


@api_bp.post("/interact/msq/<int:recorder_id>/run")
@scoped(sma.WRITE_MSQ)
def run_msq(recorder_id: int) -> Any:
    db = get_db()
    recorder = _msq_recorder(db, recorder_id)
    last = parse_iso(recorder["last_run_at"])
    if last is not None and (utcnow() - last).total_seconds() < 30:
        raise sma.SmaError(
            "probe_throttled",
            "That recorder ran less than 30 seconds ago. Probes cost upstream tokens.",
            429,
        )
    result = run_recorder(current_app._get_current_object(), recorder)
    _audit("msq.run", target=recorder_id, status=200 if result.get("ok") else 502)
    return jsonify(result)


# -- consent page (browser) --------------------------------------------------


@consent_bp.before_request
def _consent_prepare() -> Any:
    db = get_db()
    if not db.sma_enabled(current_app.config["SMA_ENABLED"]):
        abort(404)
    if _transport_blocked(db):
        abort(
            403,
            description=(
                "The System Management Accounts API is only available over HTTPS "
                "on this MidWare server."
            ),
        )
    db.expire_system_accounts()
    return None


def _csrf_token() -> str:
    token = session.get(_CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 16:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token


def _csrf_ok(candidate: str) -> bool:
    expected = session.get(_CSRF_SESSION_KEY)
    return isinstance(expected, str) and bool(candidate) and secrets.compare_digest(
        expected, candidate
    )


def _consent_row(token: str) -> Any:
    db = get_db()
    row = db.get_system_account_by_token(sma.hash_token(token), "activation")
    if row is None:
        abort(410)
    if row["state"] not in {"pending", "approved"}:
        abort(410)
    expires = parse_iso(row["activation_expires_at"])
    if expires is None or expires <= utcnow():
        abort(410)
    return row


@consent_bp.get("/<token>")
@adminauth.admin_required
def consent(token: str) -> Any:
    if not adminauth.credentials_configured():
        abort(404)
    row = _consent_row(token)
    scopes = sma.parse_scopes(row["scopes"])
    frozen = _recomputed_hash(row)
    response = render_template(
        "sma_consent.html",
        token=token,
        account=row,
        scopes=scopes,
        scope_descriptions=sma.SCOPE_DESCRIPTIONS,
        redirect_host=urlsplit(row["redirect_uri"]).netloc,
        csrf=_csrf_token(),
        tampered=frozen != row["payload_hash"],
        transport=row["requested_transport"] or "unknown",
    )
    return _no_store(response)


def _recomputed_hash(row: Any) -> str:
    return sma.enrollment_payload_hash(
        {
            "name": row["name"],
            "public_key": row["public_key"],
            "redirect_uri": row["redirect_uri"],
            "scopes": sorted(sma.parse_scopes(row["scopes"])),
        }
    )


@consent_bp.post("/<token>")
@adminauth.admin_required
def consent_decide(token: str) -> Any:
    if not adminauth.credentials_configured():
        abort(404)
    if not _csrf_ok(request.form.get("csrf", "")):
        abort(400, description="Missing or invalid form token. Reload the page and try again.")
    row = _consent_row(token)
    db = get_db()

    if _recomputed_hash(row) != row["payload_hash"]:
        db.record_system_event(
            int(row["id"]), "consent.tampered", status=409, ip=_client_ip()
        )
        return _no_store(
            render_template("sma_result.html", state="error", account=row, token=token)
        ), 409

    decision = request.form.get("decision", "")
    if decision not in {"allow", "deny"}:
        abort(400, description="Choose Allow or Deny.")

    if decision == "allow":
        with db.write() as conn:
            cur = conn.execute(
                "UPDATE system_accounts SET state = 'approved', activated_at = ? "
                "WHERE id = ? AND state = 'pending'",
                (to_iso(utcnow()), int(row["id"])),
            )
        approved = cur.rowcount == 1
        db.record_system_event(
            int(row["id"]),
            "consent.allow" if approved else "consent.repeat",
            status=200 if approved else 409,
            ip=_client_ip(),
        )
        state = "approved" if approved else row["state"]
    else:
        with db.write() as conn:
            cur = conn.execute(
                "UPDATE system_accounts SET state = 'denied', revoked_at = ?, "
                "revoked_reason = 'denied', activation_token_hash = NULL, "
                "activation_expires_at = NULL "
                "WHERE id = ? AND state = 'pending'",
                (to_iso(utcnow()), int(row["id"])),
            )
        denied = cur.rowcount == 1
        db.record_system_event(
            int(row["id"]),
            "consent.deny" if denied else "consent.repeat",
            status=200 if denied else 409,
            ip=_client_ip(),
        )
        state = "denied" if denied else row["state"]

    session["sma_result"] = {"token": sma.hash_token(token), "state": state}
    return redirect(url_for("sma_consent.consent_result", token=token))


@consent_bp.get("/<token>/result")
def consent_result(token: str) -> Any:
    row = get_db().get_system_account_by_token(sma.hash_token(token), "activation")
    # The outcome lives in the session, not the query string: a forged
    # ?state=approved must not be able to fake having approved anything.
    stored = session.get("sma_result")
    state = "unknown"
    if isinstance(stored, dict) and stored.get("token") == sma.hash_token(token):
        state = stored.get("state", "unknown")
        session.pop("sma_result", None)
    if row is None:
        return _no_store(render_template("sma_result.html", state=state, account=None, token=token))
    return _no_store(
        render_template(
            "sma_result.html",
            state=state,
            account=row,
            token=token,
            redirect_uri=row["redirect_uri"],
        )
    )


def _no_store(body: str) -> Response:
    response = current_app.response_class(body, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    # The consent page is the one place a click decides a grant.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response
