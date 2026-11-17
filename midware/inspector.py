"""The page model for one captured request.

Everything here is derived from bytes MidWare already stores — ``requests.
request_body``/``response_body`` plus the row's own columns — so the inspector
can show cache reads, reasoning tokens, what the call cost and which billing
pocket paid, without a schema change and without reaching upstream.

The reason it is worth a module rather than template logic: the providers put
this in the usage block they already send, and each dialect names the same
number differently (OpenAI ``prompt_tokens_details.cached_tokens``, Anthropic
``cache_read_input_tokens``, Gemini ``cachedContentTokenCount``). Reading it in
one place keeps the page honest about which dialect answered.

Two rules this module obeys:

* Never invent a figure. A value the response did not carry is ``None`` and the
  template renders ``none``, rather than a zero that reads as a measurement.
* Never grow a body in memory it does not have to. Bodies are strings that can
  reach ``MAX_CAPTURE_BYTES`` (8 MiB); the parse stops at the usage object and
  the stream scan is capped.
"""

from __future__ import annotations

import json
from typing import Any

from .usage import _as_int, parse_sse_events

#: How many stream events to walk when hunting for a usage block. Providers put
#: it in the last event; a capped scan keeps a pathological stream cheap.
STREAM_SCAN_LIMIT = 4000

#: Bodies can carry the capture truncation marker (see ``usage.truncate``).
TRUNCATION_MARKER = "bytes truncated"

#: How much of a body the page inlines. A stored body can reach 8 MiB, most of
#: it a stream; the fold shows the head and the archive download has the rest.
INLINE_LIMIT = 240_000


#: Above this a body is left on one line rather than reformatted: indent-2 on a
#: multi-megabyte JSON value costs more memory than the page is worth.
REFORMAT_LIMIT = 512_000


def preview(text: str | None, kind: str | None = None) -> tuple[str, bool, bool]:
    """A body as the page should render it: text, clipped, reformatted.

    JSON is pretty-printed for reading. The bytes as sent stay in the archive,
    and a reformat is flagged in the page so nobody mistakes it for the wire
    format. A stream is left alone: one event per line is how a stream reads,
    and expanding each event would bury the shape in thousands of lines.
    """
    if not text:
        return "", False, False
    display, reformatted = text, False
    if kind == "json" and len(text) <= REFORMAT_LIMIT:
        try:
            display = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            reformatted = True
        except (ValueError, TypeError):
            display = text
    if len(display) <= INLINE_LIMIT:
        return display, False, reformatted
    return display[:INLINE_LIMIT], True, reformatted


def body_kind(text: str | None) -> str:
    """Classify a captured body so the viewer knows how to present it."""
    if not text or not text.strip():
        return "empty"
    head = text.lstrip()[:400]
    if head.startswith("{") or head.startswith("["):
        try:
            json.loads(text)
        except (ValueError, TypeError):
            return "json-partial"
        return "json"
    if head.startswith("data:") or "\ndata:" in text[:2000] or "event:" in head:
        return "sse"
    if "Content-Disposition" in head or "Content-Type: multipart" in head:
        return "multipart"
    if head.lower().startswith(("<!doctype", "<html")):
        return "html"
    return "text"


def _first_int(*values: Any) -> int | None:
    for value in values:
        number = _as_int(value)
        if number is not None:
            return number
    return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                continue
    return None


def _dig(mapping: Any, *path: str) -> Any:
    """Read a nested key path, returning None the moment the chain breaks."""
    node = mapping
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _usage_object(event: Any) -> dict | None:
    """The usage mapping inside one decoded response object, if it has one."""
    if not isinstance(event, dict):
        return None
    usage = event.get("usage")
    if isinstance(usage, dict):
        return usage
    # Gemini names it differently and puts it at the top level.
    if isinstance(event.get("usageMetadata"), dict):
        return event["usageMetadata"]
    if "promptTokenCount" in event:
        return event
    return None


def _response_usage(response_body: str | None) -> dict | None:
    """The usage block of a stored response body, streamed or not.

    A stream repeats ``usage`` on its final event (when the client asked for
    it), so the last one wins: it is the only one carrying the totals.
    """
    if not response_body or not response_body.strip():
        return None
    kind = body_kind(response_body)
    if kind in ("json", "json-partial"):
        try:
            payload = json.loads(response_body)
        except (ValueError, TypeError):
            return None
        return _usage_object(payload)
    if kind != "sse":
        return None
    events = parse_sse_events(response_body)
    if not events:
        return None
    for event in reversed(events[-STREAM_SCAN_LIMIT:]):
        usage = _usage_object(event)
        if usage:
            return usage
    return None


