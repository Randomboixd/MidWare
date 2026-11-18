from __future__ import annotations

import json

import httpx

from test_proxy import StubResponse, auth_header, install_stub


COOKIE_VALUE = "midware_session=signed-admin-session"
FORWARDED_FOR = "203.0.113.9"


def _lowered(headers) -> dict[str, str]:
    return {str(name).lower(): value for name, value in headers.items()}


# -- fix 1: client credentials and proxy plumbing never leave the box ----------


def test_client_credentials_never_reach_the_upstream(configured):
    app, token = configured
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=b'{"usage": {"total_tokens": 1}}'))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": []},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Midware-Key": token,
            "Cookie": COOKIE_VALUE,
            "X-Forwarded-For": FORWARDED_FOR,
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "midware.example",
            "X-Forwarded-Port": "8443",
            "X-Forwarded-Ssl": "on",
            "Forwarded": f"for={FORWARDED_FOR}",
            "X-Real-Ip": FORWARDED_FOR,
            "X-Custom": "keep-me",
            "User-Agent": "midware-test-agent",
        },
    )

    assert response.status_code == 200
    upstream = _lowered(stub.sent[0][0]["headers"])
    assert upstream["authorization"] == "Bearer sk-upstream"
    for name in (
        "cookie",
        "x-midware-key",
        "x-api-key",
        "forwarded",
        "x-real-ip",
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-ssl",
    ):
        assert name not in upstream, f"{name} leaked upstream"
    assert token not in upstream.values()
    assert COOKIE_VALUE not in upstream.values()
    # Everything else still passes through untouched.
    assert upstream["x-custom"] == "keep-me"
    assert upstream["user-agent"] == "midware-test-agent"


def test_workflow_subcalls_never_see_client_credentials(configured):
    """The Origin's ``system_request`` variable feeds sub-call prompts (and the
    JavaScript sandbox); the same credential headers must be stripped there."""
    app, token = configured
    db = app.extensions["midware_db"]
    workflow = {
        "version": 1,
        "final": {"messages": [{"kind": "core_context"}], "model_override": ""},
        "nodes": [
            {
                "type": "api_request",
                "name": "inspect",
                "system_prompt": "You inspect.",
                "prompt": "Hdrs: {{ system_request }}",
                "timeout_seconds": 30,
            }
        ],
    }
    db.create_premodel(
        name="Sniff",
        slug="sniff",
        route_id=db.active_route()["id"],
        model="sniff-model",
        use_workflow=True,
        workflow_json=json.dumps(workflow),
    )
    sub_reply = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    final_body = {
        "id": "chatcmpl-final",
        "choices": [{"message": {"role": "assistant", "content": "Done."}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    stub = install_stub(
        app,
        StubResponse(content=json.dumps(final_body).encode()),
        posts=[StubResponse(json_body=sub_reply)],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-sniff", "messages": [{"role": "user", "content": "Hi"}]},
        headers={
            "Authorization": f"Bearer {token}",
            "X-Midware-Key": token,
            "Cookie": COOKIE_VALUE,
            "X-Forwarded-For": FORWARDED_FOR,
        },
    )

    assert response.status_code == 200
    url, sub_payload, sub_headers, timeout = stub.posts[0]
    sub_prompt = sub_payload["messages"][1]["content"]
    assert "Hdrs:" in sub_prompt
    assert COOKIE_VALUE not in sub_prompt
    assert token not in sub_prompt
    assert FORWARDED_FOR not in sub_prompt
    # The request *description* (method/path) still travels; only credentials
    # and proxy plumbing are stripped.
    assert "chat/completions" in sub_prompt


# -- fix 2: aborted streams are settled once, and honestly ---------------------


class _DyingStream(StubResponse):
    """A stub upstream that raises mid-stream after delivering its chunks."""

    def __init__(self, chunks, exc):
        super().__init__(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            stream_chunks=chunks,
        )
        self._exc = exc

    def iter_bytes(self):
        for chunk in self._chunks:
            yield chunk
        raise self._exc


def _streamed_call(client, token, body=None):
    return client.post(
        "/v1/chat/completions",
        json=body or {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "Say hello."}]},
        headers=auth_header(token),
    )


def test_client_disconnect_midstream_settles_the_request_row(configured):
    app, token = configured
    client = app.test_client()
    chunks = [
        b'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{"content":"llo"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    install_stub(app, StubResponse(stream_chunks=chunks, headers={"content-type": "text/event-stream"}))

    response = _streamed_call(client, token)
    iterator = response.response
    assert next(iterator)  # the first chunk is on the wire
    response.close()  # the client hangs up mid-stream

    db = app.extensions["midware_db"]
    rows = db.list_requests(limit=5)
    assert len(rows) == 1, "a disconnected client must still get exactly one request row"
    row = rows[0]
    assert row["streamed"] == 1
    assert row["error"] == "client_disconnected"
    assert "data:" in (row["response_body"] or "")


def test_upstream_error_midstream_records_failure_and_signals_client(configured):
    app, token = configured
    client = app.test_client()
    chunks = [
        b'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{"content":"he"}}]}\n\n',
    ]
    install_stub(app, _DyingStream(chunks, httpx.ReadTimeout("read timed out")))

    response = _streamed_call(client, token)
    delivered = b"".join(response.response)

    # A live client must be able to tell truncation from a clean finish.
    assert b'data: {"error"' in delivered
    assert b"ReadTimeout" in delivered

    db = app.extensions["midware_db"]
    rows = db.list_requests(limit=5)
    assert len(rows) == 1, "the broken stream must be settled exactly once"
    row = rows[0]
    assert row["status_code"] == 502
    assert row["streamed"] == 1
    assert "ReadTimeout" in row["error"]
    assert "data:" in (row["response_body"] or "")
    # An error row never carries fabricated usage.
    assert row["total_tokens"] == 0
    assert row["token_source"] == "none"


# -- fix 3: a keyless route never vouches the caller's MidWare key ------------


def test_keyless_route_sends_no_client_credential_upstream(multi_host):
    app, token = multi_host
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=b'{"usage": {"total_tokens": 1}}'))

    # 'Local' is a route saved without an upstream key; the caller authenticates
    # to MidWare either way, but their mw- key must not become the upstream
    # credential. Bearer style first:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "[Local]gpt-4o", "messages": []},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    upstream = _lowered(stub.sent[0][0]["headers"])
    assert "authorization" not in upstream
    assert "x-api-key" not in upstream
    assert token not in upstream.values()

    # x-api-key style caller, same refusal:
    response = client.post(
        "/v1/chat/completions",
        json={"model": "[Local]gpt-4o", "messages": []},
        headers={"x-api-key": token},
    )
    assert response.status_code == 200
    upstream = _lowered(stub.sent[1][0]["headers"])
    assert "authorization" not in upstream
    assert "x-api-key" not in upstream
    assert token not in upstream.values()
    assert "mw-" in token  # sanity: the credential really is a MidWare key


