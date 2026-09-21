from __future__ import annotations

from test_proxy import StubResponse, auth_header, install_stub


def _key_id(app):
    return app.extensions["midware_db"].list_api_keys()[0]["id"]


def test_proxy_response_defaults_to_wildcard(configured_client):
    client, token = configured_client
    install_stub(client.application, StubResponse(content=b"{}"))

    response = client.post("/v1/chat/completions", json={"model": "m"}, headers=auth_header(token))

    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_proxy_echoes_allowlisted_origin(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    db.update_api_key(_key_id(app), cors_allow_origin="https://app.example.com")
    install_stub(app, StubResponse(content=b"{}"))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "m"},
        headers={**auth_header(token), "Origin": "https://app.example.com"},
    )

    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example.com"
    assert "Origin" in response.headers["Vary"]


def test_disallowed_origin_is_rejected_before_forwarding(configured_client):
    client, token = configured_client
    app = client.application
    app.extensions["midware_db"].update_api_key(
        _key_id(app), cors_allow_origin="https://app.example.com"
    )
    stub = install_stub(app, StubResponse(content=b"{}"))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "m"},
        headers={**auth_header(token), "Origin": "https://evil.example.com"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "origin_not_allowed"
    assert stub.sent == []


def test_allowlist_without_origin_is_allowed(configured_client):
    client, token = configured_client
    app = client.application
    app.extensions["midware_db"].update_api_key(
        _key_id(app), cors_allow_origin="https://app.example.com"
    )
    install_stub(app, StubResponse(content=b"{}"))

    response = client.post("/v1/chat/completions", json={"model": "m"}, headers=auth_header(token))

    assert response.status_code == 200


def test_empty_cors_blocks_browser_origins(configured_client):
    client, token = configured_client
    app = client.application
    app.extensions["midware_db"].update_api_key(_key_id(app), cors_allow_origin="")
    install_stub(app, StubResponse(content=b"{}"))

    blocked = client.post(
        "/v1/chat/completions",
        json={"model": "m"},
        headers={**auth_header(token), "Origin": "https://app.example.com"},
    )
    assert blocked.status_code == 403

    plain = client.post("/v1/chat/completions", json={"model": "m"}, headers=auth_header(token))
    assert plain.status_code == 200


def test_preflight_is_permissive_and_echoes_origin(configured_client):
    client, _ = configured_client
    response = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "https://anywhere.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "https://anywhere.example.com"
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    assert "authorization" in response.headers["Access-Control-Allow-Headers"].lower()


def test_admin_pages_send_no_cors_headers(configured_client):
    client, _ = configured_client
    response = client.get("/", headers={"Origin": "https://app.example.com"})
    assert "Access-Control-Allow-Origin" not in response.headers
