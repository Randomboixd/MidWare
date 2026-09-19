"""The middleware itself: authenticate, forward, measure, persist, stream back.

The response is streamed to the client *and* teed into a buffer so token usage can
be parsed after the fact without ever buffering the whole response in memory.
"""

from __future__ import annotations

import json
import re
import time
from typing import Iterator

import httpx
from flask import Blueprint, Response, current_app, request, stream_with_context

from ..auth import authenticate, get_db
from ..usage import (
    model_from_request_body,
    parse_sse_events,
    truncate,
    usage_from_mapping,
    usage_from_response,
    usage_from_stream,
)

bp = Blueprint("proxy", __name__)

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

HOST_PREFIX_RE = re.compile(r"^\s*\[(?P<name>[^\]#]+)?(?:#(?P<id>\d+))?\]\s*")

_DEFAULT_CAPTURE_LIMIT = 8 * 1024 * 1024


def _http_client() -> httpx.Client:
    return current_app.extensions["midware_http"]


def _capture_limit() -> int:
    return int(current_app.config.get("MAX_CAPTURE_BYTES", _DEFAULT_CAPTURE_LIMIT))


def _split_target(route) -> tuple[str, str]:
    """Split a configured target host into its base URL and path prefix."""
    target = (route["target_host"] or "https://api.openai.com").rstrip("/")
    if target.endswith("/v1"):
        return target[: -len("/v1")], "/v1"
    return target, ""


def _host_label(name: str) -> str:
    """Turn a route name into the short tag clients type as ``[Name]``."""
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    return cleaned[:32]


def _route_labels(route) -> list[str]:
    labels = []
    for candidate in (_host_label(route["name"]), route["name"]):
        if candidate and candidate not in labels:
            labels.append(candidate)
    return labels


def _resolve_target(db, name: str, explicit_route_id: int | None = None):
    """Find the route addressed by an in-model ``[Name]`` prefix.

    A prefix never drags traffic to an inactive route: the active route wins on a
    name collision, and an unknown name falls back to the default route.
    """
    wanted = name.strip().casefold()
    active = db.active_route()

    if explicit_route_id is not None:
        route = db.get_route(explicit_route_id)
        if route is not None:
            return route

    if active is not None:
        for label in _route_labels(active):
            if label.casefold() == wanted:
                return active

    for route in db.list_routes():
        for label in _route_labels(route):
            if label.casefold() == wanted:
                return route

    return None


def _extract_host_prefix(payload: dict | None) -> tuple[str | None, int | None]:
    """Parse ``[Host]`` / ``[Host#42]`` off a request's ``model`` field."""
    if not isinstance(payload, dict):
        return None, None
    model = payload.get("model")
    if not isinstance(model, str):
        return None, None

    match = HOST_PREFIX_RE.match(model)
    if not match:
        return None, None

    name = (match.group("name") or "").strip()
    route_id = int(match.group("id")) if match.group("id") else None
    if not name and route_id is None:
        return None, None
    return name or None, route_id


def _rewrite_model(raw_body: bytes, payload: dict | None) -> bytes:
    """Strip the host prefix from ``model`` before forwarding upstream."""
    if not isinstance(payload, dict):
        return raw_body
    model = payload.get("model")
    if not isinstance(model, str):
        return raw_body
    new_model = HOST_PREFIX_RE.sub("", model, count=1).lstrip()
    if new_model == model or not new_model:
        return raw_body
    payload["model"] = new_model
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _filtered_headers(headers, drop: set[str] | None = None):
    drop = drop or set()
    for name, value in headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP or lower in drop:
            continue
        yield name, value


