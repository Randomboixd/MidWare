from __future__ import annotations

from midware.usage import (
    model_from_request_body,
    parse_sse_events,
    usage_from_response,
    usage_from_stream,
)


def test_usage_from_openai_non_stream():
    body = b'{"id":"x","model":"gpt-4o-mini","usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}'
    usage = usage_from_response(body)
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (10, 5, 15)
    assert usage.model == "gpt-4o-mini"


def test_usage_from_response_derives_missing_total():
    usage = usage_from_response('{"usage":{"prompt_tokens":7,"completion_tokens":3}}')
    assert usage.total_tokens == 10


def test_usage_from_anthropic_response():
    body = '{"model":"claude-3","usage":{"input_tokens":12,"output_tokens":8}}'
    usage = usage_from_response(body)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (12, 8, 20)


def test_usage_from_invalid_body_is_none():
    assert usage_from_response("not json") is None
    assert usage_from_response(None) is None
    assert usage_from_response("{}") is None


STREAM = (
    'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{"content":"hi"}}]}\n\n'
    'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{}}]}\n\n'
    'data: {"id":"1","model":"gpt-4o","usage":{"prompt_tokens":4,"completion_tokens":6,"total_tokens":10}}\n\n'
    "data: [DONE]\n\n"
)


def test_usage_from_stream_picks_final_usage():
    usage = usage_from_stream(STREAM)
    assert usage is not None
    assert usage.total_tokens == 10
    assert usage.model == "gpt-4o"


def test_parse_sse_events_ignores_done_and_comments():
    events = parse_sse_events(": ping\n\ndata: [DONE]\n\ndata: {\"a\":1}\n\n")
    assert events == [{"a": 1}]


def test_usage_from_anthropic_stream_merges_events():
    stream = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"model":"claude-3","usage":{"input_tokens":20,"output_tokens":1}}}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","usage":{"output_tokens":35}}\n\n'
    )
    usage = usage_from_stream(stream)
    assert usage.prompt_tokens == 20
    assert usage.completion_tokens == 35
    assert usage.total_tokens == 55
    assert usage.model == "claude-3"


def test_model_from_request_body():
    assert model_from_request_body(b'{"model":"llama3"}') == "llama3"
    assert model_from_request_body(b"garbage") is None


def test_parse_sse_events_joins_multiline_data():
    # A complete first line ends the event, so a following ``data: `` line
    # belongs to the next (here continuation-less) payload.
    stream = 'data: {"a":\ndata:  1}\n\n'
    assert parse_sse_events(stream) == [{"a": 1}]


def test_parse_sse_events_splits_unseparated_complete_payloads():
    stream = 'data: {"a":1}\ndata: {"b":2}'
    assert parse_sse_events(stream) == [{"a": 1}, {"b": 2}]


def test_parse_sse_events_keeps_unseparated_continuation():
    # A truncated capture can split one JSON value across lines; the first
    # fragment does not parse on its own so it must be joined, not discarded.
    stream = 'data: {"a":\ndata:   1}\ndata: ["x"]\n\n'
    assert parse_sse_events(stream) == [{"a": 1}, ["x"]]

