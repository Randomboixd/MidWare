"""Minimal CORS support for browser clients.

Enabled by default (``*``) and configurable from the control panel, so the proxy
can be called from a web UI without a redeploy. Hand-rolled instead of pulling in
``flask-cors`` because a handful of headers does not warrant a dependency.
"""

from __future__ import annotations

from flask import Flask, Response, current_app, request

CORS_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
DEFAULT_ALLOWED_HEADERS = "Authorization, Content-Type, X-Api-Key, X-Midware-Key"


def _configured_origin() -> str:
    from .auth import get_db

    default = current_app.config.get("CORS_ALLOW_ORIGIN", "*")
    try:
        return get_db().cors_allow_origin(default).strip()
    except Exception:  # a DB hiccup must not break the response
        return (default or "").strip()


def _resolve_origin(configured: str) -> str | None:
    """Pick the ``Access-Control-Allow-Origin`` value, or ``None`` to omit CORS.

    ``*`` allows everything; otherwise the value is a comma-separated allowlist
    and the caller's ``Origin`` is echoed only when it matches. An empty setting
    disables CORS entirely.
    """
    if not configured:
        return None
    if configured == "*":
        return "*"
    allowed = [part.strip() for part in configured.split(",") if part.strip()]
    request_origin = request.headers.get("Origin")
    if request_origin and request_origin in allowed:
        return request_origin
    if not request_origin and len(allowed) == 1:
        return allowed[0]
    return None


def register_cors(app: Flask) -> None:
    @app.after_request
    def _cors_headers(response: Response) -> Response:
        origin = _resolve_origin(_configured_origin())
        if origin is None:
            return response
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = CORS_METHODS
        requested = request.headers.get("Access-Control-Request-Headers")
        response.headers["Access-Control-Allow-Headers"] = requested or DEFAULT_ALLOWED_HEADERS
        if origin != "*":
            vary = response.headers.get("Vary")
            response.headers["Vary"] = f"{vary}, Origin" if vary else "Origin"
        return response
