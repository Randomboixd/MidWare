"""The request inspector's page model: what it reads out of the stored bytes."""

from __future__ import annotations

import json

from midware.inspector import (
    INLINE_LIMIT,
    body_kind,
    build_page,
    cache_split,
    preview,
    request_facts,
    response_facts,
    usage_detail,
)


def sse(*events: dict) -> str:
    lines = [f"data: {json.dumps(event)}" for event in events]
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def openai_usage(**extra) -> dict:
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 250,
        "total_tokens": 1250,
        "prompt_tokens_details": {"cached_tokens": 800},
        "completion_tokens_details": {"reasoning_tokens": 40},
        "cost": 0.0123,
        "currency": "USD",
    }
    usage.update(extra)
    return usage


def test_body_kind_classifies_transports():
    assert body_kind(None) == "empty"
    assert body_kind("") == "empty"
    assert body_kind('{"a": 1}') == "json"
    assert body_kind('{"a": ') == "json-partial"
    assert body_kind('data: {"a": 1}\n\n') == "sse"
    assert body_kind("<!doctype html><p>x") == "html"
    assert body_kind("plain words") == "text"


def test_usage_detail_reads_an_openai_usage_block():
    body = json.dumps({"choices": [], "usage": openai_usage()})
    detail = usage_detail(body)

    assert detail["reported"] is True
    assert (detail["prompt"], detail["completion"], detail["total"]) == (1000, 250, 1250)
    assert detail["cached"] == 800
    assert detail["uncached"] == 200
    assert detail["reasoning"] == 40
    assert detail["cost"] == 0.0123
    assert detail["currency"] == "USD"
    assert detail["cache_hit"] is not None and abs(detail["cache_hit"] - 80.0) < 0.01


def test_usage_detail_takes_the_last_stream_event():
    """A stream repeats ``usage``; only the final event carries the totals."""
    body = sse(
        {"choices": [], "usage": openai_usage(prompt_tokens=10, total_tokens=10)},
        {"choices": [], "usage": openai_usage()},
    )
    detail = usage_detail(body)

    assert detail["prompt"] == 1000
    assert detail["cached"] == 800


def test_usage_detail_reads_anthropic_and_gemini_dialects():
    anthropic = json.dumps(
        {
            "usage": {
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_read_input_tokens": 400,
                "cache_creation_input_tokens": 60,
            }
        }
    )
    detail = usage_detail(anthropic)
    assert (detail["prompt"], detail["completion"], detail["total"]) == (500, 100, 600)
    assert detail["cached"] == 400
    assert detail["cache_write"] == 60

    gemini = json.dumps(
        {
            "usageMetadata": {
                "promptTokenCount": 300,
                "candidatesTokenCount": 40,
                "totalTokenCount": 340,
                "cachedContentTokenCount": 120,
                "thoughtsTokenCount": 12,
            }
        }
    )
    detail = usage_detail(gemini)
    assert detail["prompt"] == 300
    assert detail["cached"] == 120
    assert detail["reasoning"] == 12


def test_usage_detail_reads_the_opencode_go_pocket():
    """Go bills a subscription: its own cost is 0, the list price is the figure."""
    body = sse(
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 50,
                "total_tokens": 550,
                "cost": 0,
                "prompt_tokens_details": {"cached_tokens": 400},
                "opencode": {
                    "billing_source": "go",
                    "list_cost": 0.0199,
                    "allowance": 0.08,
                    "tokens": {"input": 100, "output": 50, "reasoning": 5, "cache_read": 400},
                },
            },
        }
    )
    detail = usage_detail(body)

    assert detail["cost"] == 0
    assert detail["list_cost"] == 0.0199
    assert detail["billing_source"] == "go"
    assert detail["allowance"] == 0.08
    assert detail["uncached"] == 100
    assert detail["reasoning"] == 5


def test_usage_detail_reports_nothing_rather_than_zero_when_absent():
    """A missing figure is ``None``: a zero would read as a measurement."""
    detail = usage_detail(json.dumps({"choices": []}))

    assert detail["reported"] is False
    assert detail["cached"] is None
    assert detail["cost"] is None
    assert detail["cache_hit"] is None


def test_request_facts_reads_sampling_parameters():
    body = json.dumps(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}] * 3,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 4096,
            "temperature": 0.2,
            "tools": [{"type": "function"}] * 2,
        }
    )
    facts = request_facts(body)

    assert facts["messages"] == 3
    assert facts["tools"] == 2
    assert facts["stream"] is True
    assert facts["usage_requested"] is True
    assert facts["max_tokens"] == 4096
    assert facts["temperature"] == 0.2


def test_request_facts_survives_a_body_that_is_not_json():
    facts = request_facts("--boundary\nContent-Disposition: form-data; name='prompt'")

    assert facts["kind"] == "multipart"
    assert facts["messages"] is None


def test_response_facts_counts_stream_events_and_finish_reason():
    body = sse(
        {"choices": [{"delta": {"content": "he"}}], "model": "m"},
        {"choices": [{"delta": {"content": "llo"}, "finish_reason": "stop"}]},
    )
    facts = response_facts(body, streamed=True)

    assert facts["kind"] == "sse"
    assert facts["events"] == 2
    assert facts["finish_reasons"] == ["stop"]
    assert facts["model"] == "m"
    assert facts["truncated"] is False


def test_preview_reformats_json_and_flags_it():
    text, clipped, reformatted = preview('{"a":1}', "json")

    assert "\n  " in text
    assert clipped is False
    assert reformatted is True


def test_preview_leaves_a_stream_on_one_line():
    body = sse({"a": 1})
    text, clipped, reformatted = preview(body, "sse")

    assert text == body
    assert reformatted is False


def test_preview_clips_a_body_over_the_inline_limit():
    text, clipped, _ = preview("x" * (INLINE_LIMIT + 10), "text")

    assert clipped is True
    assert len(text) == INLINE_LIMIT


def test_cache_split_reads_anthropic_ttl_buckets():
    body = json.dumps(
        {
            "usage": {
                "input_tokens": 10,
                "cache_creation": {"ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 40},
            }
        }
    )
    assert cache_split(usage_detail(body), body) == {"5m": 30, "1h": 40}


def test_build_page_falls_back_to_the_logged_columns():
    """A response with no usage block still shows the counts the proxy logged."""
    row = {
        "request_body": '{"messages": []}',
        "response_body": json.dumps({"choices": []}),
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "streamed": 0,
        "token_source": "estimate:tiktoken",
    }
    page = build_page(row)

    assert page["usage"]["total"] == 15
    assert page["usage"]["reported"] is False
    assert page["estimated"] is True
    assert page["request_kind"] == "json"


def test_build_page_marks_an_estimate_and_a_captured_body():
    row = {
        "request_body": "not json at all",
        "response_body": "",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "streamed": 1,
        "token_source": "upstream",
    }
    page = build_page(row)

    assert page["estimated"] is False
    assert page["response_kind"] == "empty"
    assert page["response_bytes"] == 0
