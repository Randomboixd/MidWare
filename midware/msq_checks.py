"""Hardcore MSQ checks: the optional, higher-signal probes.

Three families, each independently toggled per recorder and all off by default:

* **Stream integrity** - incremental SSE auditing for malformed chunks, missing
  completion markers, provider error events and long inter-token stalls.
* **Text forensics** - cheap, dependency-free analysis of the answer itself:
  repetition loops, glitch/mojibake characters and leaked chat-template tokens.
* **Long-context needle** - hides a code in filler text and checks the model can
  still retrieve it, which catches attention/context degradation a poem cannot.

The module deliberately contains no database or HTTP code: it takes bytes/text in
and returns ``findings`` (each ``{"reason", "points", "kind", "passed"}``) plus a
``hard`` flag for failures severe enough to mark a recorder failing immediately.
"""

from __future__ import annotations

import json
import random
import re
import secrets
import time
from collections import Counter
from typing import Any

from .conversation import build_conversation

# Content fields that hold the visible answer inside a streamed delta.
_REASONING_FIELDS = (
    "reasoning",
    "reasoning_content",
    "thinking",
    "thought",
    "analysis",
    "reasoning_text",
)


def finding(reason: str, points: float = 0.0, kind: str = "check", passed: bool = True) -> dict[str, Any]:
    return {"reason": reason, "points": round(float(points), 1), "kind": kind, "passed": bool(passed)}


def _field_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        return "".join(chunks)
    return ""


def _event_text(event: Any) -> str:
    """Best-effort visible text carried by one parsed SSE event."""
    if not isinstance(event, dict):
        return ""
    parts: list[str] = []

    choices = event.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for holder_key in ("delta", "message"):
                holder = choice.get(holder_key)
                if isinstance(holder, dict):
                    parts.append(_field_text(holder.get("content")))
                    for field in _REASONING_FIELDS:
                        value = holder.get(field)
                        if isinstance(value, str):
                            parts.append(value)
            text = choice.get("text")
            if isinstance(text, str):
                parts.append(text)

    delta = event.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("text"), str):
        parts.append(delta["text"])
    block = event.get("content_block")
    if isinstance(block, dict) and isinstance(block.get("text"), str):
        parts.append(block["text"])

    return "".join(part for part in parts if part)


def _short(value: Any, limit: int = 160) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _fmt_duration(ms: int | float | None) -> str:
    if ms is None:
        return "—"
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{int(ms)}ms"


