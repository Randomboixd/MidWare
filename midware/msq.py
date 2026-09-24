"""Model Service Quality: periodic health/quality probes for a chosen model.

MSQ asks a model a fixed question on a schedule, measures how it answers (time to
first token, total generation time, token throughput) and looks for cheap signs
of trouble (stray non-Latin symbols, slop words). Each check is scored 0-100 and
stored; after a day of history a recorder is summarised as Working or Degraded.

The scheduler is a single daemon thread that wakes on ``MSQ_SCHEDULER_INTERVAL``
and runs whatever recorder is due. That mirrors the model-refresh thread and
keeps the "no ORM, no queue, no external worker" bias of the rest of MidWare.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from .db import parse_iso, parse_json_object, utcnow
from .msq_checks import (
    StreamAudit,
    build_needle_prompt,
    finding,
    needle_findings,
    split_response_text,
    stream_integrity_findings,
    text_forensics_findings,
)
from .tokens import completion_text, count_text, estimate_usage
from .usage import truncate, usage_from_response, usage_from_stream

EXAMINING = "examining"
FAILING = "failing"
DEGRADED = "degraded"
WORKING = "working"

STATUS_LABELS = {
    EXAMINING: "Still under examination",
    FAILING: "Failing",
    DEGRADED: "Degraded",
    WORKING: "Working",
}

DEFAULT_SYSTEM_PROMPT = "You are a precise assistant. Answer in clear English."
DEFAULT_USER_PROMPT = "Write a short poem about the ocean at sunrise."

# Collected probes are filed under this auto-created key so they appear in the
# normal request history and count toward token usage.
MSQ_API_KEY_NAME = "MSQ Measurements"
MSQ_REQUEST_PATH = "chat/completions"

# Scripts that should not appear in an English answer. A model that glitches into
# Chinese (or any other non-Latin script) while asked an English question is a
# useful, cheap degradation signal.
_NON_LATIN_RE = re.compile(
    "["
    "\u0400-\u04ff"  # Cyrillic
    "\u0590-\u05ff"  # Hebrew
    "\u0600-\u06ff"  # Arabic
    "\u0900-\u097f"  # Devanagari
    "\u0e00-\u0e7f"  # Thai
    "\u1100-\u11ff"  # Hangul jamo
    "\u3000-\u303f"  # CJK punctuation
    "\u3040-\u30ff"  # Hiragana / Katakana
    "\u3400-\u4dbf"  # CJK extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uac00-\ud7af"  # Hangul syllables
    "\uff00-\uffef"  # Fullwidth forms
    "\u2600-\u27bf"  # misc symbols
    "\U0001f000-\U0001faff"  # emoji
    "]"
)

def parse_slop_list(raw: str | None) -> list[str]:
    """Split the comma-separated slop setting into distinct, lowercased terms."""
    terms: list[str] = []
    for part in (raw or "").split(","):
        term = part.strip().lower()
        if term and term not in terms:
            terms.append(term)
    return terms


def _non_latin_matches(text: str) -> list[str]:
    """Distinct non-Latin characters found, in order of first appearance."""
    if not text:
        return []
    seen: list[str] = []
    for char in _NON_LATIN_RE.findall(text):
        if char not in seen:
            seen.append(char)
    return seen


def slop_hits(text: str, slop_list: str | None) -> list[str]:
    lowered = (text or "").lower()
    if not lowered:
        return []
    return [term for term in parse_slop_list(slop_list) if term in lowered]


def _fmt_ms(value: int | float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{int(value)}ms"


def score_check(
    *,
    ok: bool,
    status_code: int = 0,
    ttft_ms: int | None = None,
    total_ms: int | None = None,
    text: str = "",
    penalize_symbols: bool = True,
    slop_list: str | None = None,
    max_ttft_ms: int | None = None,
    max_total_ms: int | None = None,
    error: str | None = None,
) -> tuple[float, list[dict[str, Any]]]:
    """Return ``(score, penalties)`` for one probe, score clamped to 0-100.

    The score starts at 100 and each finding subtracts points. A failed request
    is an automatic zero, since nothing about it is worth averaging.
    """
    if not ok:
        reason = error or (f"Upstream returned HTTP {status_code}." if status_code else "Request failed.")
        return 0.0, [{"reason": reason, "points": 100.0}]

    score = 100.0
    penalties: list[dict[str, Any]] = []

    if max_ttft_ms is not None and max_ttft_ms >= 0 and ttft_ms is not None and ttft_ms > max_ttft_ms:
        over = (ttft_ms - max_ttft_ms) / max(max_ttft_ms, 1)
        points = min(40.0, 40.0 * over)
        penalties.append(
            {
                "reason": f"First token at {_fmt_ms(ttft_ms)}, over the {_fmt_ms(max_ttft_ms)} budget",
                "points": round(points, 1),
            }
        )
        score -= points

    if max_total_ms is not None and max_total_ms >= 0 and total_ms is not None and total_ms > max_total_ms:
        over = (total_ms - max_total_ms) / max(max_total_ms, 1)
        points = min(30.0, 30.0 * over)
        penalties.append(
            {
                "reason": f"Total response time {_fmt_ms(total_ms)}, over the {_fmt_ms(max_total_ms)} budget",
                "points": round(points, 1),
            }
        )
        score -= points

    if penalize_symbols:
        symbols = _non_latin_matches(text)
        if symbols:
            points = min(25.0, 15.0 + 5.0 * (len(symbols) - 1))
            penalties.append(
                {
                    "reason": "Non-Latin symbols: " + " ".join(symbols[:10]),
                    "points": round(points, 1),
                }
            )
            score -= points

    terms = slop_hits(text, slop_list)
    if terms:
        points = min(25.0, 10.0 * len(terms))
        penalties.append(
            {
                "reason": "Slop word(s): " + ", ".join(terms[:10]),
                "points": round(points, 1),
            }
        )
        score -= points

    return round(max(0.0, min(100.0, score)), 1), penalties


def _chat_url(route) -> str:
    target = (route["target_host"] or "https://api.openai.com").rstrip("/")
    if target.endswith("/v1"):
        return f"{target}/chat/completions"
    return f"{target}/v1/chat/completions"


def _headers(route, stream: bool = True) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "Accept-Encoding": "identity",
    }
    key = (route["upstream_key"] or "").strip()
    if key:
        if "anthropic" in (route["target_host"] or "").lower():
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def run_recorder(app, recorder) -> dict[str, Any]:
    """Probe one recorder once, store the check and reschedule it.

    Never raises: a background check that blows up must not kill the scheduler.
    """
    db = app.extensions["midware_db"]
    now = utcnow()
    hardcore = parse_json_object(recorder["hardcore_json"])
    route = db.get_route(recorder["route_id"]) if recorder["route_id"] else None
    if route is None:
        return _store_failure(
            db,
            recorder,
            "No route is configured for this recorder.",
            status_code=0,
            now=now,
            hardcore=hardcore,
        )

    model = recorder["model"] or ""
    messages: list[dict[str, str]] = []
    if (recorder["system_prompt"] or "").strip():
        messages.append({"role": "system", "content": recorder["system_prompt"]})
    messages.append({"role": "user", "content": recorder["user_prompt"] or DEFAULT_USER_PROMPT})
    body = {"model": model, "stream": True, "messages": messages}

    client = app.extensions.get("midware_http")
    if client is None:
        return _store_failure(
            db, recorder, "No HTTP client is available.", status_code=0, now=now, hardcore=hardcore
        )

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    started = time.perf_counter()
    try:
        request = client.build_request(
            "POST", _chat_url(route), content=payload, headers=_headers(route)
        )
        response = client.send(request, stream=True)
    except Exception as exc:  # a probe must never escape into the scheduler
        error = f"{type(exc).__name__}: {exc}"
        _maybe_log_probe(
            app,
            db,
            route,
            recorder,
            request_body=payload,
            response_body=None,
            status_code=502,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=error,
            model=model,
        )
        return _store_failure(db, recorder, error, status_code=0, now=now, hardcore=hardcore)

    if response.status_code >= 400:
        raw = _safe_content(response)
        _safe_close(response)
        excerpt = truncate(_decode(raw), 1000)
        _maybe_log_probe(
            app,
            db,
            route,
            recorder,
            request_body=payload,
            response_body=raw,
            status_code=response.status_code,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=f"HTTP {response.status_code}",
            model=model,
        )
        return _store_failure(
            db,
            recorder,
            f"Upstream returned HTTP {response.status_code}.",
            status_code=response.status_code,
            now=now,
            response_excerpt=excerpt,
            hardcore=hardcore,
        )

    limit = int(app.config.get("MAX_CAPTURE_BYTES", 8 * 1024 * 1024))
    stall_ms = int(app.config.get("MSQ_STALL_MS", 5000))
    buffer = bytearray()
    audit = StreamAudit(stall_ms=stall_ms)
    ttft_ms: int | None = None
    stream_error: str | None = None
    try:
        for chunk in response.iter_bytes():
            if not chunk:
                continue
            if len(buffer) < limit:
                buffer.extend(chunk[: limit - len(buffer)])
            audit.feed(chunk)
            if ttft_ms is None and audit.first_token:
                ttft_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        stream_error = f"{type(exc).__name__}: {exc}"
    _safe_close(response)

    total_ms = int((time.perf_counter() - started) * 1000)
    text = _decode(completion_text(bytes(buffer)))
    analysis_text = text
    if text.strip():
        # ``completion_text`` joins streamed chunks with newlines, which can split
        # words; rebuild both streams cleanly from the conversation parser and
        # analyse the visible answer (plus reasoning only when asked to).
        visible, reasoning = split_response_text(bytes(buffer))
        if recorder["ignore_thinking"]:
            analysis_text = visible if visible.strip() else text
        else:
            combined = "\n\n".join(part for part in (visible, reasoning) if part.strip())
            analysis_text = combined if combined.strip() else text

    base_score, base_findings = score_check(
        ok=stream_error is None,
        status_code=response.status_code,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        text=analysis_text,
        penalize_symbols=bool(recorder["penalize_symbols"]),
        slop_list=recorder["slop_list"],
        max_ttft_ms=recorder["max_ttft_ms"],
        max_total_ms=recorder["max_total_ms"],
        error=stream_error,
    )

    extra_findings, hard_fail = _hardcore_findings(
        app, route, recorder, hardcore, audit, analysis_text, text, stream_error
    )
    findings = list(base_findings) + extra_findings

    if ttft_ms is None and text.strip() and not stream_error:
        # Non-streaming upstreams give no separate first-token timing; the total
        # is the closest honest measure of time-until-anything.
        ttft_ms = total_ms

    completion_tokens = 0
    generation_ms: int | None = None
    tokens_per_second: float | None = None
    if text.strip():
        completion_tokens, _ = count_text(text, model)
        if ttft_ms is not None:
            generation_ms = max(total_ms - ttft_ms, 0)
            if generation_ms > 0:
                tokens_per_second = completion_tokens / (generation_ms / 1000)

    extra = sum(float(item.get("points", 0.0)) for item in findings) - sum(
        float(item.get("points", 0.0)) for item in base_findings
    )
    if hard_fail or stream_error:
        score = 0.0
    else:
        score = round(max(0.0, min(100.0, base_score - max(extra, 0.0))), 1)

    ok = not hard_fail and stream_error is None and bool(text.strip())
    error = stream_error
    if not text.strip() and stream_error is None:
        error = "The model returned an empty response."
    _maybe_log_probe(
        app,
        db,
        route,
        recorder,
        request_body=payload,
        response_body=bytes(buffer),
        status_code=response.status_code,
        latency_ms=total_ms,
        error=stream_error,
        model=model,
    )
    return _record(
        db,
        recorder,
        status_code=response.status_code,
        ok=ok,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        generation_ms=generation_ms,
        completion_tokens=completion_tokens,
        tokens_per_second=tokens_per_second,
        score=score,
        hard_fail=hard_fail,
        findings=findings,
        excerpt=truncate(text, 2000) if text else None,
        error=error,
        now=now,
    )


def _hardcore_findings(app, route, recorder, hardcore, audit, analysis_text, full_text, stream_error):
    """Run the opt-in hardcore checks and return ``(findings, hard_fail)``.

    ``analysis_text`` is what quality checks look at (visible answer only when
    thinking is ignored); ``full_text`` is the whole completion, used purely to
    decide whether the model answered at all.
    """
    findings: list[dict[str, Any]] = []
    hard_fail = False

    investigate = bool(hardcore.get("stream_integrity"))
    if investigate:
        integrity, hard = stream_integrity_findings(audit, app.config.get("MSQ_STALL_MS", 5000))
        findings += integrity
        hard_fail = hard_fail or hard
    if stream_error and investigate:
        hard_fail = True
    if not full_text.strip() and not stream_error:
        findings.append(finding("The model returned an empty response.", 100.0, "empty", False))
        if investigate:
            hard_fail = True
    if analysis_text and hardcore.get("text_forensics"):
        findings += text_forensics_findings(analysis_text)
    if hardcore.get("needle") and not stream_error:
        needle, needle_hard = _run_needle(app, route, recorder, hardcore)
        findings += needle
        hard_fail = hard_fail or needle_hard
    return findings, hard_fail


def _run_needle(app, route, recorder, hardcore) -> tuple[list[dict[str, Any]], bool]:
    db = app.extensions["midware_db"]
    client = app.extensions.get("midware_http")
    if client is None:
        return [finding("Needle check skipped: no HTTP client", 0.0, "needle", False)], False
    context_chars = int(
        hardcore.get("needle_context_chars") or app.config.get("MSQ_NEEDLE_CONTEXT_CHARS", 4000)
    )
    code, prompt = build_needle_prompt(context_chars)
    body = {
        "model": recorder["model"] or "",
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    started = time.perf_counter()
    try:
        request = client.build_request(
            "POST",
            _chat_url(route),
            content=payload,
            headers=_headers(route, stream=False),
        )
        response = client.send(request, stream=False)
    except Exception as exc:
        error = f"Needle check errored: {type(exc).__name__}: {exc}"
        _maybe_log_probe(
            app,
            db,
            route,
            recorder,
            request_body=payload,
            response_body=None,
            status_code=502,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error=error,
            model=recorder["model"] or "",
            streamed=False,
            enabled=bool(hardcore.get("collect_needle")),
        )
        return [finding(error, 100.0, "needle", False)], True

    raw = _safe_content(response)
    status_code = response.status_code
    _safe_close(response)
    _maybe_log_probe(
        app,
        db,
        route,
        recorder,
        request_body=payload,
        response_body=raw,
        status_code=status_code,
        latency_ms=int((time.perf_counter() - started) * 1000),
        error=f"HTTP {status_code}" if status_code >= 400 else None,
        model=recorder["model"] or "",
        streamed=False,
        enabled=bool(hardcore.get("collect_needle")),
    )
    if status_code >= 400:
        return [finding(f"Needle check failed: HTTP {status_code}", 100.0, "needle", False)], True
    return needle_findings(code, _decode(completion_text(raw)))


def _maybe_log_probe(
    app,
    db,
    route,
    recorder,
    *,
    request_body: Any,
    response_body: Any,
    status_code: int,
    latency_ms: int,
    error: str | None,
    model: str,
    streamed: bool = True,
    request_path: str = MSQ_REQUEST_PATH,
    enabled: bool | None = None,
) -> None:
    """File a collected probe under the automatic MSQ key as a normal request.

    ``enabled`` overrides the recorder-wide ``collect_requests`` flag for one call
    (the needle probe is only collected when its own toggle is on too). Best-effort:
    history bookkeeping must never break the probe itself.
    """
    wanted = bool(recorder["collect_requests"]) if enabled is None else bool(enabled)
    if not (wanted and recorder["collect_requests"]):
        return
    try:
        api_key_id = db.get_or_create_api_key(MSQ_API_KEY_NAME, "Automatic MSQ measurements")
    except Exception:
        return

    limit = int(app.config.get("MAX_CAPTURE_BYTES", 8 * 1024 * 1024))
    request_body = truncate(_decode(request_body), limit)
    response_body = truncate(_decode(response_body), limit)

    prompt = completion = total = 0
    token_source = "none"
    if status_code < 400:
        usage = usage_from_stream(response_body) if streamed else usage_from_response(response_body)
        if usage is not None and (usage.prompt_tokens or usage.completion_tokens or usage.total_tokens):
            prompt, completion, total = usage.prompt_tokens, usage.completion_tokens, usage.total_tokens
            token_source = "upstream"
        else:
            estimate = estimate_usage(request_body, response_body, model)
            if estimate.total_tokens:
                prompt, completion, total = (
                    estimate.prompt_tokens,
                    estimate.completion_tokens,
                    estimate.total_tokens,
                )
                token_source = estimate.source

    try:
        db.record_request(
            api_key_id=api_key_id,
            route_id=route["id"],
            method="POST",
            request_path=request_path,
            model=model,
            status_code=status_code,
            latency_ms=int(latency_ms or 0),
            streamed=streamed,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            token_source=token_source,
            error=error,
            request_body=request_body,
            response_body=response_body,
            model_raw=None,
            host_prefix=None,
        )
        db.touch_api_key(api_key_id)
        db.prune_requests(api_key_id)
    except Exception:
        pass


def _record(
    db,
    recorder,
    *,
    status_code: int,
    ok: bool,
    ttft_ms: int | None,
    total_ms: int | None,
    generation_ms: int | None = None,
    completion_tokens: int = 0,
    tokens_per_second: float | None = None,
    score: float,
    hard_fail: bool,
    findings: list[dict[str, Any]],
    excerpt: str | None,
    error: str | None,
    now: datetime,
) -> dict[str, Any]:
    db.record_msq_check(
        recorder_id=recorder["id"],
        status_code=status_code,
        ok=ok,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        generation_ms=generation_ms,
        completion_tokens=completion_tokens,
        tokens_per_second=tokens_per_second,
        score=score,
        hard_fail=hard_fail,
        penalties=findings,
        response_excerpt=excerpt,
        error=error,
    )
    db.mark_msq_ran(recorder["id"], now)
    return {
        "recorder_id": recorder["id"],
        "ok": ok,
        "hard_fail": hard_fail,
        "status_code": status_code,
        "score": score,
        "ttft_ms": ttft_ms,
        "total_ms": total_ms,
        "penalties": findings,
        "error": error,
    }


def _safe_content(response) -> bytes:
    try:
        return response.content or b""
    except Exception:
        return b""


def _safe_close(response) -> None:
    try:
        response.close()
    except Exception:
        pass


def _decode(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return str(raw)


def _store_failure(
    db,
    recorder,
    error: str,
    *,
    status_code: int,
    now: datetime,
    total_ms: int | None = None,
    ttft_ms: int | None = None,
    response_excerpt: str | None = None,
    hardcore: dict | None = None,
) -> dict[str, Any]:
    score, findings = score_check(ok=False, status_code=status_code, error=error)
    hard_fail = bool((hardcore or {}).get("stream_integrity"))
    return _record(
        db,
        recorder,
        status_code=status_code,
        ok=False,
        ttft_ms=ttft_ms,
        total_ms=total_ms,
        score=score,
        hard_fail=hard_fail,
        findings=findings,
        excerpt=response_excerpt,
        error=error,
        now=now,
    )


def _within(check, now: datetime, hours: float) -> bool:
    seen = parse_iso(check["created_at"])
    if seen is None:
        return False
    return seen >= now - timedelta(hours=max(hours, 0))


def _hard_failed(check) -> bool:
    """Read the ``hard_fail`` flag, tolerating rows/dicts from before it existed."""
    try:
        return bool(check["hard_fail"])
    except (KeyError, IndexError, TypeError):
        return False


def evaluate_recorder(
    recorder,
    checks,
    *,
    examine_hours: float = 24.0,
    min_checks: int = 3,
    degraded_threshold: float = 70.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarise a recorder from its checks: examining, degraded or working.

    ``checks`` is expected oldest-first. Until the recorder is old enough *and*
    has enough samples, no verdict is given, so a model is never declared
    degraded off a single bad minute.
    """
    now = now or utcnow()
    created = parse_iso(recorder["created_at"]) or now
    age_hours = max((now - created).total_seconds() / 3600.0, 0.0)
    total = len(checks)
    window = [check for check in checks if _within(check, now, examine_hours)] or checks
    scores = [float(check["score"]) for check in window]
    average = statistics.fmean(scores) if scores else None
    latest = checks[-1] if checks else None
    hard_failed = latest is not None and _hard_failed(latest)

    # A hard failure is reported immediately rather than waiting out the
    # examination window: "the stream truncated" is not a statistical question.
    if hard_failed:
        status = FAILING
    elif age_hours < examine_hours or total < min_checks or average is None:
        status = EXAMINING
    elif average < degraded_threshold:
        status = DEGRADED
    else:
        status = WORKING

    return {
        "status": status,
        "label": STATUS_LABELS[status],
        "average_score": round(average, 1) if average is not None else None,
        "sample_count": len(window),
        "total_checks": total,
        "age_hours": round(age_hours, 1),
        "hard_fail": hard_failed,
        "latest": latest,
    }