def usage_detail(response_body: str | None) -> dict[str, Any]:
    """Token, cache, reasoning and cost detail from one stored response body."""
    usage: dict[str, Any] = _response_usage(response_body) or {}
    opencode_raw = usage.get("opencode")
    opencode: dict[str, Any] = opencode_raw if isinstance(opencode_raw, dict) else {}
    tokens_raw = opencode.get("tokens")
    opencode_tokens: dict[str, Any] = tokens_raw if isinstance(tokens_raw, dict) else {}

    prompt = _first_int(
        usage.get("prompt_tokens"),
        usage.get("input_tokens"),
        usage.get("promptTokenCount"),
    )
    completion = _first_int(
        usage.get("completion_tokens"),
        usage.get("output_tokens"),
        usage.get("candidatesTokenCount"),
    )
    total = _first_int(usage.get("total_tokens"), usage.get("totalTokenCount"))
    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)

    cached = _first_int(
        _dig(usage, "prompt_tokens_details", "cached_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("cachedContentTokenCount"),
        opencode_tokens.get("cache_read"),
    )
    cache_write = _first_int(
        _dig(usage, "prompt_tokens_details", "cache_write_tokens"),
        usage.get("cache_creation_input_tokens"),
        _dig(usage, "cache_creation", "cache_creation_input_tokens"),
        opencode_tokens.get("cache_write_5m"),
    )
    reasoning = _first_int(
        _dig(usage, "completion_tokens_details", "reasoning_tokens"),
        usage.get("reasoning_tokens"),
        usage.get("thoughtsTokenCount"),
        opencode_tokens.get("reasoning"),
    )
    # What the provider actually charged, and what it would have charged on a
    # metered plan — Go bills a subscription, so its own ``cost`` is 0 and the
    # list price is the number that says what the call was worth.
    charged = _first_number(usage.get("cost"), usage.get("total_cost"))
    list_cost = _first_number(opencode.get("list_cost"), usage.get("list_cost"))
    currency = usage.get("currency") if isinstance(usage.get("currency"), str) else None
    billing_source = opencode.get("billing_source")
    allowance = _first_number(opencode.get("allowance"))

    uncached = _first_int(opencode_tokens.get("input"))
    if uncached is None and prompt is not None and cached is not None:
        uncached = max(prompt - cached, 0)

    hit = None
    if prompt and cached is not None and prompt > 0:
        hit = min(max(cached / prompt * 100.0, 0.0), 100.0)

    return {
        "prompt": prompt,
        "completion": completion,
        "total": total,
        "cached": cached,
        "cache_write": cache_write,
        "uncached": uncached,
        "reasoning": reasoning,
        "cost": charged,
        "list_cost": list_cost,
        "currency": currency or ("USD" if charged is not None or list_cost is not None else None),
        "billing_source": billing_source if isinstance(billing_source, str) else None,
        "allowance": allowance,
        "cache_hit": hit,
        "reported": bool(usage),
    }


def request_facts(request_body: str | None) -> dict[str, Any]:
    """The call as the client sent it: transport flags and sampling knobs.

    Only top-level parameters that mean the same thing across providers are
    read; anything else stays in the body, which the payload block shows whole.
    """
    facts: dict[str, Any] = {"messages": None, "tools": None, "stream": None,
                             "usage_requested": None, "model": None,
                             "max_tokens": None, "temperature": None, "top_p": None,
                             "kind": body_kind(request_body)}
    if not request_body:
        return facts
    try:
        payload = json.loads(request_body)
    except (ValueError, TypeError):
        return facts
    if not isinstance(payload, dict):
        return facts
    messages = payload.get("messages")
    if isinstance(messages, list):
        facts["messages"] = len(messages)
    elif payload.get("input") is not None:
        facts["messages"] = 1 if isinstance(payload.get("input"), str) else None
    tools = payload.get("tools")
    if isinstance(tools, list):
        facts["tools"] = len(tools)
    if isinstance(payload.get("stream"), bool):
        facts["stream"] = payload["stream"]
    if isinstance(payload.get("stream_options"), dict):
        facts["usage_requested"] = bool(payload["stream_options"].get("include_usage"))
    model = payload.get("model")
    if isinstance(model, str) and model:
        facts["model"] = model
    facts["max_tokens"] = _first_int(payload.get("max_tokens"), payload.get("max_completion_tokens"))
    facts["temperature"] = _first_number(payload.get("temperature"))
    facts["top_p"] = _first_number(payload.get("top_p"))
    return facts


def response_facts(response_body: str | None, *, streamed: bool) -> dict[str, Any]:
    """The reply as the server sent it: transport, event count, finish reasons."""
    kind = body_kind(response_body)
    facts: dict[str, Any] = {
        "kind": kind,
        "bytes": len(response_body.encode("utf-8")) if response_body else 0,
        "streamed": streamed,
        "events": None,
        "event_types": [],
        "finish_reasons": [],
        "object": None,
        "model": None,
        "truncated": bool(response_body and TRUNCATION_MARKER in response_body),
    }
    if not response_body:
        return facts
    if kind in ("json", "json-partial"):
        try:
            payload = json.loads(response_body)
        except (ValueError, TypeError):
            return facts
        if isinstance(payload, dict):
            if isinstance(payload.get("object"), str):
                facts["object"] = payload["object"]
            if isinstance(payload.get("model"), str):
                facts["model"] = payload["model"]
            facts["finish_reasons"] = _finish_reasons(payload)
        return facts
    if kind != "sse":
        return facts
    events = parse_sse_events(response_body)
    facts["events"] = len(events)
    counts: dict[str, int] = {}
    reasons: list[str] = []
    for event in events[-STREAM_SCAN_LIMIT:]:
        if not isinstance(event, dict):
            continue
        obj = event.get("object")
        if isinstance(obj, str):
            counts[obj] = counts.get(obj, 0) + 1
        if isinstance(event.get("model"), str) and not facts["model"]:
            facts["model"] = event["model"]
        for reason in _finish_reasons(event):
            if reason not in reasons:
                reasons.append(reason)
    facts["event_types"] = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    facts["finish_reasons"] = reasons
    return facts


def _finish_reasons(payload: dict) -> list[str]:
    reasons: list[str] = []
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return reasons
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        reason = choice.get("finish_reason") or choice.get("stop_reason")
        if isinstance(reason, str) and reason and reason not in reasons:
            reasons.append(reason)
    return reasons


def cache_split(usage: dict[str, Any], response_body: str | None) -> dict[str, int]:
    """Anthropic-style cache write split (5m / 1h), when the body carries one."""
    split: dict[str, int] = {}
    usage_object = _response_usage(response_body) or {}
    creation = usage_object.get("cache_creation")
    if isinstance(creation, dict):
        for key, label in (("ephemeral_5m_input_tokens", "5m"), ("ephemeral_1h_input_tokens", "1h")):
            number = _as_int(creation.get(key))
            if number:
                split[label] = number
    tokens = _dig(usage_object, "opencode", "tokens")
    if isinstance(tokens, dict):
        for key, label in (("cache_write_5m", "5m"), ("cache_write_1h", "1h")):
            number = _as_int(tokens.get(key))
            if number:
                split[label] = number
    return split


def build_page(row, route=None, key=None) -> dict[str, Any]:
    """One dict the inspector template renders, assembled from one row."""
    request_body = row["request_body"] if "request_body" in row.keys() else None
    response_body = row["response_body"] if "response_body" in row.keys() else None
    usage = usage_detail(response_body)
    source = (row["token_source"] if "token_source" in row.keys() else "") or "upstream"
    # The stored column is what the proxy committed to; when the response no
    # longer carries a usage block the column is the only account we have.
    if not usage["reported"] and row["prompt_tokens"] is not None:
        usage.update(
            {
                "prompt": row["prompt_tokens"],
                "completion": row["completion_tokens"],
                "total": row["total_tokens"],
                "uncached": row["prompt_tokens"],
            }
        )
    estimated = source not in ("upstream", "none")
    request_kind = body_kind(request_body)
    response_kind = body_kind(response_body)
    request_preview, request_clipped, request_reformatted = preview(request_body, request_kind)
    response_preview, response_clipped, response_reformatted = preview(response_body, response_kind)
    return {
        "usage": usage,
        "cache_split": cache_split(usage, response_body),
        "request_bytes": len(request_body.encode("utf-8")),
        "response_bytes": len(response_body.encode("utf-8")),
        "request_preview": request_preview,
        "response_preview": response_preview,
        "request_clipped": request_clipped,
        "response_clipped": response_clipped,
        "inline_limit": INLINE_LIMIT,
        "request": request_facts(request_body),
        "response": response_facts(response_body, streamed=bool(row["streamed"])),
        "request_body": request_body or "",
        "response_body": response_body or "",
        "request_kind": request_kind,
        "response_kind": response_kind,
        "request_reformatted": request_reformatted,
        "response_reformatted": response_reformatted,
        "estimated": estimated,
        "token_source": source,
        "route": route,
        "key": key,
        "has_route_key": bool(route is not None and route["provider"]),
    }
