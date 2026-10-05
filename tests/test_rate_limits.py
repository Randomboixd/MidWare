from __future__ import annotations

from datetime import datetime, timezone

from midware.db import rate_window_end, rate_window_start
from test_proxy import StubResponse, auth_header, install_stub


def _key_id(app):
    return app.extensions["midware_db"].list_api_keys()[0]["id"]


def test_rate_window_boundaries():
    now = datetime(2026, 10, 7, 13, 45, 30, tzinfo=timezone.utc)
    utc = timezone.utc

    assert rate_window_start("minute", now) == datetime(2026, 10, 7, 13, 45, tzinfo=utc)
    assert rate_window_start("hour", now) == datetime(2026, 10, 7, 13, 0, tzinfo=utc)
    assert rate_window_start("day", now) == datetime(2026, 10, 7, tzinfo=utc)
    assert rate_window_start("week", now) == datetime(2026, 10, 5, tzinfo=utc)
    assert rate_window_end("minute", now) == datetime(2026, 10, 7, 13, 46, tzinfo=utc)
    assert rate_window_end("week", now) == datetime(2026, 10, 12, tzinfo=utc)


def test_key_form_renders_limit_controls(configured_client):
    client, _ = configured_client
    page = client.get("/admin/keys/new")

    assert page.status_code == 200
    for marker in (
        b'id="limit-tokens"',
        b'id="limit-requests"',
        b'name="max_tokens_day"',
        b'name="max_tokens_week"',
        b'name="max_requests_minute"',
        b'name="max_requests_week"',
    ):
        assert marker in page.data


def test_key_create_stores_limits(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]

    client.post(
        "/admin/keys/new",
        data={
            "name": "capped",
            "limit_tokens_enabled": "1",
            "limit_tokens_day": "1",
            "max_tokens_day": "50000",
            "limit_requests_enabled": "1",
            "limit_requests_minute": "1",
            "max_requests_minute": "10",
        },
    )

    key = [row for row in db.list_api_keys() if row["name"] == "capped"][0]
    assert key["limit_tokens_enabled"] == 1
    assert key["limit_tokens_day"] == 1
    assert key["max_tokens_day"] == 50000
    assert key["limit_tokens_hour"] == 0
    assert key["max_tokens_hour"] == 0
    assert key["limit_requests_enabled"] == 1
    assert key["limit_requests_minute"] == 1
    assert key["max_requests_minute"] == 10
    assert key["max_requests_week"] == 0


def test_key_edit_round_trips_limits(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    key_id = _key_id(client.application)
    db.update_api_key(
        key_id,
        limit_tokens_enabled=True,
        limit_tokens_hour=True,
        max_tokens_hour=1234,
        limit_requests_enabled=True,
        limit_requests_day=True,
        max_requests_day=99,
    )

    page = client.get(f"/admin/keys/{key_id}/edit")
    assert page.status_code == 200
    assert b'value="1234"' in page.data
    assert b'value="99"' in page.data

    client.post(
        f"/admin/keys/{key_id}/edit",
        data={"name": "capped", "limit_tokens_hour": "1", "max_tokens_hour": "42"},
    )

    row = db.get_api_key(key_id)
    assert row["limit_tokens_enabled"] == 0
    assert row["limit_tokens_hour"] == 1
    assert row["max_tokens_hour"] == 42
    assert row["limit_requests_day"] == 0
    assert row["max_requests_day"] == 0


def test_request_limit_rejects_second_call(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    key_id = _key_id(app)
    db.update_api_key(
        key_id, limit_requests_enabled=True, limit_requests_minute=True, max_requests_minute=1
    )
    stub = install_stub(app, StubResponse(content=b"{}"))

    first = client.post(
        "/v1/chat/completions", json={"model": "m"}, headers=auth_header(token)
    )
    second = client.post(
        "/v1/chat/completions", json={"model": "m"}, headers=auth_header(token)
    )

    assert first.status_code == 200
    assert second.status_code == 429
    error = second.get_json()["error"]
    assert error["code"] == "request_limit_exceeded"
    assert error["type"] == "midware_rate_limit_error"
    assert int(second.headers["Retry-After"]) >= 1
    assert len(stub.sent) == 1

    counts = db.rate_usage_amounts(key_id, "requests", ("minute",))
    assert counts["minute"] == 1

    rows = db.list_requests(api_key_id=key_id)
    assert rows[0]["status_code"] == 429
    assert "request_limit_exceeded" in rows[0]["error"]


def test_disabled_master_ignores_child_limits(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    db.update_api_key(
        _key_id(app),
        limit_requests_enabled=False,
        limit_requests_minute=True,
        max_requests_minute=1,
    )
    install_stub(app, StubResponse(content=b"{}"))

    first = client.post(
        "/v1/chat/completions", json={"model": "m"}, headers=auth_header(token)
    )
    second = client.post(
        "/v1/chat/completions", json={"model": "m"}, headers=auth_header(token)
    )

    assert first.status_code == 200
    assert second.status_code == 200


def test_zero_limit_means_unlimited(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    db.update_api_key(
        _key_id(app), limit_requests_enabled=True, limit_requests_day=True, max_requests_day=0
    )
    install_stub(app, StubResponse(content=b"{}"))

    for _ in range(3):
        response = client.post(
            "/v1/chat/completions", json={"model": "m"}, headers=auth_header(token)
        )
        assert response.status_code == 200


def test_token_limit_blocks_before_upstream(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    key_id = _key_id(app)
    db.update_api_key(
        key_id, limit_tokens_enabled=True, limit_tokens_day=True, max_tokens_day=10
    )
    db.rate_usage_add(key_id, "tokens", {"day": 10})
    stub = install_stub(app, StubResponse(content=b"{}"))

    response = client.post(
        "/v1/chat/completions", json={"model": "m"}, headers=auth_header(token)
    )

    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "token_limit_exceeded"
    assert int(response.headers["Retry-After"]) >= 1
    assert stub.sent == []


def test_successful_call_counts_tokens(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    key_id = _key_id(app)
    db.update_api_key(
        key_id, limit_tokens_enabled=True, limit_tokens_day=True, max_tokens_day=1000
    )
    install_stub(
        app,
        StubResponse(
            content=b'{"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}'
        ),
    )

    response = client.post(
        "/v1/chat/completions", json={"model": "m"}, headers=auth_header(token)
    )

    assert response.status_code == 200
    counts = db.rate_usage_amounts(key_id, "tokens", ("day",))
    assert counts["day"] == 7


def test_models_catalogue_is_not_rate_limited(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    key_id = _key_id(app)
    db.update_api_key(
        key_id, limit_requests_enabled=True, limit_requests_minute=True, max_requests_minute=1
    )

    first = client.get("/v1/models", headers=auth_header(token))
    second = client.get("/v1/models", headers=auth_header(token))

    assert first.status_code == 200
    assert second.status_code == 200
    assert db.rate_usage_amounts(key_id, "requests", ("minute",))["minute"] == 0


def test_claim_rolls_back_over_limit(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    key_id = _key_id(client.application)
    limits = {"minute": 2}

    assert db.rate_usage_claim(key_id, limits) == ({"minute": 1}, True)
    assert db.rate_usage_claim(key_id, limits) == ({"minute": 2}, True)
    assert db.rate_usage_claim(key_id, limits) == ({"minute": 2}, False)
    assert db.rate_usage_claim(key_id, limits) == ({"minute": 2}, False)
    assert db.rate_usage_amounts(key_id, "requests", ("minute",))["minute"] == 2
