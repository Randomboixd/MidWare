from __future__ import annotations

from test_proxy import StubResponse, auth_header, install_stub


def _key_id(app):
    return app.extensions["midware_db"].list_api_keys()[0]["id"]


def _make_premodel(db, **overrides):
    route = db.active_route()
    fields = {
        "name": "Story",
        "slug": "story",
        "route_id": route["id"],
        "model": "story-model",
    }
    fields.update(overrides)
    return db.create_premodel(**fields)


def _call(client, token, model="<p>-story"):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": []},
        headers=auth_header(token),
    )


# -- admin form --------------------------------------------------------------


def test_premodel_form_renders_limit_controls(configured_client):
    client, _ = configured_client
    page = client.get("/admin/premodels/new")

    assert page.status_code == 200
    for marker in (
        b'id="limits-toggle"',
        b'id="limits-panel"',
        b'id="limit-tokens"',
        b'id="limit-requests"',
        b'name="max_tokens_day"',
        b'name="max_tokens_week"',
        b'name="max_requests_minute"',
        b'name="max_requests_week"',
    ):
        assert marker in page.data


def test_premodel_create_stores_limits(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]

    client.post(
        "/admin/premodels/new",
        data={
            "name": "Capped",
            "slug": "capped",
            "route_id": db.active_route()["id"],
            "model": "m",
            "prompt_mode": "simple",
            "system_prompt": "hi",
            "limit_tokens_enabled": "1",
            "limit_tokens_day": "1",
            "max_tokens_day": "50000",
            "limit_requests_enabled": "1",
            "limit_requests_minute": "1",
            "max_requests_minute": "10",
        },
    )

    premodel = db.get_premodel_by_slug("capped")
    assert premodel is not None
    assert premodel["limit_tokens_enabled"] == 1
    assert premodel["limit_tokens_day"] == 1
    assert premodel["max_tokens_day"] == 50000
    assert premodel["limit_tokens_hour"] == 0
    assert premodel["max_tokens_hour"] == 0
    assert premodel["limit_requests_enabled"] == 1
    assert premodel["limit_requests_minute"] == 1
    assert premodel["max_requests_minute"] == 10
    assert premodel["max_requests_week"] == 0


