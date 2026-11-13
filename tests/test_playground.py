"""The Playground page and the temporary API key it runs on.

Two things are load-bearing here and both are tested: the key must be reissued
by name (never accumulate rows), and an expired key must stop working at the
proxy surface itself, not just in the UI.

Written by an AI coding agent (Torch, for RandombXYZ).
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from midware.blueprints.playground import (
    KEY_MAX_REQUESTS_PER_HOUR,
    KEY_NAME,
    KEY_TTL_SECONDS,
)
from midware.db import parse_iso, utcnow

from test_proxy import StubResponse, install_stub


def chat_body(text: str = "hello") -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-playground",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-4o-mini",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    ).encode()


@pytest.fixture()
def ready(configured):
    app, _ = configured
    return app, app.test_client()


def mint(client):
    response = client.post(
        "/admin/playground/key", headers={"Accept": "application/json"}
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


def chat(client, token, model: str = "gpt-4o-mini"):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}]}),
    )


# -- the page ----------------------------------------------------------------


def test_page_renders_with_the_base_url_of_this_request(ready):
    app, client = ready
    html = client.get("/admin/playground").get_data(as_text=True)
    assert 'data-api-base="http://localhost/v1"' in html
    assert 'id="pg-code"' in html
    assert "Manage keys" in html


def test_page_never_shows_the_key(ready):
    """The temporary key lives in the page's own JS call, not in front of the user."""
    app, client = ready
    html = client.get("/admin/playground").get_data(as_text=True)

    assert "Temporary API key" not in html
    assert "temporary key" not in html
    assert "Temporary:" not in html
    assert "req/hour" not in html


def test_page_starts_with_the_playground_prompt(ready):
    app, client = ready
    html = client.get("/admin/playground").get_data(as_text=True)
    assert "eldritch" not in html  # seeded by JS, not baked into the template

    js = client.get("/static/playground.js").get_data(as_text=True)
    assert "eldritch god" in js
    assert "i like turtles" in js


def test_page_is_linked_from_the_nav(ready):
    app, client = ready
    html = client.get("/requests").get_data(as_text=True)
    assert 'href="/admin/playground"' in html


def test_keys_page_explains_the_base_url_and_links_the_playground(ready):
    app, client = ready
    html = client.get("/admin/keys").get_data(as_text=True)
    assert "MidWare API key usage" in html
    assert "http://localhost/v1" in html
    assert 'href="/admin/playground"' in html


# -- minting -----------------------------------------------------------------


def test_mints_a_temporary_key_with_the_agreed_shape(ready):
    app, client = ready
    body = mint(client)
    db = app.extensions["midware_db"]

    assert body["token"].startswith("mw-")
    assert body["hour_limit"] == KEY_MAX_REQUESTS_PER_HOUR
    assert body["requests_url"].endswith(f"key={body['key_id']}")

    row = db.get_api_key(body["key_id"])
    assert row["name"] == KEY_NAME
    assert row["enabled"] == 1
    assert row["is_system"] == 0
    assert row["description"]
    assert row["limit_requests_enabled"] == 1
    assert row["limit_requests_hour"] == 1
    assert row["max_requests_hour"] == KEY_MAX_REQUESTS_PER_HOUR
    # Logging stays on: a playground request you cannot inspect afterwards is
    # half a playground.
    assert row["log_requests"] == 1

    expires_at = parse_iso(row["expires_at"])
    assert expires_at is not None
    remaining = (expires_at - utcnow()).total_seconds()
    assert abs(remaining - KEY_TTL_SECONDS) < 120


def test_a_second_visit_reuses_the_same_row(ready):
    app, client = ready
    first = mint(client)
    second = mint(client)
    db = app.extensions["midware_db"]

    assert first["key_id"] == second["key_id"]
    assert first["token"] == second["token"]
    named = [key for key in db.list_api_keys() if key["name"] == KEY_NAME]
    assert len(named) == 1


def test_reopening_extends_an_expired_playground_key(ready):
    app, client = ready
    body = mint(client)
    db = app.extensions["midware_db"]

    db.set_api_key_expiry(body["key_id"], utcnow() - timedelta(minutes=5))
    assert chat(client, body["token"]).status_code == 403

    again = mint(client)
    assert again["key_id"] == body["key_id"]
    fresh_expiry = parse_iso(db.get_api_key(body["key_id"])["expires_at"])
    assert fresh_expiry is not None
    assert fresh_expiry > utcnow()


