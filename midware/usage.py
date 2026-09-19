"""Extract token accounting from OpenAI-compatible request/response payloads.

MidWare is provider-agnostic: it understands the OpenAI wire format first and
falls back to Anthropic-style ``usage`` blocks when the OpenAI shape is absent.
Anything it cannot understand yields ``None`` so the caller can leave the
stored counters alone rather than writing a bogus zero.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str | None = None

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def usage_from_mapping(data: Any) -> TokenUsage | None:
    """Pull token counts out of a decoded usage object (both dialect styles)."""
    if not isinstance(data, dict):
        return None

    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = data

    prompt = completion = total = None
    if isinstance(usage, dict):
        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))
        total = _as_int(usage.get("total_tokens"))

        if prompt is None:
            prompt = _as_int(usage.get("input_tokens"))
        if completion is None:
            completion = _as_int(usage.get("output_tokens"))
        # Anthropic nests cache accounting inside input_tokens already.

        if total is None and (prompt is not None or completion is not None):
            total = (prompt or 0) + (completion or 0)
        if prompt is None and total is not None and completion is not None:
            prompt = max(total - completion, 0)
        if completion is None and total is not None and prompt is not None:
            completion = max(total - prompt, 0)

    if prompt is None and completion is None and total is None:
        return None

    model = data.get("model") if isinstance(data, dict) else None
    return TokenUsage(
        prompt_tokens=prompt or 0,
        completion_tokens=completion or 0,
        total_tokens=total if total is not None else (prompt or 0) + (completion or 0),
        model=model if isinstance(model, str) and model else None,
    )


def usage_from_response(raw: bytes | str | None) -> TokenUsage | None:
    """Parse a full (non-streamed) JSON response body."""
    if not raw:
        return None
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return usage_from_mapping(payload)


def parse_sse_events(raw: bytes | str | None) -> list[dict[str, Any]]:
    """Parse an SSE transcript into the list of JSON payloads it carried."""
    if not raw:
        return []
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw

    events: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", text):
        data_lines: list[str] = []
        for line in block.splitlines():
            line = line.rstrip("\r")
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if not data_lines:
            continue
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            continue
        try:
            events.append(json.loads(data))
        except (ValueError, TypeError):
            continue
    return events


def usage_from_stream(raw: bytes | str | None) -> TokenUsage | None:
    """Parse the usage out of a captured SSE stream.

    OpenAI-style streams emit ``usage`` on the final chunk (and on every chunk
    when ``stream_options.include_usage`` is set); Anthropic-style streams emit
    ``message_start`` / ``message_delta`` events. We merge whatever we find so
    both dialects work.
    """
    events = parse_sse_events(raw)
    if not events:
        return None

    prompt = completion = total = None
    model: str | None = None

    def absorb(usage: dict[str, Any]) -> None:
        nonlocal prompt, completion, total
        value = usage.get("prompt_tokens")
        if value is None:
            value = usage.get("input_tokens")
        if (found := _as_int(value)) is not None:
            prompt = found
        value = usage.get("completion_tokens")
        if value is None:
            value = usage.get("output_tokens")
        if (found := _as_int(value)) is not None:
            completion = found
        if (found := _as_int(usage.get("total_tokens"))) is not None:
            total = found
        for key in ("input_tokens", "prompt_tokens"):
            nested = usage.get(key)
            if isinstance(nested, dict):
                absorb(nested)

    for event in events:
        if not isinstance(event, dict):
            continue
        if model is None and isinstance(event.get("model"), str):
            model = event["model"] or None
        if isinstance(event.get("message"), dict) and isinstance(event["message"].get("model"), str):
            model = event["message"]["model"] or model
        if isinstance(event.get("usage"), dict):
            absorb(event["usage"])
        if isinstance(event.get("message"), dict) and isinstance(event["message"].get("usage"), dict):
            absorb(event["message"]["usage"])

    if prompt is None and completion is None and total is None:
        if model is None:
            return None
        return TokenUsage(model=model)

    if total is None:
        total = (prompt or 0) + (completion or 0)
    if prompt is None:
        prompt = max(total - (completion or 0), 0)
    if completion is None:
        completion = max(total - (prompt or 0), 0)

    return TokenUsage(
        prompt_tokens=prompt or 0,
        completion_tokens=completion or 0,
        total_tokens=total,
        model=model,
    )


def model_from_request_body(raw: bytes | str | None) -> str | None:
    if not raw:
        return None
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = raw
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("model"), str):
        return payload["model"] or None
    return None


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def truncate(text: str | None, limit: int) -> str | None:
    """Decode bytes-ish content and cap it so the database stays small."""
    if text is None:
        return None
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    text = strip_ansi(text)
    if limit and len(text) > limit:
        return text[:limit] + f"\n... [{len(text) - limit} bytes truncated]"
    return text


def iter_sse_lines(chunks: Iterable[bytes]) -> Iterable[bytes]:
    """Re-emit raw SSE bytes untouched; kept for symmetry with the parser."""
    for chunk in chunks:
        if chunk:
            yield chunk
