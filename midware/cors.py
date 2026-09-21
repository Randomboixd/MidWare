"""Per-key CORS for browser clients.

Each API key carries its own allow-origin list. A browser ``OPTIONS`` preflight
cannot identify the key (browsers do not attach the credential), so preflights
are answered permissively; the actual request is validated against the key's
list before it is forwarded, and a disallowed ``Origin`` is rejected outright.

Hand-rolled instead of pulling in ``flask-cors`` because a handful of headers
does not warrant a dependency.
"""

from __future__ import annotations

from flask import Flask, Response, g, request

CORS_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
DEFAULT_ALLOWED_HEADERS = "Authorization, Content-Type, X-Api-Key, X-Midware-Key"


def _is_proxy_path() -> bool:
    path = request.path or ""
    return (
        path == "/v1"
        or path.startswith("/v1/")
        or path == "/proxy"
        or path.startswith("/proxy/")
    )


def origin_allowed(configured: str | None, origin: str | None) -> bool:
    """Whether ``origin`` may use a key configured with ``configured``.

    A missing ``Origin`` means a non-browser client and is always allowed.
    ``*`` allows every origin; otherwise the value is a comma-separated
    allowlist. An empty setting allows no browser origin.
    """
    if not origin:
        return True
    configured = (configured or "").strip()
    if not configured:
        return False
    if configured == "*":
        return True
    allowed = [part.strip() for part in configured.split(",") if part.strip()]
    return origin in allowed


def check_origin(configured: str | None) -> tuple[dict, int] | None:
    """Reject a proxied request whose browser ``Origin`` the key does not allow."""
    origin = request.headers.get("Origin")
    if origin_allowed(configured, origin):
        return None
    return (
        {
            "error": {
                "message": f"Origin '{origin}' is not allowed for this API key.",
                "type": "midware_permission_error",
                "code": "origin_not_allowed",
            }
        },
        403,
    )


def _apply(response: Response, origin: str) -> None:
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = CORS_METHODS
    requested = request.headers.get("Access-Control-Request-Headers")
    response.headers["Access-Control-Allow-Headers"] = requested or DEFAULT_ALLOWED_HEADERS
    if origin != "*":
        vary = response.headers.get("Vary")
        response.headers["Vary"] = f"{vary}, Origin" if vary else "Origin"


def _resolve_origin(configured: str, request_origin: str | None) -> str | None:
    """Pick the ``Access-Control-Allow-Origin`` value, or ``None`` to omit CORS.

    ``*`` allows everything; otherwise the value is a comma-separated allowlist
    and the caller's ``Origin`` is echoed only when it matches.
    """
    if not configured:
        return None
    if configured == "*":
        return "*"
    allowed = [part.strip() for part in configured.split(",") if part.strip()]
    if request_origin and request_origin in allowed:
        return request_origin
    if not request_origin and len(allowed) == 1:
        return allowed[0]
    return None


def register_cors(app: Flask) -> None:
    @app.after_request
    def _cors_headers(response: Response) -> Response:
        if not _is_proxy_path():
            return response
        if request.method == "OPTIONS":
            origin = request.headers.get("Origin")
            if origin:
                _apply(response, origin)
            return response
        configured = g.get("midware_cors_origin")
        if configured is None:
            return response
        origin = _resolve_origin(configured, request.headers.get("Origin"))
        if origin is not None:
            _apply(response, origin)
        return response