def _prepare_headers(route, force_sse: bool) -> dict[str, str]:
    """Build upstream headers, swapping the client's MidWare key for the route key.

    The route's stored credential wins. If a route was saved without one we fall
    back to whatever the caller sent so the server can still act as a pass-through.
    """
    client_auth = request.headers.get("Authorization")
    client_api_key = request.headers.get("x-api-key")

    headers = dict(
        _filtered_headers(request.headers, drop={"authorization", "x-api-key", "accept-encoding"})
    )
    headers.setdefault("Accept-Encoding", "identity")

    upstream_key = (route["upstream_key"] or "").strip()
    is_anthropic = "anthropic" in (route["target_host"] or "").lower()

    if upstream_key:
        if is_anthropic:
            headers["x-api-key"] = upstream_key
        else:
            headers["Authorization"] = f"Bearer {upstream_key}"
    elif client_auth:
        headers["Authorization"] = client_auth
    elif client_api_key:
        headers["x-api-key"] = client_api_key

    if force_sse:
        headers["Accept"] = "text/event-stream"
    elif not headers.get("Accept"):
        headers["Accept"] = "application/json"
    return headers


def _stream_requested(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("stream") is True:
        return True
    if isinstance(payload.get("stream"), str):
        return payload["stream"].strip().lower() in {"true", "1", "yes"}
    return False


def _server_header() -> tuple[str, str]:
    return ("Server", "MidWare")


def _json_error(message: str, status: int, code: str, err_type: str = "midware_error") -> Response:
    body = {"error": {"message": message, "type": err_type, "code": code}}
    return Response(json.dumps(body), status=status, mimetype="application/json")


def _upstream_error_status(response: httpx.Response) -> int:
    return response.status_code


def _persist(
    *,
    ctx: dict,
    path: str,
    model: str | None,
    status_code: int,
    latency_ms: int,
    streamed: bool,
    request_body: str | None,
    response_body: str | None,
    error: str | None = None,
    model_raw: str | None = None,
    host_prefix: str | None = None,
) -> None:
    db = get_db()
    api_key = ctx["api_key"]
    route = ctx["route"]

    capture_limit = _capture_limit()
    request_body = truncate(request_body, capture_limit)
    response_body = truncate(response_body, capture_limit)

    usage = None
    if streamed:
        events = parse_sse_events(response_body)
        for event in events:
            if model is None and isinstance(event.get("model"), str):
                model = event["model"] or None
            if isinstance(event.get("message"), dict) and isinstance(event["message"].get("model"), str):
                model = event["message"]["model"] or model
        usage = usage_from_stream(response_body)
    else:
        usage = usage_from_response(response_body)

    if usage is not None:
        model = model or usage.model
        prompt = usage.prompt_tokens
        completion = usage.completion_tokens
        total = usage.total_tokens
    else:
        prompt = completion = total = 0

    upstream_request_id = None
    if response_body:
        try:
            decoded = json.loads(response_body)
            if isinstance(decoded, dict):
                upstream_request_id = decoded.get("id")
        except (ValueError, TypeError):
            upstream_request_id = None

    db.record_request(
        api_key_id=api_key["id"],
        route_id=route["id"],
        method="POST",
        request_path=path,
        model=model,
        status_code=status_code,
        latency_ms=latency_ms,
        streamed=streamed,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        upstream_request_id=upstream_request_id,
        error=error,
        request_body=request_body,
        response_body=response_body,
        model_raw=model_raw,
        host_prefix=host_prefix,
    )
    db.touch_api_key(api_key["id"])
    db.prune_requests(api_key["id"])


def _forward(ctx: dict, path: str) -> Response:
    route = ctx["route"]
    db = get_db()

    raw_body = request.get_data() or b""
    try:
        parsed_body = json.loads(raw_body) if raw_body else None
    except (ValueError, TypeError):
        parsed_body = None

    model_raw = model_from_request_body(raw_body)
    model = model_raw
    host_prefix = None

    prefix_name, prefix_route_id = _extract_host_prefix(parsed_body)
    if prefix_name or prefix_route_id is not None:
        target = _resolve_target(db, prefix_name or "", prefix_route_id)
        if target is None:
            return _json_error(
                f"Unknown host prefix '[{prefix_name or prefix_route_id}]'."
                f" Known hosts: {', '.join('[%s]' % _host_label(r['name']) for r in db.list_routes()) or 'none'}.",
                404,
                "unknown_host",
                "midware_routing_error",
            )
        route = target
        host_prefix = _host_label(target["name"])
        raw_body = _rewrite_model(raw_body, parsed_body)
        model = HOST_PREFIX_RE.sub("", model_raw).lstrip() or None

    base_url, prefix = _split_target(route)
    upstream_path = f"{prefix}/{(path or '').lstrip('/')}"

    streamed = _stream_requested(parsed_body)
    headers = _prepare_headers(route, force_sse=streamed)

    url = f"{base_url}/{upstream_path.lstrip('/')}"
    if request.query_string:
        url = f"{url}?{request.query_string.decode('utf-8', 'replace')}"

    client = _http_client()
    started = time.perf_counter()

    def persist(**kwargs):
        _persist(
            ctx={"api_key": ctx["api_key"], "route": route},
            path=path,
            request_body=raw_body.decode("utf-8", "replace"),
            model_raw=model_raw,
            host_prefix=host_prefix,
            **kwargs,
        )

    try:
        upstream = client.send(
            client.build_request("POST", url, content=raw_body, headers=headers),
            stream=streamed,
        )
    except httpx.HTTPError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        persist(
            model=model,
            status_code=502,
            latency_ms=latency_ms,
            streamed=streamed,
            response_body=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        return _json_error(
            f"Upstream request failed: {exc}",
            502,
            "upstream_error",
            "midware_upstream_error",
        )

    response_headers = list(_filtered_headers(upstream.headers))
    response_headers.append(_server_header())
    content_type = upstream.headers.get("content-type", "application/json")

    if not streamed:
        body = upstream.content
        upstream.close()
        latency_ms = int((time.perf_counter() - started) * 1000)
        persist(
            model=model,
            status_code=upstream.status_code,
            latency_ms=latency_ms,
            streamed=False,
            response_body=body.decode("utf-8", "replace") if body else None,
        )
        return Response(body, status=upstream.status_code, headers=dict(response_headers), content_type=content_type)

    capture_limit = _capture_limit()

    def generate() -> Iterator[bytes]:
        buffer = bytearray()
        try:
            for chunk in upstream.iter_bytes():
                if chunk:
                    if len(buffer) < capture_limit:
                        buffer.extend(chunk[: capture_limit - len(buffer)])
                    yield chunk
        except httpx.HTTPError as exc:  # client dropped or upstream reset
            upstream.close()
            latency_ms = int((time.perf_counter() - started) * 1000)
            persist(
                model=model,
                status_code=upstream.status_code,
                latency_ms=latency_ms,
                streamed=True,
                response_body=bytes(buffer).decode("utf-8", "replace"),
                error=f"{type(exc).__name__}: {exc}",
            )
            return
        finally:
            upstream.close()

        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            persist(
                model=model,
                status_code=upstream.status_code,
                latency_ms=latency_ms,
                streamed=True,
                response_body=bytes(buffer).decode("utf-8", "replace"),
            )
        except Exception as exc:  # never let bookkeeping break a delivered stream
            current_app.logger.warning("MidWare failed to record streamed request: %s", exc)

    return Response(
        stream_with_context(generate()),
        status=upstream.status_code,
        headers=dict(response_headers),
        content_type=content_type,
        direct_passthrough=True,
    )


@bp.route("/v1/<path:subpath>", methods=["POST", "GET", "PUT", "PATCH", "DELETE"])
@bp.route("/v1", methods=["POST", "GET", "PUT", "PATCH", "DELETE"])
def proxy_v1(subpath: str = ""):
    ctx, error = authenticate()
    if error:
        payload, status = error
        return Response(json.dumps(payload), status=status, mimetype="application/json")
    if request.method != "POST":
        return _json_error("Only POST requests are proxied.", 405, "method_not_allowed")
    return _forward(ctx, subpath)


@bp.route("/proxy/<path:subpath>", methods=["POST", "GET", "PUT", "PATCH", "DELETE"])
def proxy_explicit(subpath: str):
    """Explicit alias for hosts that already include ``/v1`` in their base URL."""
    ctx, error = authenticate()
    if error:
        payload, status = error
        return Response(json.dumps(payload), status=status, mimetype="application/json")
    if request.method != "POST":
        return _json_error("Only POST requests are proxied.", 405, "method_not_allowed")
    return _forward(ctx, subpath)
