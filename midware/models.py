"""Fetch and cache the model catalogue of every upstream route.

MidWare keeps one deduplicated list of model slugs. The default route's models
are emitted bare (``gpt-4o``); every other route's models are prefixed with the
route name (``[NanoGPT]text-model``) so a client can copy a slug straight out of
``/v1/models`` and get routed back to the right host.

Routes that share a ``target_host`` are only ever queried once per refresh, and
the fetched payload is applied to every route pointing at that host.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Iterable

_MODEL_LIST_KEYS = ("data", "models", "result", "results", "items")
_ID_KEYS = ("id", "model", "model_id", "name", "slug")


def extract_model_ids(payload: Any) -> list[str]:
    """Pull model identifiers out of the many shapes providers return.

    Handles OpenAI (``{"data": [{"id": ...}]}``), Anthropic (``{"data": [...]}``),
    Ollama (``{"models": [{"name": ...}]}``) and bare ``["a", "b"]`` arrays.
    """
    if payload is None:
        return []

    items: Iterable[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = []
        for key in _MODEL_LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            items = [payload]
    else:
        return []

    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = ""
            for key in _ID_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
        else:
            continue
        if text and text not in ids:
            ids.append(text)
    return ids


def _models_url(target_host: str) -> str:
    base = (target_host or "").rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/models"
    return f"{base}/v1/models"


def normalize_host(target_host: str | None) -> str:
    """Canonical key for a target host: trailing slash and ``/v1`` suffix ignored.

    ``https://api.example.com`` and ``https://api.example.com/v1/`` are the same
    upstream, so they must only ever be fetched once per refresh.
    """
    base = (target_host or "").strip().rstrip("/").lower()
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return base


def _headers(route) -> dict[str, str]:
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    key = (route["upstream_key"] or "").strip()
    if key:
        if "anthropic" in (route["target_host"] or "").lower():
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def fetch_route_models(client, route) -> tuple[list[str], str | None]:
    """Return ``(model_ids, error)`` for a single route."""
    import httpx

    url = _models_url(route["target_host"])
    try:
        response = client.get(url, headers=_headers(route))
    except httpx.HTTPError as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if response.status_code >= 400:
        return [], f"HTTP {response.status_code} from {url}"
    try:
        payload = response.json()
    except ValueError as exc:
        return [], f"Invalid JSON from {url}: {exc}"
    return extract_model_ids(payload), None


def probe_route(client, name: str, target_host: str, upstream_key: str) -> dict[str, Any]:
    """Try fetching a prospective route's models before it is saved.

    Returns ``{"ok": bool, "error": str | None, "models": list[str], "url": str}``.
    A reachable host that answers with something other than a model list (a
    plain HTML page, a non-JSON body) still counts as reachable — we only fail
    when the network call itself fails or the host rejects our credential.
    """
    route = {"name": name, "target_host": target_host, "upstream_key": upstream_key}
    url = _models_url(target_host)
    model_ids, error = fetch_route_models(client, route)
    if error and "Invalid JSON" in error:
        return {"ok": True, "error": None, "models": [], "url": url, "warning": error}
    if error:
        return {"ok": False, "error": error, "models": [], "url": url}
    return {"ok": True, "error": None, "models": model_ids, "url": url}


def refresh_all(app) -> dict[str, Any]:
    """Refresh every route's models, querying each distinct host only once."""
    db = app.extensions["midware_db"]
    client = app.extensions["midware_http"]

    routes = db.list_routes()
    by_host: dict[str, list[Any]] = {}
    for route in routes:
        by_host.setdefault(normalize_host(route["target_host"]), []).append(route)

    results: list[dict[str, Any]] = []
    for group in by_host.values():
        representative = group[0]
        model_ids, error = fetch_route_models(client, representative)
        for route in group:
            if error:
                results.append({"route_id": route["id"], "route_name": route["name"], "count": 0, "error": error})
                continue
            count = db.replace_models(route["id"], model_ids)
            results.append({"route_id": route["id"], "route_name": route["name"], "count": count, "error": None})

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if results and all(result["error"] for result in results):
        db.set_meta("models_refresh_error", results[0]["error"])
    else:
        db.set_meta("models_refresh_error", "")
        db.set_meta("models_refreshed_at", stamp)

    return {
        "at": stamp,
        "total": db.count_models(),
        "routes": results,
        "errors": [result for result in results if result["error"]],
    }


def refresh_in_background(app) -> None:
    def run() -> None:
        with app.app_context():
            try:
                refresh_all(app)
            except Exception as exc:  # never let a background refresh crash the server
                app.logger.warning("MidWare model refresh failed: %s", exc)

    threading.Thread(target=run, name="midware-model-refresh", daemon=True).start()


def refresh_if_stale(app, max_age_hours: float = 24.0) -> bool:
    """Kick off a background refresh when the catalogue is older than ``max_age_hours``."""
    db = app.extensions["midware_db"]
    if not db.list_routes():
        return False
    raw = db.get_meta("models_refreshed_at")
    if not raw:
        stale = True
    else:
        try:
            seen = datetime.fromisoformat(raw)
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            stale = (datetime.now(timezone.utc) - seen).total_seconds() >= max_age_hours * 3600
        except ValueError:
            stale = True
    if not stale:
        return False
    refresh_in_background(app)
    return True