def test_premodel_edit_round_trips_limits(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route = db.active_route()
    premodel_id = _make_premodel(
        db,
        limit_tokens_enabled=True,
        limit_tokens_hour=True,
        max_tokens_hour=1234,
        limit_requests_enabled=True,
        limit_requests_day=True,
        max_requests_day=99,
    )

    page = client.get(f"/admin/premodels/{premodel_id}/edit")
    assert page.status_code == 200
    assert b'value="1234"' in page.data
    assert b'value="99"' in page.data

    client.post(
        f"/admin/premodels/{premodel_id}/edit",
        data={
            "name": "Story",
            "slug": "story",
            "route_id": route["id"],
            "model": "story-model",
            "prompt_mode": "simple",
            "system_prompt": "",
            "limit_tokens_hour": "1",
            "max_tokens_hour": "42",
        },
    )

    row = db.get_premodel(premodel_id)
    assert row["limit_tokens_enabled"] == 0
    assert row["limit_tokens_hour"] == 1
    assert row["max_tokens_hour"] == 42
    assert row["limit_requests_day"] == 0
    assert row["max_requests_day"] == 0


# -- enforcement ------------------------------------------------------------


def test_premodel_request_limit_rejects_second_call(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    premodel_id = _make_premodel(
        db,
        limit_requests_enabled=True,
        limit_requests_minute=True,
        max_requests_minute=1,
    )
    stub = install_stub(app, StubResponse(content=b"{}"))

    first = _call(client, token)
    second = _call(client, token)

    assert first.status_code == 200
    assert second.status_code == 429
    error = second.get_json()["error"]
    assert error["code"] == "request_limit_exceeded"
    assert error["type"] == "midware_rate_limit_error"
    assert "premodel" in error["message"]
    assert int(second.headers["Retry-After"]) >= 1
    assert len(stub.sent) == 1

    counts = db.premodel_rate_usage_amounts(premodel_id, "requests", ("minute",))
    assert counts["minute"] == 1

    rows = db.list_requests(api_key_id=_key_id(app))
    assert rows[0]["status_code"] == 429
    assert "request_limit_exceeded" in rows[0]["error"]


def test_disabled_master_ignores_child_limits(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    _make_premodel(
        db,
        limit_requests_enabled=False,
        limit_requests_minute=True,
        max_requests_minute=1,
    )
    install_stub(app, StubResponse(content=b"{}"))

    assert _call(client, token).status_code == 200
    assert _call(client, token).status_code == 200


def test_zero_limit_means_unlimited(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    _make_premodel(db, limit_requests_enabled=True, limit_requests_day=True, max_requests_day=0)
    install_stub(app, StubResponse(content=b"{}"))

    for _ in range(3):
        assert _call(client, token).status_code == 200


def test_premodel_token_limit_blocks_before_upstream(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    premodel_id = _make_premodel(
        db, limit_tokens_enabled=True, limit_tokens_day=True, max_tokens_day=10
    )
    db.premodel_rate_usage_add(premodel_id, "tokens", {"day": 10})
    stub = install_stub(app, StubResponse(content=b"{}"))

    response = _call(client, token)

    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "token_limit_exceeded"
    assert int(response.headers["Retry-After"]) >= 1
    assert stub.sent == []


def test_successful_call_counts_tokens_on_premodel(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    premodel_id = _make_premodel(
        db, limit_tokens_enabled=True, limit_tokens_day=True, max_tokens_day=1000
    )
    key_id = _key_id(app)
    install_stub(
        app,
        StubResponse(
            content=b'{"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}'
        ),
    )

    response = _call(client, token)

    assert response.status_code == 200
    assert db.premodel_rate_usage_amounts(premodel_id, "tokens", ("day",))["day"] == 7
    # The key has no token limits, so its token counter stays untouched.
    assert db.rate_usage_amounts(key_id, "tokens", ("day",))["day"] == 0


def test_premodel_rejection_does_not_consume_key_quota(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    key_id = _key_id(app)
    premodel_id = _make_premodel(
        db, limit_requests_enabled=True, limit_requests_minute=True, max_requests_minute=1
    )
    install_stub(app, StubResponse(content=b"{}"))

    assert _call(client, token).status_code == 200
    assert _call(client, token).status_code == 429

    assert db.premodel_rate_usage_amounts(premodel_id, "requests", ("minute",))["minute"] == 1
    # An unlimited key never writes counters, so the rejection cannot eat its quota.
    assert db.rate_usage_amounts(key_id, "requests", ("minute",))["minute"] == 0


def test_key_rejection_does_not_consume_premodel_quota(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    key_id = _key_id(app)
    db.update_api_key(
        key_id, limit_requests_enabled=True, limit_requests_minute=True, max_requests_minute=1
    )
    premodel_id = _make_premodel(
        db, limit_requests_enabled=True, limit_requests_minute=True, max_requests_minute=50
    )
    install_stub(app, StubResponse(content=b"{}"))

    assert _call(client, token).status_code == 200
    assert _call(client, token).status_code == 429

    # The key check runs first; its rejection rolls back before the premodel claim.
    assert db.rate_usage_amounts(key_id, "requests", ("minute",))["minute"] == 1
    assert db.premodel_rate_usage_amounts(premodel_id, "requests", ("minute",))["minute"] == 1


def test_plain_model_calls_do_not_touch_premodel_counters(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    premodel_id = _make_premodel(
        db, limit_requests_enabled=True, limit_requests_minute=True, max_requests_minute=5
    )
    install_stub(app, StubResponse(content=b"{}"))

    assert _call(client, token, model="plain-model").status_code == 200
    assert db.premodel_rate_usage_amounts(premodel_id, "requests", ("minute",))["minute"] == 0


def test_premodel_claim_rolls_back_over_limit(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    premodel_id = _make_premodel(db)
    limits = {"minute": 2}

    assert db.premodel_rate_usage_claim(premodel_id, limits) == ({"minute": 1}, True)
    assert db.premodel_rate_usage_claim(premodel_id, limits) == ({"minute": 2}, True)
    assert db.premodel_rate_usage_claim(premodel_id, limits) == ({"minute": 2}, False)
    assert db.premodel_rate_usage_claim(premodel_id, limits) == ({"minute": 2}, False)
    assert db.premodel_rate_usage_amounts(premodel_id, "requests", ("minute",))["minute"] == 2


def test_deleting_premodel_removes_counters(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    premodel_id = _make_premodel(db)
    db.premodel_rate_usage_add(premodel_id, "tokens", {"day": 5})

    db.delete_premodel(premodel_id)

    assert db.premodel_rate_usage_amounts(premodel_id, "tokens", ("day",))["day"] == 0