class StreamAudit:
    """Incrementally parse an SSE transcript, tracking health and timing.

    Feeding raw chunks (rather than re-parsing the whole capture at the end) is
    what makes inter-token stalls detectable: a provider that pauses mid-stream
    is invisible to a start-to-finish latency number.
    """

    def __init__(self, stall_ms: int = 5_000) -> None:
        self.stall_ms = max(1, int(stall_ms))
        self._line = b""
        self._event: list[bytes] = []
        self._last_at: float | None = None
        self.first_token = False
        self.chunks = 0
        self.malformed = 0
        self.event_count = 0
        self.terminal = False
        self.finish_reason: str | None = None
        self.error_event: str | None = None
        self.max_gap_ms = 0
        self.stall_count = 0

    def feed(self, chunk: bytes, at: float | None = None) -> None:
        if not chunk:
            return
        at = time.perf_counter() if at is None else at
        self.chunks += 1
        if self._last_at is not None:
            gap = int((at - self._last_at) * 1000)
            if gap > self.max_gap_ms:
                self.max_gap_ms = gap
            if gap > self.stall_ms:
                self.stall_count += 1
        self._last_at = at

        self._line += chunk
        while True:
            index = self._line.find(b"\n")
            if index < 0:
                break
            raw = self._line[:index]
            self._line = self._line[index + 1 :]
            self._consume_line(raw.rstrip(b"\r"))

    def _consume_line(self, line: bytes) -> None:
        if not line:
            self._flush()
            return
        if line.startswith(b":"):
            return
        if line.startswith(b"data:"):
            self._event.append(line[5:].lstrip(b" "))

    def _flush(self) -> None:
        if not self._event:
            return
        data = b"\n".join(self._event).strip()
        self._event = []
        if not data:
            return
        if data == b"[DONE]":
            self.terminal = True
            return
        try:
            event = json.loads(data.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            self.malformed += 1
            return
        self.event_count += 1
        if isinstance(event, dict):
            self._absorb(event)

    def _absorb(self, event: dict[str, Any]) -> None:
        if not self.first_token and _event_text(event):
            self.first_token = True
        error = event.get("error")
        if error and not self.error_event:
            self.error_event = _short(error)
        choices = event.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if isinstance(choice, dict) and choice.get("finish_reason"):
                    self.finish_reason = choice["finish_reason"]
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("stop_reason"):
            self.finish_reason = delta["stop_reason"]
        if event.get("stop_reason"):
            self.finish_reason = event["stop_reason"]
        if event.get("type") in {"message_stop", "message_end"}:
            self.terminal = True


def stream_integrity_findings(audit: StreamAudit, stall_ms: int | None = None) -> tuple[list[dict], bool]:
    """Findings from a completed stream audit, plus a hard-failure flag."""
    findings: list[dict[str, Any]] = []
    hard = False

    if audit.malformed:
        hard = True
        findings.append(finding(f"{audit.malformed} malformed stream chunk(s)", 100.0, "stream", False))
    if audit.error_event:
        hard = True
        findings.append(finding(f"Stream carried an error: {audit.error_event}", 100.0, "stream", False))
    # Only judge completeness when the body actually looked like SSE; a host that
    # answered `stream:true` with a plain JSON object has nothing to truncate.
    saw_sse = audit.terminal or audit.event_count > 0 or audit.malformed > 0
    if saw_sse and not audit.terminal and audit.finish_reason is None:
        hard = True
        findings.append(
            finding("Stream ended without a completion marker (truncated)", 100.0, "stream", False)
        )
    elif audit.finish_reason == "length":
        findings.append(
            finding("The provider cut the response at its length limit", 15.0, "stream", False)
        )

    limit = audit.stall_ms if stall_ms is None else int(stall_ms)
    if audit.stall_count:
        points = min(30.0, 8.0 * audit.stall_count)
        findings.append(
            finding(
                f"{audit.stall_count} stall(s) over {_fmt_duration(limit)} (worst {_fmt_duration(audit.max_gap_ms)})",
                points,
                "stall",
                False,
            )
        )

    if not findings:
        findings.append(finding("Stream completed cleanly", 0.0, "stream", True))
    return findings, hard


_WORD_RE = re.compile(r"[A-Za-z0-9']+")
_WORD_LOOP_RE = re.compile(r"\b(\w+)(?:\s+\1){5,}\b", re.IGNORECASE)
_GLITCH_MOJIBAKE_RE = re.compile(r"[\u00c2-\u00c3][\u0080-\u00bf]|\u00e2\u0080[\u0080-\u00bf]")
_TEMPLATE_RE = re.compile(
    r"<\|(?:im_start|im_end|endoftext|start_header_id|end_header_id|eot_id|begin_of_text|"
    r"end_of_text|user|assistant|system|tool|pad)\|>|\[/?INST\]|<<SYS>>|</?s>"
)


def _repetition_findings(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    words = _WORD_RE.findall((text or "").lower())
    if len(words) >= 24:
        size = 6
        grams = Counter(tuple(words[i : i + size]) for i in range(len(words) - size + 1))
        gram, count = grams.most_common(1)[0]
        if count >= 4:
            points = min(30.0, 10.0 * (count - 2))
            findings.append(
                finding(f"Repeats “{' '.join(gram[:8])}” {count} times", points, "repetition", False)
            )
    if _WORD_LOOP_RE.search(text or ""):
        findings.append(finding("A word repeats many times in a row", 25.0, "repetition", False))
    return findings


def _glitch_findings(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if "\ufffd" in text:
        findings.append(
            finding(f"{text.count(chr(0xFFFD))} replacement character(s) in the output", 25.0, "glitch", False)
        )
    control = sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t")
    if control:
        findings.append(finding(f"{control} control character(s) in the output", 20.0, "glitch", False))
    zero_width = sum(text.count(char) for char in ("\u200b", "\u200c", "\u200d", "\ufeff"))
    if zero_width:
        findings.append(finding(f"{zero_width} zero-width character(s) in the output", 15.0, "glitch", False))
    mojibake = len(_GLITCH_MOJIBAKE_RE.findall(text))
    if mojibake:
        findings.append(finding(f"{mojibake} mojibake sequence(s) in the output", 20.0, "glitch", False))
    return findings


def _template_findings(text: str) -> list[dict[str, Any]]:
    matches: list[str] = []
    for match in _TEMPLATE_RE.findall(text or ""):
        if match not in matches:
            matches.append(match)
    if not matches:
        return []
    return [
        finding("Leaked chat-template token(s): " + " ".join(matches[:5]), 25.0, "template", False)
    ]


def split_response_text(raw: Any) -> tuple[str, str]:
    """Split a captured response into ``(visible_answer, reasoning)``.

    Reasoning models draft and repeat themselves while thinking, which is noisy
    for quality scoring, so the analysis can be pointed at the visible answer
    only. Falls back to the raw body through ``completion_text`` elsewhere.
    """
    try:
        data = build_conversation(None, raw)
    except Exception:
        return "", ""
    visible: list[str] = []
    reasoning: list[str] = []
    for block in data.get("messages", []):
        if block.get("source") != "response":
            continue
        if block.get("text"):
            visible.append(block["text"])
        for part in block.get("reasoning") or []:
            if part:
                reasoning.append(part)
    return "\n\n".join(visible), "\n\n".join(reasoning)


def text_forensics_findings(text: str) -> list[dict[str, Any]]:
    findings = _repetition_findings(text) + _glitch_findings(text) + _template_findings(text)
    if not findings:
        findings.append(finding("Text looks clean", 0.0, "forensics", True))
    return findings


_FILLER_WORDS = (
    "river", "stone", "quiet", "market", "silver", "harbor", "candle", "meadow",
    "lantern", "compass", "garden", "thunder", "valley", "willow", "anchor", "copper",
    "morning", "shadow", "ember", "ribbon", "mountain", "hollow", "feather", "orchard",
    "puzzle", "signal", "harbor", "window", "traveler", "kingdom", "harvest", "beacon",
)


def build_needle_prompt(context_chars: int, rng: random.Random | None = None) -> tuple[str, str]:
    """Return ``(code, prompt)`` with a code buried in ``context_chars`` of filler."""
    rng = rng or random.Random()
    target = max(200, int(context_chars))
    words: list[str] = []
    size = 0
    while size < target:
        word = rng.choice(_FILLER_WORDS)
        words.append(word)
        size += len(word) + 1
    filler = " ".join(words)

    code = "MW-" + secrets.token_hex(3).upper()
    split = filler.find(" ", int(len(filler) * 0.55))
    if split < 0:
        split = len(filler) // 2
    document = filler[:split] + f" The vault access code is {code}. " + filler[split:]
    prompt = (
        "The text below contains a hidden vault access code. Read it carefully and "
        "reply with ONLY the code, nothing else.\n\n"
        f"{document}\n\nQuestion: What is the vault access code?"
    )
    return code, prompt


def needle_findings(code: str, text: str) -> tuple[list[dict[str, Any]], bool]:
    """Grade a needle answer: a miss is a hard failure, a hit is a pass."""
    if code and code.lower() in (text or "").lower():
        return [finding("Recovered the hidden code from long context", 0.0, "needle", True)], False
    return [finding("Missed the hidden code buried in long context", 100.0, "needle", False)], True
