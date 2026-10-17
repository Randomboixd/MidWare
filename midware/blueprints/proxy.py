"""The middleware itself: authenticate, forward, measure, persist, stream back.

The response is streamed to the client *and* teed into a buffer so token usage can
be parsed after the fact without ever buffering the whole response in memory.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Iterator

import httpx
from flask import Blueprint, Response, abort, current_app, g, request, stream_with_context

from ..auth import authenticate, get_db
from ..cors import check_origin
from ..db import (
    REQUEST_LIMIT_WINDOWS,
    TOKEN_LIMIT_WINDOWS,
    model_key,
    parse_id_list,
    parse_model_list,
    rate_window_end,
    utcnow,
)
from ..images import IMAGE_CONTENT_TYPES, load_image, rewrite_image_body
from ..premodels import apply_premodel_messages, apply_sampling_params, extract_premodel_prefix
from ..tokens import estimate_usage, prompt_estimate
from ..upstream import HOP_BY_HOP, split_target, upstream_url
from ..usage import (
    model_from_request_body,
    parse_sse_events,
    truncate,
    usage_from_mapping,
    usage_from_response,
    usage_from_stream,
)
from ..workflow import WorkflowError, WorkflowNodeError, load_workflow, run_workflow

bp = Blueprint("proxy", __name__)

HOST_PREFIX_RE = re.compile(r"^\s*\[(?P<name>[^\]#]+)?(?:#(?P<id>\d+))?\]\s*")
IMAGE_PATH_RE = re.compile(r"^images/(?:generations|edits|variations)$")

_DEFAULT_CAPTURE_LIMIT = 8 * 1024 * 1024


def _http_client() -> httpx.Client:
    return current_app.extensions["midware_http"]


def _capture_limit() -> int:
    return int(current_app.config.get("MAX_CAPTURE_BYTES", _DEFAULT_CAPTURE_LIMIT))


def _key_limits(api_key) -> dict:
    """Parse an API key's provider/premodel restrictions into lookup-friendly sets."""
    return {
        "allow_premodels": bool(api_key["allow_premodels"]),
        "restrict_premodels": bool(api_key["restrict_premodels"]),
        "allowed_premodels": parse_id_list(api_key["allowed_premodel_ids"]),
        "restrict_providers": bool(api_key["restrict_providers"]),
        "allowed_routes": parse_id_list(api_key["allowed_route_ids"]),
        "restrict_models": bool(api_key["restrict_models"]),
        "allowed_models": parse_model_list(api_key["allowed_model_ids"]),
    }


def _route_allowed(limits: dict, route_id: int | None) -> bool:
    if not limits["restrict_providers"]:
        return True
    return route_id in limits["allowed_routes"]


def _model_allowed(limits: dict, route_id: int | None, model: str | None) -> bool:
    if not limits["restrict_models"]:
        return True
    if route_id is None or not model:
        return False
    return model_key(route_id, model) in limits["allowed_models"]


def _premodel_allowed(limits: dict, premodel) -> bool:
    if not limits["allow_premodels"]:
        return False
    if not limits["restrict_premodels"]:
        return True
    return premodel["id"] in limits["allowed_premodels"]


def _permission_error(message: str, code: str) -> Response:
    return _json_error(message, 403, code, "midware_permission_error")


def _enabled_token_windows(api_key) -> tuple[str, ...]:
    """Token windows with the master switch on, a child ticked, and a real cap."""
    if not api_key["limit_tokens_enabled"]:
        return ()
    return tuple(
        window
        for window in TOKEN_LIMIT_WINDOWS
        if api_key[f"limit_tokens_{window}"] and int(api_key[f"max_tokens_{window}"] or 0) > 0
    )


def _enabled_request_limits(api_key) -> dict[str, int]:
    """Request windows in force as {window: max}; {} when nothing is limited."""
    if not api_key["limit_requests_enabled"]:
        return {}
    return {
        window: int(api_key[f"max_requests_{window}"] or 0)
        for window in REQUEST_LIMIT_WINDOWS
        if api_key[f"limit_requests_{window}"] and int(api_key[f"max_requests_{window}"] or 0) > 0
    }


def _rate_limit_error(window: str, message: str, code: str) -> Response:
    response = _json_error(message, 429, code, "midware_rate_limit_error")
    seconds = max(1, int((rate_window_end(window) - utcnow()).total_seconds()))
    response.headers["Retry-After"] = str(seconds)
    return response


