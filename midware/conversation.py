"""Turn captured request/response bodies into a readable conversation.

The raw bodies are provider-shaped JSON: a request carries ``messages`` and a
response carries ``choices``. This module normalizes both into a flat list of
display blocks so the UI can render them without knowing any provider quirks.

Reasoning is deliberately treated as a first-class block. Different stacks spell
it differently (``reasoning``, ``reasoning_content``, ``thinking``, ``thought``,
``analysis``, ``reasoning_details``, Anthropic ``thinking`` content parts), and
several presets emit more than one field at once, so every recognized variant
becomes its own collapsible section.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Message fields that hold the visible answer. ``content`` is by far the most
# common; ``refusal`` is OpenAI's safety escape hatch and is worth surfacing.
TEXT_FIELDS = ("content", "text", "output_text", "refusal")

# Fields that hold hidden chain-of-thought under various names.
REASONING_FIELDS = (
    "reasoning",
    "reasoning_content",
    "thinking",
    "thought",
    "thoughts",
    "analysis",
    "reasoning_text",
)

# Anthropic-style content parts whose ``text`` is actually reasoning.
REASONING_PART_TYPES = {"thinking", "redacted_thinking", "reasoning", "analysis"}

# Placeholders shown when a block carries no human-readable text.
_NO_TEXT = "(no text)"
_IMAGE = "[image]"
_AUDIO = "[audio]"
_TOOL_CALL = "tool call"


def _pretty(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return str(value)


def _field_text(value: Any) -> str:
    """Render one message field, flattening multimodal content-part lists."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                kind = str(part.get("type") or "").lower()
                if kind in {"text", "input_text", "output_text"}:
                    chunks.append(str(part.get("text") or ""))
                elif kind in {"image_url", "input_image", "image"}:
                    chunks.append(_IMAGE)
                elif kind in {"input_audio", "audio"}:
                    chunks.append(_AUDIO)
                else:
                    chunks.append(_pretty(part))
            else:
                chunks.append(_pretty(part))
        return "\n".join(chunk for chunk in chunks if chunk)
    return _pretty(value)


