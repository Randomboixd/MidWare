"""Upstream wire helpers shared by the proxy and the workflow engine.

Kept in their own module so ``workflow.py`` can build sub-request URLs and
filter hop-by-hop headers without importing the proxy blueprint (which imports
``workflow.py`` back — a cycle Flask would happily create at import time).
"""

from __future__ import annotations

import hashlib

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


def split_target(route) -> tuple[str, str]:
    """Split a configured target host into its base URL and path prefix."""
    target = (route["target_host"] or "https://api.openai.com").rstrip("/")
    if target.endswith("/v1"):
        return target[: -len("/v1")], "/v1"
    return target, ""


def upstream_url(route, path: str = "") -> str:
    """Full URL for ``path`` on a route, honouring a ``/v1``-suffixed host."""
    base, prefix = split_target(route)
    joined = f"{prefix}/{(path or '').lstrip('/')}"
    return f"{base}/{joined.lstrip('/')}"


# -- provider-required headers ------------------------------------------------

OPENCODE_GO = "opencode-go"
OPENCODE_ZEN = "opencode-zen"

# Both OpenCode gateways get the same treatment. Go *requires* the session
# header (it answers 400 MissingSessionID before it even checks the bearer);
# Zen does not, but its docs ask for one on every request because the same value
# is the routing and prompt-cache affinity key. Sending it to either is correct;
# only Go turns its absence into a failure.
OPENCODE_GATEWAYS = (OPENCODE_GO, OPENCODE_ZEN)

# A sign-in-obtained route is still reachable by harnesses that bring their own
# user agent; what must never happen is MidWare forwarding a bare HTTP library's
# default, which is exactly what OpenCode asks clients not to send.
_GENERIC_AGENTS = (
    "python-httpx",
    "python-requests",
    "python-urllib",
    "httpx/",
    "requests/",
    "werkzeug/",
    "curl/",
    "wget/",
    "node-fetch",
    "undici",
    "axios/",
    "go-http-client",
    "okhttp/",
    "postmanruntime",
    "java/",
)


def _header_value(headers: dict[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    for key in [key for key in headers if key.lower() == name.lower()]:
        del headers[key]
    headers[name] = value


def _client_agent() -> str:
    from . import __version__

    return f"MidWare/{__version__}"


def opencode_session_id(seed: str) -> str:
    """An opaque, stable routing id: the same seed always yields the same id.

    OpenCode wants one id per conversation. MidWare cannot see conversations —
    clients trim, compact and rewrite their history between turns — so the seed
    is deliberately made of things that outlive that: the caller's key, the
    route and the model. A value derived from messages would silently change
    mid-chat, costing both the session and its cache.
    """
    return "mw-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def apply_provider_headers(
    headers: dict[str, str], route, *, seed: str, model: str = ""
) -> None:
    """Add the headers a provider demands that its clients never send.

    OpenCode Go answers ``400 MissingSessionID`` *before* it checks the bearer,
    so a proxy in front of it cannot pass a client request through unchanged;
    the same header is also its routing and prompt-cache affinity key, which is
    why the value must stay put across a conversation. Zen does not require it
    but wants it for the same routing reason, so both gateways get it. Only
    tagged routes are touched, so nothing else gains headers by accident.
    """
    try:
        provider = route["provider"] or ""
    except (IndexError, KeyError):
        return
    if provider not in OPENCODE_GATEWAYS:
        return
    if not (_header_value(headers, "x-opencode-session") or "").strip():
        route_id = route["id"]
        _set_header(
            headers,
            "x-opencode-session",
            opencode_session_id(f"{seed}|{route_id}|{model}"),
        )
    agent = (_header_value(headers, "user-agent") or "").strip()
    if not agent or any(marker in agent.lower() for marker in _GENERIC_AGENTS):
        _set_header(headers, "user-agent", _client_agent())
