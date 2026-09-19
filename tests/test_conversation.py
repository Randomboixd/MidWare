from __future__ import annotations

import json

from midware.conversation import build_conversation

RESPONSE = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "xiaomi/mimo-v2.5-pro:thinking",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello there.",
                "reasoning": "The user said hi, so greet them.",
                "reasoning_details": [
                    {"type": "reasoning.text", "text": "The user", "format": "unknown", "index": 0},
                    {"type": "reasoning.text", "text": " said hi.", "format": "unknown", "index": 0},
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def test_builds_request_and_response_messages():
    request = {"model": "m", "messages": [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hi"},
    ]}
    convo = build_conversation(json.dumps(request), json.dumps(RESPONSE))
    roles = [m["role"] for m in convo["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert [m["number"] for m in convo["messages"]] == [1, 2, 3]
    assert [m["source"] for m in convo["messages"]] == ["request", "request", "response"]
    assert convo["messages"][1]["text"] == "Hi"
    assert convo["messages"][2]["text"] == "Hello there."


def test_reasoning_variants_are_collected_separately():
    request = {"messages": [{"role": "user", "content": "Hi"}]}
    convo = build_conversation(json.dumps(request), json.dumps(RESPONSE))
    assistant = convo["messages"][-1]
    # ``reasoning`` plus the concatenated ``reasoning_details`` fragments.
    assert len(assistant["reasoning"]) == 2
    assert "greet them" in assistant["reasoning"][0]
    assert "The user said hi." in assistant["reasoning"][1]
    assert "The user said hi." not in assistant["text"]


def test_multiple_reasoning_field_presets_all_kept():
    response = {
        "choices": [{"message": {
            "role": "assistant",
            "content": "Answer",
            "reasoning_content": "preset one",
            "thinking": "preset two",
        }}]
    }
    convo = build_conversation(json.dumps({"messages": []}), json.dumps(response))
    assistant = convo["messages"][0]
    assert assistant["reasoning"] == ["preset one", "preset two"]


def test_anthropic_content_parts_split_thinking_and_text():
    response = {"content": [
        {"type": "thinking", "thinking": "let me think"},
        {"type": "text", "text": "Final answer"},
    ]}
    convo = build_conversation(None, json.dumps(response))
    assistant = convo["messages"][0]
    assert assistant["text"] == "Final answer"
    assert assistant["reasoning"] == ["let me think"]


def test_multimodal_parts_flatten():
    request = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "What is this?"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
    ]}]}
    convo = build_conversation(json.dumps(request), None)
    assert "What is this?" in convo["messages"][0]["text"]
    assert "[image]" in convo["messages"][0]["text"]


def test_sse_stream_is_rebuilt():
    sse = (
        'data: {"choices":[{"delta":{"reasoning":"step one "}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning":"step two"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n'
    )
    convo = build_conversation(None, sse)
    assert len(convo["messages"]) == 1
    assistant = convo["messages"][0]
    assert assistant["text"] == "Hello world"
    assert assistant["reasoning"] == ["step one step two"]


def test_unknown_body_yields_no_messages():
    convo = build_conversation("not json", "")
    assert convo["messages"] == []
    assert convo["raw_available"] is True


def test_empty_bodies_have_no_raw_available():
    convo = build_conversation("", "")
    assert convo["messages"] == []
    assert convo["raw_available"] is False


def test_tool_calls_surface():
    response = {"choices": [{"message": {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}],
    }}]}
    convo = build_conversation(None, json.dumps(response))
    assistant = convo["messages"][0]
    assert assistant["tool_calls"] == ['get_weather({"city":"Paris"})']
    assert assistant["empty"] is False