def _reasoning_parts(value: Any) -> list[str]:
    """Flatten a reasoning field into a list of text sections."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        for key in ("text", "content", "value", "reasoning"):
            if isinstance(value.get(key), str):
                return _reasoning_parts(value[key])
        return [_pretty(value)] if value else []
    if isinstance(value, list):
        sections: list[str] = []
        for part in value:
            sections.extend(_reasoning_parts(part))
        return sections
    return [_pretty(value)]


def _reasoning_from_details(value: Any) -> list[str]:
    """``reasoning_details`` is a list of typed fragments; concatenate per type."""
    if not isinstance(value, list):
        return _reasoning_parts(value)
    joined = ""
    for part in value:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                joined += text
            else:
                joined += _pretty(part)
        elif isinstance(part, str):
            joined += part
    joined = joined.strip()
    return [joined] if joined else []


def _collect_reasoning(message: dict) -> list[str]:
    sections: list[str] = []
    for field in REASONING_FIELDS:
        sections.extend(_reasoning_parts(message.get(field)))
    sections.extend(_reasoning_from_details(message.get("reasoning_details")))

    # Anthropic-style content parts can mix text and thinking.
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                kind = str(part.get("type") or "").lower()
                if kind in REASONING_PART_TYPES:
                    sections.extend(_reasoning_parts(part.get("thinking") or part.get("text") or part))

    # De-duplicate while preserving order; drop exact repeats only.
    unique: list[str] = []
    for section in sections:
        section = section.strip()
        if section and section not in unique:
            unique.append(section)
    return unique


def _collect_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        visible: list[str] = []
        for part in content:
            if isinstance(part, dict):
                kind = str(part.get("type") or "").lower()
                if kind in REASONING_PART_TYPES:
                    continue
                if kind in {"text", "input_text", "output_text"}:
                    visible.append(str(part.get("text") or ""))
                elif kind in {"image_url", "input_image", "image"}:
                    visible.append(_IMAGE)
                elif kind in {"input_audio", "audio"}:
                    visible.append(_AUDIO)
                else:
                    visible.append(_pretty(part))
            elif isinstance(part, str):
                visible.append(part)
        joined = "\n".join(chunk for chunk in visible if chunk).strip()
        if joined:
            return joined

    chunks: list[str] = []
    for field in TEXT_FIELDS:
        text = _field_text(message.get(field)).strip()
        if text:
            chunks.append(text)

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        names = []
        for call in tool_calls:
            if isinstance(call, dict):
                name = call.get("function", {}).get("name") if isinstance(call.get("function"), dict) else None
                names.append(name or call.get("name") or "?")
        chunks.append(f"[{_TOOL_CALL}: {', '.join(names)}]")

    return "\n\n".join(chunks)


def _tool_call_lines(message: dict) -> list[str]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    lines = []
    for call in calls:
        if isinstance(call, dict):
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = function.get("name") or call.get("name") or "?"
            args = function.get("arguments", call.get("arguments"))
            lines.append(f"{name}({args if isinstance(args, str) else _pretty(args)})")
    return lines


def _base_message(index: int, role: str, source: str) -> dict[str, Any]:
    return {
        "index": index,
        "role": role or "unknown",
        "source": source,
        "text": "",
        "reasoning": [],
        "tool_calls": [],
        "tool_call_id": None,
        "name": None,
        "extra": {},
        "empty": True,
    }


def _from_message(raw: Any, index: int, source: str, default_role: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        block = _base_message(index, default_role, source)
        block["text"] = _field_text(raw).strip()
        block["empty"] = not block["text"]
        return block

    role = raw.get("role") or default_role
    block = _base_message(index, str(role), source)
    block["text"] = _collect_text(raw)
    block["reasoning"] = _collect_reasoning(raw)
    block["tool_calls"] = _tool_call_lines(raw)
    if isinstance(raw.get("tool_call_id"), str):
        block["tool_call_id"] = raw["tool_call_id"]
    if isinstance(raw.get("name"), str):
        block["name"] = raw["name"]
    if raw.get("finish_reason") is not None:
        block["extra"]["finish_reason"] = raw["finish_reason"]

    known = {
        "role",
        "content",
        "text",
        "output_text",
        "refusal",
        "tool_calls",
        "tool_call_id",
        "name",
        "finish_reason",
        *TEXT_FIELDS,
        *REASONING_FIELDS,
        "reasoning_details",
    }
    block["extra"].update({k: v for k, v in raw.items() if k not in known})

    block["empty"] = not (block["text"] or block["reasoning"] or block["tool_calls"])
    return block


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _append_reasoning(target: dict[str, Any], field: str, value: Any) -> None:
    """Append a reasoning delta without double-counting mirrored fields.

    Providers such as NanoGPT stream the same text through both ``reasoning``
    and ``reasoning_details`` (and sometimes ``reasoning_content``), so naive
    concatenation duplicates every chunk. We keep one string per spelling and
    collapse a spelling whose text is a prefix of another's, which is exactly
    the relationship of the mirrored streams.
    """
    if not isinstance(value, str) or not value:
        return
    key = "_reasoning_" + field
    target[key] = target.get(key, "") + value


def _flatten_reasoning(target: dict[str, Any]) -> str:
    """Collapse mirrored reasoning streams into a single string.

    ``reasoning``/``reasoning_details``/``reasoning_content`` frequently carry
    the same text, so we take the longest stream and drop every other stream
    that is a prefix of it. A stream that merely shares a long prefix (two
    genuinely different spellings that happen to agree early) must not be
    discarded, hence the coverage guard: a prefix is only treated as a mirror
    when it is either an exact match or covers most of the longest stream.
    """
    streams = sorted(
        (target[key] for key in target if key.startswith("_reasoning_")),
        key=len,
        reverse=True,
    )
    if not streams:
        return ""
    best = streams[0]
    for stream in streams[1:]:
        if not stream:
            continue
        if best.startswith(stream):
            coverage = len(stream) / len(best) if best else 0
            if stream == best or coverage >= 0.9:
                continue
        best = best if len(best) >= len(stream) else stream
        if not best.startswith(stream) and not stream.startswith(best):
            best = best + "\n\n" + stream
    return best


def _from_sse(raw: Any) -> list[dict[str, Any]]:
    """Rebuild assistant turns from a captured streaming transcript."""
    from .usage import parse_sse_events

    events = parse_sse_events(raw)
    if not events:
        return []

    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def ensure() -> dict[str, Any]:
        nonlocal current
        if current is None:
            current = {"role": "assistant", "content": "", "reasoning": ""}
        return current

    for event in events:
        if not isinstance(event, dict):
            continue
        # Anthropic wraps deltas in a named event with a payload.
        payload = event.get("delta") if isinstance(event.get("delta"), dict) else event
        message = event.get("message") if isinstance(event.get("message"), dict) else None
        if message is not None and isinstance(message.get("content"), list):
            for part in message["content"]:
                if isinstance(part, dict):
                    kind = str(part.get("type") or "").lower()
                    target = ensure()
                    if kind in REASONING_PART_TYPES:
                        _append_reasoning(target, "part", str(part.get("thinking") or part.get("text") or ""))
                    elif kind == "text":
                        target["content"] += str(part.get("text") or "")

        text = payload.get("content")
        if isinstance(text, str) and text:
            ensure()["content"] += text
        elif isinstance(text, list):
            for part in text:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    ensure()["content"] += part["text"]

        for field in REASONING_FIELDS:
            _append_reasoning(ensure(), field, payload.get(field))

        delta_details = payload.get("reasoning_details")
        if isinstance(delta_details, list):
            for part in delta_details:
                if isinstance(part, dict):
                    _append_reasoning(ensure(), "details", part.get("text"))

        choices = event.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    if isinstance(delta.get("content"), str):
                        ensure()["content"] += delta["content"]
                    for field in REASONING_FIELDS:
                        _append_reasoning(ensure(), field, delta.get(field))
                    delta_details = delta.get("reasoning_details")
                    if isinstance(delta_details, list):
                        for part in delta_details:
                            if isinstance(part, dict):
                                _append_reasoning(ensure(), "details", part.get("text"))
                message = choice.get("message")
                if isinstance(message, dict):
                    if isinstance(message.get("content"), str) and message["content"]:
                        ensure()["content"] += message["content"]
                    for field in REASONING_FIELDS:
                        _append_reasoning(ensure(), field, message.get(field))

        if event.get("type") in {"message_stop", "message_end"} or event.get("stop_reason"):
            turns.append(ensure())
            current = None

    if current is not None:
        current["reasoning"] = _flatten_reasoning(current)
        if current["content"] or current["reasoning"]:
            turns.append(current)
    return turns


def _raw_present(*bodies: Any) -> bool:
    """True when at least one captured body actually carries content."""
    for body in bodies:
        if isinstance(body, (bytes, bytearray)):
            if bytes(body).strip():
                return True
        elif isinstance(body, str):
            if body.strip():
                return True
        elif body:
            return True
    return False


def build_conversation(request_body: Any, response_body: Any) -> dict[str, Any]:
    """Normalize a captured request/response pair into display blocks."""
    request_payload = _parse_json(request_body)
    response_payload = _parse_json(response_body)

    messages: list[dict[str, Any]] = []
    counter = 0

    if isinstance(request_payload, dict):
        raw_messages = request_payload.get("messages")
        if isinstance(raw_messages, list):
            for raw in raw_messages:
                messages.append(_from_message(raw, counter, "request", "user"))
                counter += 1
        if not messages and request_payload.get("input") is not None:
            messages.append(_from_message(request_payload.get("input"), counter, "request", "user"))
            counter += 1

    if isinstance(response_payload, dict):
        choices = response_payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    messages.append(_from_message(choice.get("message"), counter, "response", "assistant"))
                    counter += 1
        elif isinstance(response_payload.get("content"), list):
            messages.append(_from_message(response_payload, counter, "response", "assistant"))
            counter += 1
    elif isinstance(response_payload, list):
        # Anthropic non-streamed responses are a list of content parts.
        messages.append(_from_message({"role": "assistant", "content": response_payload}, counter, "response", "assistant"))
        counter += 1

    # ``responses_api`` streams and any unparsed streaming transcript still need
    # turning into assistant turns. Only do this when the JSON path produced no
    # assistant block at all.
    if not any(block["source"] == "response" for block in messages) and response_body:
        for turn in _from_sse(response_body):
            messages.append(_from_message(turn, counter, "response", "assistant"))
            counter += 1

    for position, block in enumerate(messages, start=1):
        block["number"] = position

    return {
        "messages": messages,
        "request_meta": _meta(request_payload),
        "response_meta": _meta(response_payload),
        "has_request_messages": bool(messages) and any(m["source"] == "request" for m in messages),
        "raw_available": _raw_present(request_body, response_body),
    }


def _meta(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    meta: dict[str, Any] = {}
    for key in ("model", "object", "temperature", "max_tokens", "max_completion_tokens", "top_p", "stream"):
        if key in payload:
            meta[key] = payload[key]
    usage = payload.get("usage")
    if isinstance(usage, dict):
        meta["usage"] = {
            key: usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens")
            if key in usage
        }
    return meta


def split_host_prefix(model: str | None) -> tuple[str | None, str | None]:
    """Small helper used by tests/UI: ``[Host]model`` -> (``Host``, ``model``)."""
    if not model:
        return None, None
    match = re.match(r"^\s*\[(?P<name>[^\]#]+)?(?:#(?P<id>\d+))?\]\s*", model)
    if not match:
        return None, model
    return (match.group("name") or match.group("id")), model[match.end():]
