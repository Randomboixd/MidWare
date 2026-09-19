from __future__ import annotations

import json

import pytest


class StubResponse:
    def __init__(self, status_code=200, content=b"", headers=None, stream_chunks=None, json_body=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "application/json"}
        self._chunks = stream_chunks or []
        self._json = json_body

    def iter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.content or b"{}")

    def close(self):
        pass


class StubClient:
    def __init__(self, response, error=None):
        self.response = response
        self.error = error
        self.sent = []
        self.gets = []

    def build_request(self, method, url, content=None, headers=None):
        return {"method": method, "url": url, "content": content, "headers": headers}

    def send(self, request, stream=False):
        self.sent.append((request, stream))
        if self.error:
            raise self.error
        return self.response

    def get(self, url, headers=None):
        self.gets.append((url, headers))
        return self.response


def install_stub(app, response=None, error=None):
    stub = StubClient(response or StubResponse(), error)
    app.extensions["midware_http"] = stub
    return stub


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def test_proxy_requires_key(configured_client):
    client, _ = configured_client
    response = client.post("/v1/chat/completions", json={"model": "gpt-4o"})
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "missing_api_key"


def test_proxy_rejects_unknown_key(configured_client):
    client, _ = configured_client
    response = client.post("/v1/chat/completions", json={}, headers=auth_header("mw-nope"))
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_api_key"


def test_proxy_forwards_and_replaces_credentials(configured, monkeypatch):
    app, token = configured
    client = app.test_client()
    body = json.dumps(
        {
            "id": "chatcmpl-1",
            "model": "gpt-4o-mini",
            "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        }
    ).encode()
    stub = install_stub(app, StubResponse(status_code=200, content=body))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert response.get_json()["usage"]["total_tokens"] == 33
    assert response.headers["Server"] == "MidWare"

    upstream_request, stream = stub.sent[0]
    assert stream is False
    assert upstream_request["url"] == "https://api.example.com/chat/completions"
    assert upstream_request["headers"]["Authorization"] == "Bearer sk-upstream"

    db = app.extensions["midware_db"]
    row = db.list_requests(limit=1)[0]
    assert row["total_tokens"] == 33
    assert row["prompt_tokens"] == 11
    assert row["completion_tokens"] == 22
    assert row["model"] == "gpt-4o-mini"
    assert row["upstream_request_id"] == "chatcmpl-1"
    assert row["streamed"] == 0


def test_proxy_target_with_v1_suffix_does_not_duplicate(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    db.update_route(db.active_route()["id"], target_host="https://api.openai.com/v1")
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=b'{"usage":{"total_tokens":1}}'))

    client.post("/v1/chat/completions", json={"model": "m"}, headers=auth_header(token))

    assert stub.sent[0][0]["url"] == "https://api.openai.com/v1/chat/completions"


def test_proxy_streams_and_records_usage(configured):
    app, token = configured
    client = app.test_client()
    chunks = [
        b'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{"content":"he"}}]}\n\n',
        b'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{"content":"llo"}}]}\n\n',
        b'data: {"id":"1","model":"gpt-4o","usage":{"prompt_tokens":3,"completion_tokens":9,"total_tokens":12}}\n\n',
        b"data: [DONE]\n\n",
    ]
    install_stub(
        app,
        StubResponse(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            stream_chunks=chunks,
        ),
    )

    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "stream": True},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    delivered = b"".join(response.response)
    assert b"[DONE]" in delivered

    db = app.extensions["midware_db"]
    row = db.list_requests(limit=1)[0]
    assert row["streamed"] == 1
    assert row["total_tokens"] == 12
    assert row["model"] == "gpt-4o"
    assert row["token_source"] == "upstream"


def test_stream_without_usage_is_estimated_locally(configured):
    app, token = configured
    client = app.test_client()
    chunks = [
        b'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"id":"1","model":"gpt-4o","choices":[{"delta":{"content":" world"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    install_stub(
        app,
        StubResponse(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            stream_chunks=chunks,
        ),
    )

    b"".join(
        client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "Say hello world."}]},
            headers=auth_header(token),
        ).response
    )

    row = app.extensions["midware_db"].list_requests(limit=1)[0]
    assert row["total_tokens"] > 0
    assert row["prompt_tokens"] > 0
    assert row["completion_tokens"] > 0
    assert row["token_source"] in {"tiktoken", "chars"}


def test_non_streamed_response_without_usage_is_estimated(configured):
    app, token = configured
    client = app.test_client()
    body = b'{"id":"1","model":"gpt-4o","choices":[{"message":{"role":"assistant","content":"Hi there"}}]}'
    install_stub(app, StubResponse(status_code=200, content=body))

    client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "Say hi."}]},
        headers=auth_header(token),
    )

    row = app.extensions["midware_db"].list_requests(limit=1)[0]
    assert row["total_tokens"] > 0
    assert row["token_source"] in {"tiktoken", "chars"}


def test_error_response_never_gets_estimated_tokens(configured):
    app, token = configured
    client = app.test_client()
    install_stub(app, StubResponse(status_code=500, content=b'{"error":"boom"}'))

    client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "Say hi."}]},
        headers=auth_header(token),
    )

    row = app.extensions["midware_db"].list_requests(limit=1)[0]
    assert row["status_code"] == 500
    assert row["total_tokens"] == 0
    assert row["token_source"] == "none"


def test_upstream_failure_is_recorded(configured):
    import httpx

    app, token = configured
    client = app.test_client()
    install_stub(app, error=httpx.ConnectError("boom"))

    response = client.post("/v1/chat/completions", json={"model": "m"}, headers=auth_header(token))

    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "upstream_error"
    db = app.extensions["midware_db"]
    row = db.list_requests(limit=1)[0]
    assert row["status_code"] == 502
    assert "ConnectError" in row["error"]


def test_x_api_key_style_auth(configured):
    app, token = configured
    client = app.test_client()
    install_stub(app, StubResponse(content=b'{"usage":{"total_tokens":5}}'))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "m"},
        headers={"x-api-key": token},
    )

    assert response.status_code == 200
    assert app.extensions["midware_db"].count_requests() == 1


def test_get_method_not_allowed(configured_client):
    client, token = configured_client
    response = client.get("/v1/chat/completions", headers=auth_header(token))
    assert response.status_code == 405
