"""Upstream wire helpers shared by the proxy and the workflow engine.

Kept in their own module so ``workflow.py`` can build sub-request URLs and
filter hop-by-hop headers without importing the proxy blueprint (which imports
``workflow.py`` back — a cycle Flask would happily create at import time).
"""

from __future__ import annotations

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