def test_reopening_restores_the_agreed_request_cap(ready):
    app, client = ready
    body = mint(client)
    db = app.extensions["midware_db"]
    db.update_api_key(body["key_id"], max_requests_hour=0, limit_requests_enabled=False)

    mint(client)

    row = db.get_api_key(body["key_id"])
    assert row["limit_requests_enabled"] == 1
    assert row["max_requests_hour"] == KEY_MAX_REQUESTS_PER_HOUR


def test_a_re_enabled_key_comes_back_on(ready):
    app, client = ready
    body = mint(client)
    db = app.extensions["midware_db"]
    db.set_api_key_enabled(body["key_id"], False)

    mint(client)

    assert db.get_api_key(body["key_id"])["enabled"] == 1


def test_deleting_the_key_lets_the_next_visit_mint_a_new_one(ready):
    app, client = ready
    body = mint(client)
    db = app.extensions["midware_db"]
    db.delete_api_key(body["key_id"])

    fresh = mint(client)

    assert fresh["key_id"] != body["key_id"]
    assert fresh["token"] != body["token"]


# -- the key is a real key ---------------------------------------------------


def test_the_minted_key_proxies_a_real_request(ready):
    app, client = ready
    db = app.extensions["midware_db"]
    stub = install_stub(app, StubResponse(content=chat_body("streamed hi")))
    body = mint(client)

    response = chat(client, body["token"])

    assert response.status_code == 200
    assert len(stub.sent) == 1
    rows = db.list_requests(limit=5)
    assert rows[0]["api_key_id"] == body["key_id"]


# -- expiry is enforced at the proxy surface ---------------------------------


def test_expired_key_is_refused_before_any_upstream_call(ready):
    app, client = ready
    db = app.extensions["midware_db"]
    stub = install_stub(app, StubResponse(content=chat_body()))
    token = db.create_api_key("stale", expires_at=utcnow() - timedelta(minutes=1))

    response = chat(client, token)

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "api_key_expired"
    assert stub.sent == []


def test_expired_key_is_refused_on_the_models_catalogue(ready):
    app, client = ready
    db = app.extensions["midware_db"]
    token = db.create_api_key("stale", expires_at=utcnow() - timedelta(seconds=1))

    response = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "api_key_expired"


def test_a_future_expiry_does_not_block_the_key(ready):
    app, client = ready
    db = app.extensions["midware_db"]
    stub = install_stub(app, StubResponse(content=chat_body()))
    token = db.create_api_key("valid", expires_at=utcnow() + timedelta(hours=1))

    response = chat(client, token)

    assert response.status_code == 200
    assert len(stub.sent) == 1


def test_disabled_still_wins_over_expired(ready):
    app, client = ready
    db = app.extensions["midware_db"]
    token = db.create_api_key("both", expires_at=utcnow() - timedelta(hours=1))
    db.set_api_key_enabled(
        [key["id"] for key in db.list_api_keys() if key["name"] == "both"][0], False
    )

    response = chat(client, token)

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "api_key_disabled"


def test_an_expiring_key_is_marked_on_the_keys_page(ready):
    app, client = ready
    mint(client)

    html = client.get("/admin/keys").get_data(as_text=True)

    assert 'class="pill warn" title="Expires' in html
    assert KEY_NAME in html


def test_a_plain_key_shows_no_temporary_badge(ready):
    app, client = ready
    db = app.extensions["midware_db"]
    for key in db.list_api_keys():
        db.delete_api_key(key["id"])
    db.create_api_key("permanent")

    html = client.get("/admin/keys").get_data(as_text=True)

    assert 'class="pill warn" title="Expires' not in html


# -- the page is admin-only --------------------------------------------------


def test_page_and_key_endpoint_need_a_session(secured_app):
    from test_admin_auth import seed

    seed(secured_app)
    client = secured_app.test_client()
    db = secured_app.extensions["midware_db"]
    before = db.count_api_keys()

    page = client.get("/admin/playground")
    assert page.status_code == 302
    assert "/signin" in page.headers["Location"]

    key = client.post("/admin/playground/key", headers={"Accept": "application/json"})
    assert key.status_code == 401
    assert key.get_json()["error"]["code"] == "authentication_required"
    assert db.count_api_keys() == before