def _rate_limit_check(db, subject, subject_id: int, label: str, *, amounts, claim) -> tuple[Response, str] | None:
    """Reject a call that would push ``subject`` (a key or premodel row) past quota.

    Request counters are claimed atomically (all windows or none) so a rejected
    attempt never eats quota; token counters are settled after the response in
    ``_persist`` because only the upstream knows the final usage. ``amounts`` /
    ``claim`` are the matching db helpers — key and premodel counters live in
    separate tables and are checked independently (key first).
    """
    token_windows = _enabled_token_windows(subject)
    if token_windows:
        counts = amounts(subject_id, "tokens", token_windows)
        for window in token_windows:
            limit = int(subject[f"max_tokens_{window}"] or 0)
            if counts[window] >= limit:
                message = (
                    f"Token limit reached for this {label}: {counts[window]}/{limit} "
                    f"tokens used this {window}."
                )
                return _rate_limit_error(window, message, "token_limit_exceeded"), "token_limit_exceeded"

    request_limits = _enabled_request_limits(subject)
    if request_limits:
        counts, ok = claim(subject_id, request_limits)
        if not ok:
            window = next(w for w in request_limits if counts[w] >= request_limits[w])
            message = (
                f"Request limit reached for this {label}: {counts[window]}/"
                f"{request_limits[window]} requests sent this {window}."
            )
            return (
                _rate_limit_error(window, message, "request_limit_exceeded"),
                "request_limit_exceeded",
            )
    return None


def _authorize(ctx: dict) -> tuple[dict, int] | None:
    """Apply the key's CORS policy and stash it for the after-request hook."""
    configured = ctx["api_key"]["cors_allow_origin"]
    g.midware_cors_origin = configured
    return check_origin(configured)


def _host_label(name: str) -> str:
    """Turn a route name into the short tag clients type as ``[Name]``."""
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    return cleaned[:32]


def _premodel_prefix(db) -> str:
    """The live premodel addressing prefix (settings override, config default)."""
    return db.premodel_prefix(current_app.config["DEFAULT_PREMODEL_PREFIX"])


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


def _filtered_headers(headers, drop: set[str] | None = None):
    drop = drop or set()
    for name, value in headers.items():
        lower = name.lower()
        if lower in HOP_BY_HOP or lower in drop:
            continue
        yield name, value


def _merge_reasoning_body(body: bytes | None, reasoning: str) -> bytes:
    """Prepend workflow node logs to an upstream message's reasoning fields."""
    if not body or not reasoning:
        return body
    try:
        decoded = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(decoded, dict):
        return body
    choices = decoded.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return body
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return body
    existing = message.get("reasoning_content")
    if not isinstance(existing, str):
        existing = message.get("reasoning") if isinstance(message.get("reasoning"), str) else ""
    merged = reasoning + ("\n\n" + existing if existing else "")
    message["reasoning_content"] = merged
    if isinstance(message.get("reasoning"), str):
        message["reasoning"] = merged
    return json.dumps(decoded, ensure_ascii=False).encode("utf-8")


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


def _endpoint_family(path: str) -> str:
    """Which OpenAI API family a proxied path belongs to: chat/image/embedding."""
    cleaned = (path or "").strip("/").lower()
    if IMAGE_PATH_RE.match(cleaned):
        return "image"
    if cleaned == "embeddings":
        return "embedding"
    return "chat"


def _local_estimate(request_body, response_body, model: str | None, family: str):
    """Local token fallback when the upstream sent no usable ``usage``.

    Image generations answer with URLs/base64 blobs and embeddings with float
    vectors, so only their request side is ever counted — and for multipart
    image edits the raw body also holds uploaded image bytes that would make a
    whole-body estimate meaningless.
    """
    if family != "chat":
        return prompt_estimate(request_body, model)
    return estimate_usage(request_body, response_body, model)


def _multipart_model_body() -> dict | None:
    """Stand-in body exposing the multipart ``model`` field to the router."""
    try:
        model = request.form.get("model")
    except Exception:  # malformed or oversized form: nothing to route on
        return None
    return {"model": model} if isinstance(model, str) and model else None


def _disposition_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")


