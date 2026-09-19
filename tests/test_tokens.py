from __future__ import annotations

from midware.tokens import (
    completion_text,
    count_text,
    estimate_usage,
    prompt_text,
)


def test_count_text_empty_is_zero():
    assert count_text("") == (0, "estimated")


def test_count_text_uses_tiktoken_for_openai_models():
    tokens, source = count_text("hello world " * 100, "gpt-4o-mini")
    assert source == "tiktoken"
    assert tokens > 0


def test_count_text_falls_back_to_chars_for_unknown_models():
    tokens, source = count_text("hello world " * 100, "some-local-model")
    assert source == "chars"
    assert tokens > 0


def test_prompt_text_flattens_messages_and_roles():
    body = {
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": [{"type": "text", "text": "Hi there"}]},
        ]
    }
    text = prompt_text(body)
    assert "Be brief." in text
    assert "Hi there" in text
    assert "system" in text


def test_completion_text_reads_non_streamed_choices():
    body = {"choices": [{"message": {"role": "assistant", "content": "All done"}}]}
    assert "All done" in completion_text(body)


def test_completion_text_reads_sse_transcript():
    sse = (
        'data: {"choices":[{"delta":{"reasoning":"thinking "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        'data: [DONE]\n\n'
    )
    text = completion_text(sse)
    assert "Hello" in text
    assert "world" in text
    assert "thinking" in text


def test_estimate_usage_sums_both_sides():
    request = {"messages": [{"role": "user", "content": "Explain gravity in one sentence."}]}
    response = {"choices": [{"message": {"role": "assistant", "content": "It pulls things down."}}]}
    estimate = estimate_usage(request, response, "gpt-4o-mini")
    assert estimate.prompt_tokens > 0
    assert estimate.completion_tokens > 0
    assert estimate.total_tokens == estimate.prompt_tokens + estimate.completion_tokens
    assert estimate.source == "tiktoken"


def test_estimate_usage_of_empty_bodies_is_zero():
    estimate = estimate_usage({}, {}, "gpt-4o-mini")
    assert estimate.total_tokens == 0