def chart_point(check, index: int) -> dict[str, Any]:
    """Render one check into the JSON shape the chart consumes."""
    created = parse_iso(check["created_at"])
    penalties: list[dict[str, Any]] = []
    if check["penalties"]:
        try:
            loaded = json.loads(check["penalties"])
            if isinstance(loaded, list):
                penalties = loaded
        except (ValueError, TypeError):
            penalties = []
    return {
        "i": index,
        "t": int(created.timestamp() * 1000) if created else 0,
        "score": float(check["score"]),
        "ok": bool(check["ok"]),
        "hard_fail": _hard_failed(check),
        "ttft_ms": check["ttft_ms"],
        "total_ms": check["total_ms"],
        "generation_ms": check["generation_ms"],
        "completion_tokens": int(check["completion_tokens"] or 0),
        "tokens_per_second": check["tokens_per_second"],
        "penalties": penalties,
        "error": check["error"],
    }


_scheduler_lock = threading.Lock()
_scheduler_started = False


def run_due(app, now: datetime | None = None) -> list[dict[str, Any]]:
    """Run every recorder whose next check is due. Used by the scheduler and tests."""
    db = app.extensions["midware_db"]
    now = now or utcnow()
    results: list[dict[str, Any]] = []
    for recorder in db.due_msq_recorders(now):
        if not db.claim_msq_recorder(recorder["id"], now):
            continue
        results.append(run_recorder(app, recorder))
    return results


def start_scheduler(app) -> threading.Thread | None:
    """Start the daemon probe thread once per process; a no-op under tests."""
    global _scheduler_started
    if app.config.get("TESTING") or not app.config.get("MSQ_ENABLED", True):
        return None
    with _scheduler_lock:
        if _scheduler_started:
            return None
        _scheduler_started = True
    thread = threading.Thread(
        target=_scheduler_loop, args=(app,), name="midware-msq", daemon=True
    )
    thread.start()
    return thread


def _scheduler_loop(app) -> None:
    interval = max(5, int(app.config.get("MSQ_SCHEDULER_INTERVAL", 60)))
    while True:
        time.sleep(interval)
        try:
            with app.app_context():
                run_due(app)
        except Exception:  # a bad iteration must not stop future checks
            app.logger.warning("MidWare MSQ scheduler iteration failed", exc_info=True)