def _rebuild_multipart(model: str) -> tuple[bytes, str]:
    """Re-serialize the parsed multipart body with ``model`` substituted.

    Only reached when routing rewrote the model field (a ``[Host]`` prefix);
    every other part — including the uploaded image — is copied through.
    Returns ``(body, content_type)`` with a fresh boundary.
    """
    boundary = secrets.token_hex(16)
    chunks: list[bytes] = []
    saw_model = False
    for name, value in request.form.items(multi=True):
        if name == "model":
            name, value, saw_model = "model", model, True
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{_disposition_value(name)}"\r\n'
                f"\r\n{value}\r\n"
            ).encode("utf-8", "replace")
        )
    if not saw_model and model:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="model"\r\n\r\n{model}\r\n'
            ).encode("utf-8")
        )
    for name, storage in request.files.items(multi=True):
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{_disposition_value(name)}"; '
            f'filename="{_disposition_value(storage.filename or name)}"\r\n'
            f"Content-Type: {storage.content_type or 'application/octet-stream'}\r\n"
            f"\r\n"
        ).encode("utf-8")
        chunks.append(header + storage.read() + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _models_catalogue(ctx: dict) -> Response:
    """Serve MidWare's own model list instead of proxying the upstream's.

    The default route's models are bare; every other route's are prefixed with
    ``[Name]`` so the slug can be copied straight into a request. The catalogue
    is filtered to what the calling key is actually allowed to reach.
    """
    db = get_db()
    limits = _key_limits(ctx["api_key"])
    entries = db.list_models()
    data = [
        {
            "id": entry["slug"],
            "object": "model",
            "created": 0,
            "owned_by": entry["route_name"],
            "midware": {
                "model": entry["model_id"],
                "route_id": entry["route_id"],
                "route": entry["route_name"],
                "default": entry["is_default"],
            },
        }
        for entry in entries
        if _route_allowed(limits, entry["route_id"])
        and _model_allowed(limits, entry["route_id"], entry["model_id"])
    ]

    routes = {route["id"]: route for route in db.list_routes()}
    active = db.active_route()
    active_id = active["id"] if active else None
    prefix = _premodel_prefix(db)
    for premodel in db.list_premodels():
        if not _premodel_allowed(limits, premodel):
            continue
        if not _route_allowed(limits, premodel["route_id"]):
            continue
        effective_route_id = premodel["route_id"] or active_id
        if not _model_allowed(limits, effective_route_id, premodel["model"]):
            continue
        route = routes.get(premodel["route_id"])
        data.append(
            {
                "id": f"{prefix}{premodel['slug']}",
                "object": "model",
                "created": 0,
                "owned_by": route["name"] if route else "premodel",
                "midware": {
                    "premodel": premodel["slug"],
                    "model": premodel["model"],
                    "route_id": premodel["route_id"],
                    "route": route["name"] if route else None,
                    "description": premodel["description"],
                },
            }
        )

    payload = {"object": "list", "data": data}
    return Response(json.dumps(payload, ensure_ascii=False), mimetype="application/json")


def _json_error(message: str, status: int, code: str, err_type: str = "midware_error") -> Response:
    body = {"error": {"message": message, "type": err_type, "code": code}}
    return Response(json.dumps(body), status=status, mimetype="application/json")


def _maybe_fetch_models_after_upstream_error(status_code: int) -> None:
    """Re-fetch the catalogue when the upstream reports our stored key is bad.

    A brand-new route added without a connection test (or one whose key rotated)
    rejects the first proxied call with 401/403. That is exactly the moment to
    pull ``/v1/models`` again so the Routes page shows a real error and the
    catalogue self-heals once the key is fixed.
    """
    if status_code not in (401, 403):
        return
    try:
        db = get_db()
        if not db.list_routes():
            return
        from ..models import refresh_in_background

        refresh_in_background(current_app._get_current_object())
    except Exception:
        current_app.logger.debug("post-401 model refresh skipped", exc_info=True)


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
    workflow_usage: dict | None = None,
    rate_tokens: int | None = None,
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

    token_source = "upstream"
    family = _endpoint_family(path)
    if status_code >= 400:
        # Error bodies carry no completion; never fabricate usage from them.
        prompt = completion = total = 0
        token_source = "none"
    elif usage is not None:
        model = model or usage.model
        prompt = usage.prompt_tokens
        completion = usage.completion_tokens
        total = usage.total_tokens
        if prompt == 0 and completion == 0 and total == 0:
            estimate = _local_estimate(request_body, response_body, model, family)
            if estimate.total_tokens:
                prompt, completion, total = (
                    estimate.prompt_tokens,
                    estimate.completion_tokens,
                    estimate.total_tokens,
                )
                token_source = estimate.source
            else:
                token_source = "none"
    else:
        estimate = _local_estimate(request_body, response_body, model, family)
        if estimate.total_tokens:
            prompt, completion, total = (
                estimate.prompt_tokens,
                estimate.completion_tokens,
                estimate.total_tokens,
            )
            token_source = estimate.source
        else:
            prompt = completion = total = 0
            token_source = "none"

    upstream_request_id = None
    if response_body:
        try:
            decoded = json.loads(response_body)
            if isinstance(decoded, dict):
                upstream_request_id = decoded.get("id")
        except (ValueError, TypeError):
            upstream_request_id = None

    if workflow_usage and status_code < 400:
        # Workflow sub-calls are real spend; fold them into this row's totals so
        # token rate limits see the whole pipeline, not just the final call.
        prompt += max(0, int(workflow_usage.get("prompt") or 0))
        completion += max(0, int(workflow_usage.get("completion") or 0))
        total = prompt + completion

    # Error rows keep zero tokens (never fabricate usage), but sub-call spend
    # already happened — settle the rate counters with it so a failing chain
    # cannot be retried in a tight loop for free.
    settle_tokens = total if rate_tokens is None else max(0, int(rate_tokens))

    token_windows = _enabled_token_windows(api_key)
    if token_windows:
        db.rate_usage_add(
            api_key["id"],
            "tokens",
            {window: settle_tokens for window in token_windows},
        )

    premodel = ctx.get("premodel")
    if premodel is not None:
        token_windows = _enabled_token_windows(premodel)
        if token_windows:
            db.premodel_rate_usage_add(
                premodel["id"],
                "tokens",
                {window: settle_tokens for window in token_windows},
            )

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
        token_source=token_source,
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
    limits = _key_limits(ctx["api_key"])

    raw_body = request.get_data() or b""
    is_multipart = request.mimetype == "multipart/form-data"
    if is_multipart:
        # Image edits post multipart/form-data where ``model`` is a form field;
        # a stand-in body lets the prefix/allowlist logic run against it.
        parsed_body = _multipart_model_body()
        model_raw = parsed_body.get("model") if isinstance(parsed_body, dict) else None
    else:
        try:
            parsed_body = json.loads(raw_body) if raw_body else None
        except (ValueError, TypeError):
            parsed_body = None
        model_raw = model_from_request_body(raw_body)
    model = model_raw
    host_prefix = None
    rewritten = False

    model_field = parsed_body.get("model") if isinstance(parsed_body, dict) else None
    prefix = _premodel_prefix(db)
    premodel = None
    workflow = None
    premodel_slug, model_remainder = extract_premodel_prefix(model_field, prefix)
    if premodel_slug:
        premodel = db.get_premodel_by_slug(premodel_slug)
        if premodel is None:
            return _json_error(
                f"Unknown premodel '{prefix}{premodel_slug}'.",
                404,
                "unknown_premodel",
                "midware_routing_error",
            )
        if not _premodel_allowed(limits, premodel):
            return _permission_error(
                f"Premodel '{prefix}{premodel_slug}' is not allowed for this API key.",
                "premodel_not_allowed",
            )
        if _endpoint_family(path) != "chat":
            # Premodels are chat prompt overlays; an image or embedding call
            # addressing one is a routing mistake, never a silent pass-through.
            return _permission_error(
                f"Premodel '{prefix}{premodel_slug}' only applies to chat completions; "
                f"/v1/{path.strip('/')} is an image or embedding endpoint.",
                "premodel_chat_only",
            )
        if is_multipart:
            return _json_error(
                f"Premodel '{prefix}{premodel_slug}' needs a JSON request body.",
                400,
                "premodel_body_not_json",
                "midware_routing_error",
            )
        target = db.get_route(premodel["route_id"]) if premodel["route_id"] else None
        if target is not None:
            route = target
        workflow_override = ""
        if bool(premodel["use_workflow"]):
            try:
                workflow = load_workflow(premodel["workflow_json"])
            except WorkflowError as exc:
                return _json_error(
                    f"Premodel '{prefix}{premodel_slug}' has an invalid workflow: {exc}",
                    502,
                    "workflow_invalid",
                    "midware_workflow_error",
                )
            workflow_override = workflow["final"]["model_override"]
        elif isinstance(parsed_body.get("messages"), list):
            # Workflow mode replaces the prompt overlay entirely; the Final
            # Request builder decides what messages reach the upstream.
            parsed_body["messages"] = apply_premodel_messages(premodel, parsed_body["messages"])
        new_model = (
            (model_remainder or "").strip()
            or workflow_override
            or (premodel["model"] or "").strip()
        )
        parsed_body["model"] = new_model
        apply_sampling_params(parsed_body, premodel)
        model = new_model or None
        rewritten = True

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
        current_model = parsed_body.get("model") if isinstance(parsed_body, dict) else None
        parsed_body["model"] = HOST_PREFIX_RE.sub(
            "", current_model if isinstance(current_model, str) else "", count=1
        ).lstrip()
        model = parsed_body["model"] or None
        rewritten = True

    if not _route_allowed(limits, route["id"]):
        return _permission_error(
            f"Provider '{route['name']}' is not allowed for this API key.",
            "provider_not_allowed",
        )

    if not _model_allowed(limits, route["id"], model):
        return _permission_error(
            f"Model '{model or ''}' is not allowed for this API key.",
            "model_not_allowed",
        )

    multipart_content_type = None
    if rewritten and isinstance(parsed_body, dict):
        if is_multipart:
            raw_body, multipart_content_type = _rebuild_multipart(
                str(parsed_body.get("model") or "")
            )
        else:
            raw_body = json.dumps(parsed_body, ensure_ascii=False).encode("utf-8")

    base_url, prefix = split_target(route)
    upstream_path = f"{prefix}/{(path or '').lstrip('/')}"

    streamed = _stream_requested(parsed_body)
    headers = _prepare_headers(route, force_sse=streamed)
    if multipart_content_type:
        # The client's header carries the old boundary; the rebuilt body does not.
        headers["Content-Type"] = multipart_content_type

    url = f"{base_url}/{upstream_path.lstrip('/')}"
    if request.query_string:
        url = f"{url}?{request.query_string.decode('utf-8', 'replace')}"

    client = _http_client()
    started = time.perf_counter()

    def persist(**kwargs):
        _persist(
            ctx={"api_key": ctx["api_key"], "route": route, "premodel": premodel},
            path=path,
            request_body=raw_body.decode("utf-8", "replace"),
            model_raw=model_raw,
            host_prefix=host_prefix,
            **kwargs,
        )

    def reject(rate_error):
        response, code = rate_error
        persist(
            model=model,
            status_code=429,
            latency_ms=int((time.perf_counter() - started) * 1000),
            streamed=False,
            response_body=None,
            error=f"rate_limited: {code}",
        )
        return response

    api_key = ctx["api_key"]
    rate_error = _rate_limit_check(
        db,
        api_key,
        api_key["id"],
        "API key",
        amounts=db.rate_usage_amounts,
        claim=db.rate_usage_claim,
    )
    if rate_error:
        return reject(rate_error)
    if premodel is not None:
        rate_error = _rate_limit_check(
            db,
            premodel,
            premodel["id"],
            "premodel",
            amounts=db.premodel_rate_usage_amounts,
            claim=db.premodel_rate_usage_claim,
        )
        if rate_error:
            return reject(rate_error)

    workflow_usage: dict | None = None
    workflow_reasoning = ""
    if workflow is not None:
        # Sub-calls run after the claims so a rejected request never spends
        # upstream tokens; their usage folds into the single request row.
        try:
            result = run_workflow(
                client,
                workflow,
                premodel,
                parsed_body.get("messages")
                if isinstance(parsed_body.get("messages"), list)
                else [],
                route_lookup=db.get_route,
                request_context={
                    "method": request.method,
                    "path": request.full_path.rstrip("?"),
                    "headers": dict(request.headers),
                },
            )
        except WorkflowNodeError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            partial = exc.usage or {}
            persist(
                model=model,
                status_code=502,
                latency_ms=latency_ms,
                streamed=False,
                response_body=None,
                error=f"workflow[{exc.node_name}]: {exc.detail}",
                rate_tokens=max(0, int(partial.get("prompt") or 0))
                + max(0, int(partial.get("completion") or 0)),
            )
            return _json_error(
                f"Workflow node '{exc.node_name}' failed: {exc.public_detail}",
                502,
                "workflow_node_failed",
                "midware_workflow_error",
            )
        parsed_body["messages"] = result.messages
        raw_body = json.dumps(parsed_body, ensure_ascii=False).encode("utf-8")
        workflow_usage = result.usage
        workflow_reasoning = result.reasoning

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
    content_type = upstream.headers.get("content-type", "application/json")

    if upstream.status_code >= 400:
        _maybe_fetch_models_after_upstream_error(upstream.status_code)

    if not streamed:
        body = upstream.content
        upstream.close()
        if workflow_reasoning and upstream.status_code < 400:
            body = _merge_reasoning_body(body, workflow_reasoning)
        if (
            upstream.status_code < 400
            and _endpoint_family(path) == "image"
            and str(ctx["api_key"]["image_response_mode"] or "b64") == "url"
        ):
            # URL-mode keys get their images stored server-side; the rewritten
            # (much smaller) body is what both the client and the log see.
            body = rewrite_image_body(
                body,
                ctx["api_key"]["id"],
                int(ctx["api_key"]["image_retention_days"] or 0),
                force_https=bool(ctx["api_key"]["image_url_https"]),
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        persist(
            model=model,
            status_code=upstream.status_code,
            latency_ms=latency_ms,
            streamed=False,
            response_body=body.decode("utf-8", "replace") if body else None,
            workflow_usage=workflow_usage,
        )
        return Response(body, status=upstream.status_code, headers=dict(response_headers), content_type=content_type)

    capture_limit = _capture_limit()

    def generate() -> Iterator[bytes]:
        if workflow_reasoning and upstream.status_code < 400:
            injected = {
                "id": "midware-workflow",
                "choices": [{"delta": {"reasoning_content": workflow_reasoning}}],
            }
            yield f"data: {json.dumps(injected, ensure_ascii=False)}\n\n".encode("utf-8")
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
                workflow_usage=workflow_usage,
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


@bp.route("/img/<image_id>", methods=["GET"])
def serve_image(image_id: str):
    """Serve an image stored for a URL-mode key.

    Public by design: the unguessable id is the capability, mirroring how
    upstream providers hand out CDN URLs. Expired or unknown ids are 404s.
    """
    result = load_image(image_id)
    if result is None:
        abort(404)
    data, ext = result
    return Response(
        data,
        content_type=IMAGE_CONTENT_TYPES.get(ext, "image/png"),
        headers={
            "Cache-Control": "public, max-age=604800",
            # Static wildcard, not per-key CORS: the id is the capability, so
            # letting any origin fetch the bytes leaks nothing extra.
            "Access-Control-Allow-Origin": "*",
        },
    )


@bp.route("/v1/<path:subpath>", methods=["POST", "GET", "PUT", "PATCH", "DELETE"])
@bp.route("/v1", methods=["POST", "GET", "PUT", "PATCH", "DELETE"])
def proxy_v1(subpath: str = ""):
    ctx, error = authenticate()
    if error:
        g.midware_cors_origin = "*"
        payload, status = error
        return Response(json.dumps(payload), status=status, mimetype="application/json")
    origin_error = _authorize(ctx)
    if origin_error:
        payload, status = origin_error
        return Response(json.dumps(payload), status=status, mimetype="application/json")
    if request.method == "GET" and (subpath or "").strip("/") == "models":
        return _models_catalogue(ctx)
    if request.method != "POST":
        return _json_error("Only POST requests are proxied.", 405, "method_not_allowed")
    return _forward(ctx, subpath)


@bp.route("/proxy/<path:subpath>", methods=["POST", "GET", "PUT", "PATCH", "DELETE"])
def proxy_explicit(subpath: str):
    """Explicit alias for hosts that already include ``/v1`` in their base URL."""
    ctx, error = authenticate()
    if error:
        g.midware_cors_origin = "*"
        payload, status = error
        return Response(json.dumps(payload), status=status, mimetype="application/json")
    origin_error = _authorize(ctx)
    if origin_error:
        payload, status = origin_error
        return Response(json.dumps(payload), status=status, mimetype="application/json")
    if request.method != "POST":
        return _json_error("Only POST requests are proxied.", 405, "method_not_allowed")
    return _forward(ctx, subpath)
