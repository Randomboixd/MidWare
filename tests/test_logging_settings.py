"""Per-key request/message logging switches.

A key carries two independent but nested switches: a master "log requests" and
a child "log message contents". Turning contents off keeps metadata-only rows;
turning requests off keeps accounting (usage ledger + rate limits) but writes no
rows at all.
"""

from __future__ import annotations

import json

from test_proxy import StubResponse, auth_header, install_stub


def _key(app):
    return app.extensions["midware_db"].list_api_keys()[0]


def _chat_response(total=3):
    return json.dumps(
        {
            "id": "chatcmpl-log",
            "model": "m",
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": total},
        }
    ).encode()


def _post(client, token, content="secret"):
    return client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": content}]},
        headers=auth_header(token),
    )


def test_key_logs_contents_by_default(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    client = app.test_client()
    install_stub(app, StubResponse(status_code=200, content=_chat_response()))

    assert _post(client, token).status_code == 200

    row = db.list_requests(limit=1)[0]
    assert "secret" in (row["request_body"] or "")
    assert "chatcmpl-log" in (row["response_body"] or "")


def test_key_without_message_logging_keeps_metadata_only(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    key = _key(app)
    db.update_api_key(key["id"], log_message_contents=False)
    client = app.test_client()
    install_stub(app, StubResponse(status_code=200, content=_chat_response()))

    assert _post(client, token).status_code == 200

    rows = db.list_requests(limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["request_body"] is None
    assert row["response_body"] is None
    assert row["model"] == "m"
    assert row["status_code"] == 200
    assert row["total_tokens"] == 3


def test_key_without_request_logging_still_counts_usage(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    key = _key(app)
    db.update_api_key(key["id"], log_requests=False, log_message_contents=False)
    client = app.test_client()
    install_stub(app, StubResponse(status_code=200, content=_chat_response(total=9)))

    assert _post(client, token).status_code == 200

    assert db.count_requests() == 0
    totals = db.usage_totals("day")
    assert totals["requests"] == 1
    assert totals["total_tokens"] == 9

    listed = [row for row in db.list_api_keys() if row["id"] == key["id"]][0]
    assert listed["request_count"] == 1
    assert listed["token_count"] == 9


def test_request_logging_off_still_settles_token_rate_limits(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    key = _key(app)
    db.update_api_key(
        key["id"],
        log_requests=False,
        log_message_contents=False,
        limit_tokens_enabled=True,
        limit_tokens_day=True,
        max_tokens_day=100,
    )
    client = app.test_client()
    install_stub(app, StubResponse(status_code=200, content=_chat_response(total=9)))

    assert _post(client, token).status_code == 200

    assert db.rate_usage_amounts(key["id"], "tokens", ("day",))["day"] == 9


def test_admin_key_form_saves_logging_settings(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    client = app.test_client()

    client.post("/admin/keys/new", data={"name": "no-contents", "log_requests": "1"})
    row = db._row("SELECT * FROM api_keys WHERE name = 'no-contents'")
    assert row["log_requests"] == 1
    assert row["log_message_contents"] == 0

    client.post(
        "/admin/keys/new",
        data={"name": "silent", "log_requests": "0", "log_message_contents": "1"},
    )
    row = db._row("SELECT * FROM api_keys WHERE name = 'silent'")
    # The child cannot survive the master: contents are forced off too.
    assert row["log_requests"] == 0
    assert row["log_message_contents"] == 0

    client.post(
        "/admin/keys/new",
        data={"name": "full", "log_requests": "1", "log_message_contents": "1"},
    )
    row = db._row("SELECT * FROM api_keys WHERE name = 'full'")
    assert row["log_requests"] == 1
    assert row["log_message_contents"] == 1

    # Editing round-trips the values.
    key_id = row["id"]
    client.post(
        f"/admin/keys/{key_id}/edit",
        data={"name": "full", "log_requests": "1"},
    )
    row = db.get_api_key(key_id)
    assert row["log_requests"] == 1
    assert row["log_message_contents"] == 0


def test_key_new_form_defaults_logging_on(configured_client):
    import re

    client, _ = configured_client
    page = client.get("/admin/keys/new").get_data(as_text=True)
    assert "Logging settings" in page
    # Both boxes start checked; the contents box nests under its master.
    assert re.search(r'id="log-requests"[^>]*checked', page)
    assert re.search(r'id="log-message-contents"[^>]*checked', page)
    assert 'id="log-contents-panel"' in page
