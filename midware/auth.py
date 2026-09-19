"""Client authentication for the proxy surface.

MidWare accepts the token clients already send: ``Authorization: Bearer <token>``
(OpenAI style) or ``x-api-key: <token>`` (Anthropic style). The token is looked up
in the ``api_keys`` table; the *upstream* credential is injected by the route, so
callers never see it.
"""

from __future__ import annotations

from flask import current_app, request

from .db import Database


def extract_client_token(headers=None) -> str | None:
    if headers is None:
        headers = request.headers
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth:
        scheme, _, value = auth.partition(" ")
        if value and scheme.lower() == "bearer":
            return value.strip()
        if not value and scheme:
            return scheme.strip()

    for header in ("x-api-key", "X-Api-Key", "x-midware-key"):
        value = headers.get(header)
        if value:
            return value.strip()

    return None


def get_db() -> Database:
    return current_app.extensions["midware_db"]


def authenticate() -> tuple[dict | None, tuple | None]:
    """Resolve the caller.

    Returns ``(context, error)`` where ``context`` holds the matched key row and
    token, or ``error`` is a ``(payload, status)`` tuple to return verbatim.
    """
    token = extract_client_token()
    if not token:
        return None, (
            {
                "error": {
                    "message": "Missing credentials. Send your MidWare key as "
                    "'Authorization: Bearer <key>' or 'x-api-key: <key>'.",
                    "type": "midware_authentication_error",
                    "code": "missing_api_key",
                }
            },
            401,
        )


    row = get_db().resolve_api_key(token)
    if row is None:
        return None, (
            {
                "error": {
                    "message": "Invalid MidWare API key.",
                    "type": "midware_authentication_error",
                    "code": "invalid_api_key",
                }
            },
            401,
        )

    route = get_db().active_route()
    if route is None:
        return None, (
            {
                "error": {
                    "message": "MidWare has no upstream configured yet. Finish setup first.",
                    "type": "midware_configuration_error",
                    "code": "no_route",
                }
            },
            503,
        )

    return {"api_key": row, "route": route, "token": token}, None
