from __future__ import annotations

import json

from test_proxy import StubResponse, auth_header, install_stub


def body_for(model: str, tokens: int = 7) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-x",
            "model": model,
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": tokens},
        }
    ).encode()


def post(client, token, model, **extra):
    payload = {"model": model, "messages": []}
    payload.update(extra)
    return client.post("/v1/chat/completions", json=payload, headers=auth_header(token))


def test_no_prefix_uses_default_route(multi_host):
    app, token = multi_host
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=body_for("gpt-4o")))

    response = post(client, token, "gpt-4o")

    assert response.status_code == 200
    assert stub.sent[0][0]["url"] == "https://api.example.com/chat/completions"
    row = app.extensions["midware_db"].list_requests(limit=1)[0]
    assert row["host_prefix"] is None
    assert row["model"] == "gpt-4o"


def test_prefix_routes_to_named_host(multi_host):
    app, token = multi_host
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=body_for("xiaomi/mimo")))

    response = post(client, token, "[NanoGPT]xiaomi/mimo")

    assert response.status_code == 200
    request = stub.sent[0][0]
    assert request["url"] == "https://nano-gpt.com/subscription/v1/chat/completions"
    assert json.loads(request["content"])["model"] == "xiaomi/mimo"
    assert request["headers"]["Authorization"] == "Bearer sk-nano"

    row = app.extensions["midware_db"].list_requests(limit=1)[0]
    assert row["host_prefix"] == "NanoGPT"
    assert row["model"] == "xiaomi/mimo"
    assert row["model_raw"] == "[NanoGPT]xiaomi/mimo"


def test_prefix_matching_is_case_and_space_insensitive(multi_host):
    app, token = multi_host
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=body_for("m")))

    response = post(client, token, "[  nanogpt ]xiaomi/mimo")

    assert response.status_code == 200
    assert stub.sent[0][0]["url"] == "https://nano-gpt.com/subscription/v1/chat/completions"


def test_inactive_host_is_addressable_by_prefix(multi_host):
    app, token = multi_host
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=body_for("llama3")))

    response = post(client, token, "[Local]llama3")

    assert response.status_code == 200
    assert stub.sent[0][0]["url"] == "http://127.0.0.1:11434/v1/chat/completions"


def test_route_id_prefix_wins(multi_host):
    app, token = multi_host
    db = app.extensions["midware_db"]
    nano = [r for r in db.list_routes() if r["name"] == "NanoGPT"][0]
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=body_for("m")))

    response = post(client, token, f"[# {nano['id']}]m".replace(" ", ""))

    assert response.status_code == 200
    assert stub.sent[0][0]["url"] == "https://nano-gpt.com/subscription/v1/chat/completions"


def test_unknown_prefix_is_a_404(multi_host):
    app, token = multi_host
    client = app.test_client()
    stub = install_stub(app, StubResponse(content=body_for("m")))

    response = post(client, token, "[Nope]gpt-4o")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["error"]["code"] == "unknown_host"
    assert stub.sent == []
    assert app.extensions["midware_db"].count_requests() == 0


def test_prefix_does_not_leak_into_streamed_bookkeeping(multi_host):
    app, token = multi_host
    client = app.test_client()
    chunks = [
        b'data: {"id":"1","model":"xiaomi/mimo","choices":[{"delta":{"content":"hi"}}]}\n\n',
        b'data: {"id":"1","model":"xiaomi/mimo","usage":{"prompt_tokens":2,"completion_tokens":5,"total_tokens":7}}\n\n',
        b"data: [DONE]\n\n",
    ]
    install_stub(
        app,
        StubResponse(status_code=200, headers={"content-type": "text/event-stream"}, stream_chunks=chunks),
    )

    response = post(client, token, "[NanoGPT]xiaomi/mimo", stream=True)

    assert response.status_code == 200
    b"".join(response.response)

    row = app.extensions["midware_db"].list_requests(limit=1)[0]
    assert row["host_prefix"] == "NanoGPT"
    assert row["model"] == "xiaomi/mimo"
    assert row["total_tokens"] == 7


def test_host_usage_aggregates(multi_host):
    app, token = multi_host
    client = app.test_client()
    install_stub(app, StubResponse(content=body_for("m")))

    post(client, token, "gpt-4o")
    post(client, token, "[NanoGPT]xiaomi/mimo")
    post(client, token, "[NanoGPT]xiaomi/mimo")

    hosts = {entry["host"]: entry for entry in app.extensions["midware_db"].host_usage()}
    assert hosts["default"]["requests"] == 1
    assert hosts["NanoGPT"]["requests"] == 2


def test_migration_adds_columns_to_legacy_db(tmp_path):
    import sqlite3

    from midware.db import Database

    legacy = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy)
    connection.executescript(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            request_path TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL DEFAULT 'POST',
            model TEXT,
            status_code INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.execute(
        "INSERT INTO requests (created_at, request_path, method, model, status_code) VALUES (?,?,?,?,?)",
        ("2026-01-01T00:00:00+00:00", "p", "POST", "old-model", 200),
    )
    connection.commit()
    connection.close()

    db = Database(str(legacy))
    db.init_schema()
    columns = {row["name"] for row in db._rows("PRAGMA table_info(requests)")}
    assert {"model_raw", "host_prefix"} <= columns
    assert db.list_requests(limit=1)[0]["model"] == "old-model"
