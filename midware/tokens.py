"""Best-effort local token counting for upstreams that never report usage.

Streaming responses are only billed if the provider emits a ``usage`` block, and
quite a few OpenAI-compatible hosts omit it from the final chunk. When that
happens MidWare reconstructs the visible output from the captured SSE transcript
and counts tokens locally so the usage charts stay meaningful.

The counter is deliberately approximate: MidWare is provider-agnostic, so it
cannot know which tokenizer a given route actually uses. Modern OpenAI models
(o-series and the GPT-4o/4.1/5 families) share the ``o200k_base`` vocabulary, so
that is the default; other families (Claude, Llama, Mistral, Gemini, Qwen, DeepSeek)
estimate tokens from character counts using ratios that are close enough for a
dashboard. Every count is tagged ``estimated``/``chars`` so a real upstream value
is never mistaken for a guess.

``tiktoken`` is an optional dependency: if it is missing (or the encoding list
cannot be downloaded) we silently fall back to character heuristics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Rough characters-per-token ratios per model family. These are only used when
# tiktoken is unavailable or the model is not an OpenAI one.
_CHARS_PER_TOKEN = 3.6
_FAMILY_RATIOS = {
    "claude": 3.4,
    "anthropic": 3.4,
    "llama": 3.7,
    "qwen": 2.6,
    "deepseek": 3.4,
    "mistral": 3.8,
    "gemini": 3.7,
    "gemma": 3.8,
    "phi": 3.7,
    "command": 3.7,
}

_openai_encodings = ("o200k_base", "cl100k_base")

_encoder_cache: dict[str, Any] = {}
_unavailable = False


def _load_encoder(model: str | None = None) -> Any:
    """Return a tiktoken encoding compatible with ``model``, or ``None``.

    The model decides the vocabulary: ``o200k_base`` for the o-series and the
    GPT-4o/4.1/5 families, ``cl100k_base`` for older GPT-3.5/4 models. Anything
    else (or a missing encoding file) returns ``None`` so the caller falls back
    to the character heuristic — a few thousand tokens of error on a dashboard
    is an acceptable trade for not bundling every tokenizer under the sun.
    """
    global _unavailable
    if _unavailable:
        return None
    lowered = (model or "").lower()
    if lowered and not _is_openai_family(lowered):
        return None
    name = "cl100k_base" if lowered and not _wants_o200k(lowered) else "o200k_base"
    if name in _encoder_cache:
        return _encoder_cache[name]
    encoding = None
    try:
        import tiktoken

        encoding = tiktoken.get_encoding(name)
    except Exception:  # not installed / missing BPE file / no network
        encoding = None
    if encoding is None:
        _unavailable = True
    _encoder_cache[name] = encoding
    return encoding


def _wants_o200k(model: str) -> bool:
    """o-series and GPT-4o/4.1/5 families share the ``o200k_base`` vocabulary."""
    for marker in ("o1", "o3", "o4", "gpt-4o", "gpt-4.1", "gpt-5"):
        if marker in model:
            return True
    return False


def _is_openai_family(model: str | None) -> bool:
    return not model or "gpt" in model or "o1" in model or "o3" in model or "o4" in model


def _family(model: str | None) -> str:
    lowered = (model or "").lower()
    for key in _FAMILY_RATIOS:
        if key in lowered:
            return key
    return ""


@dataclass
class Estimate:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    source: str


def _heuristic_tokens(text: str, model: str | None) -> int:
    if not text:
        return 0
    ratio = _FAMILY_RATIOS.get(_family(model), _CHARS_PER_TOKEN)
    return max(1, int(len(text) / ratio))


def count_text(text: str, model: str | None = None) -> tuple[int, str]:
    """Return ``(tokens, source)`` for one string.

    Estimation only ever happens for responses without an upstream ``usage``
    block, so the number is labelled to make clear it is a guess. Token
    accounting must never be the reason a request fails, hence the broad
    ``except`` around encoding: an odd BPE payload degrades to the heuristic.
    """
    if not text:
        return 0, "estimated"
    encoder = _load_encoder(model)
    if encoder is not None:
        try:
            return len(encoder.encode(text)), "tiktoken"
        except Exception:
            pass
    return _heuristic_tokens(text, model), "chars"


def _message_text(message: dict[str, Any]) -> str:
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif part.get("type") == "image_url":
                    parts.append("[image]")
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))
    for field in ("reasoning", "reasoning_content", "thinking", "thought", "analysis"):
        value = message.get(field)
        if isinstance(value, str):
            parts.append(value)
    details = message.get("reasoning_details")
    if isinstance(details, list):
        for part in details:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, dict):
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                parts.append(str(function.get("name") or ""))
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    parts.append(arguments)
    return "\n".join(part for part in parts if part)


def prompt_text(request_body: Any) -> str:
    """Concatenate the input side of a captured request body."""
    payload = request_body
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", "replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return payload
    if not isinstance(payload, dict):
        return ""

    parts: list[str] = []
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict):
                parts.append(str(message.get("role") or ""))
                parts.append(_message_text(message))
    if payload.get("input") is not None and not messages:
        value = payload["input"]
        parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    system = payload.get("system")
    if isinstance(system, str):
        parts.append(system)
    return "\n".join(part for part in parts if part)


def completion_text(response_body: Any) -> str:
    """Concatenate the visible output of a captured (or streamed) response body."""
    payload = response_body
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", "replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            from .usage import parse_sse_events

            events = parse_sse_events(payload)
            if not events:
                return payload
            parts: list[str] = []
            for event in events:
                if not isinstance(event, dict):
                    continue
                if isinstance(event.get("message"), dict):
                    parts.append(_message_text(event["message"]))
                if isinstance(event.get("delta"), dict):
                    parts.append(_message_text(event["delta"]))
                choices = event.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta")
                        if isinstance(delta, dict):
                            parts.append(_message_text(delta))
                        message = choice.get("message")
                        if isinstance(message, dict):
                            parts.append(_message_text(message))
            return "\n".join(part for part in parts if part)

    if isinstance(payload, dict):
        parts = []
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict):
                    message = choice.get("message")
                    if isinstance(message, dict):
                        parts.append(_message_text(message))
                    elif isinstance(choice.get("text"), str):
                        parts.append(choice["text"])
        content = payload.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
        if not parts:
            parts.append(_message_text(payload))
        return "\n".join(part for part in parts if part)
    if isinstance(payload, list):
        return "\n".join(_message_text(part) for part in payload if isinstance(part, dict))
    if payload is None:
        return ""
    return str(payload)


def estimate_usage(request_body: Any, response_body: Any, model: str | None = None) -> Estimate:
    """Count a request/response pair locally and label how it was counted."""
    prompt = prompt_text(request_body)
    completion = completion_text(response_body)

    prompt_tokens, prompt_source = count_text(prompt, model)
    completion_tokens, completion_source = count_text(completion, model)

    if prompt_source == "tiktoken" and completion_source == "tiktoken":
        source = "tiktoken"
    elif prompt_source == "chars" and completion_source == "chars":
        source = "chars"
    else:
        source = "+".join(sorted({prompt_source, completion_source}))

    return Estimate(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        source=source or "estimated",
    )