# -- fix 4: key and premodel request claims are all-or-nothing ------------------


def test_premodel_rejection_does_not_burn_key_request_quota(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    key_id = db.list_api_keys()[0]["id"]
    db.update_api_key(
        key_id, limit_requests_enabled=True, limit_requests_minute=True, max_requests_minute=2
    )
    db.create_premodel(
        name="Story",
        slug="story",
        route_id=db.active_route()["id"],
        model="story-model",
        limit_requests_enabled=True,
        limit_requests_minute=True,
        max_requests_minute=1,
    )
    install_stub(app, StubResponse(content=b"{}"))

    first = client.post("/v1/chat/completions", json={"model": "<p>-story", "messages": []}, headers=auth_header(token))
    second = client.post("/v1/chat/completions", json={"model": "<p>-story", "messages": []}, headers=auth_header(token))
    plain = client.post("/v1/chat/completions", json={"model": "gpt-4o", "messages": []}, headers=auth_header(token))

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.get_json()["error"]["code"] == "request_limit_exceeded"
    # The refused attempt never ate the key's quota: a plain call still goes through.
    assert plain.status_code == 200
    assert db.rate_usage_amounts(key_id, "requests", ("minute",))["minute"] == 2


def test_key_rejection_does_not_burn_premodel_request_quota(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    db.update_api_key(
        db.list_api_keys()[0]["id"],
        limit_requests_enabled=True,
        limit_requests_minute=True,
        max_requests_minute=1,
    )
    free_token = db.create_api_key("free")
    premodel_id = db.create_premodel(
        name="Tale",
        slug="tale",
        route_id=db.active_route()["id"],
        model="tale-model",
        limit_requests_enabled=True,
        limit_requests_minute=True,
        max_requests_minute=2,
    )
    client = app.test_client()
    install_stub(app, StubResponse(content=b"{}"))

    first = client.post("/v1/chat/completions", json={"model": "<p>-tale", "messages": []}, headers=auth_header(token))
    refused = client.post("/v1/chat/completions", json={"model": "<p>-tale", "messages": []}, headers=auth_header(token))
    after = client.post("/v1/chat/completions", json={"model": "<p>-tale", "messages": []}, headers=auth_header(free_token))

    assert first.status_code == 200
    assert refused.status_code == 429
    assert "API key" in refused.get_json()["error"]["message"]
    # The key's refusal never ate the premodel's quota: another key's call to
    # the same premodel still goes through.
    assert after.status_code == 200
    assert db.premodel_rate_usage_amounts(premodel_id, "requests", ("minute",))["minute"] == 2


def test_rate_usage_claim_group_is_all_or_nothing(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    key_id = db.list_api_keys()[0]["id"]
    premodel_id = db.create_premodel(
        name="Story", slug="story", route_id=db.active_route()["id"], model="m"
    )
    group = [("api_key", key_id, {"minute": 2}), ("premodel", premodel_id, {"minute": 1})]

    first = db.claim_rate_usage_group(group)
    assert first == [({"minute": 1}, True), ({"minute": 1}, True)]

    # The premodel window is at its cap, so the whole group rolls back: the
    # key keeps nothing from the refused attempt.
    second = db.claim_rate_usage_group(group)
    assert second[0][1] is True  # the key was not the rejector
    assert second[1][1] is False  # the premodel was
    assert second[1][0] == {"minute": 1}
    assert db.rate_usage_amounts(key_id, "requests", ("minute",))["minute"] == 1
    assert db.premodel_rate_usage_amounts(premodel_id, "requests", ("minute",))["minute"] == 1

    # An entry with no limits answers trivially and claims nothing.
    third = db.claim_rate_usage_group([("api_key", key_id, {}), ("premodel", premodel_id, {"minute": 1})])
    assert third == [({}, True), ({"minute": 1}, False)]
