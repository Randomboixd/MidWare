from __future__ import annotations


def test_cors_defaults_to_wildcard(app):
    response = app.test_client().get("/")
    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_preflight_allows_proxy_methods_and_headers(configured_client):
    client, _ = configured_client
    response = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "https://app.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in response.headers["Access-Control-Allow-Methods"]
    assert "authorization" in response.headers["Access-Control-Allow-Headers"].lower()


def test_allowlisted_origin_is_echoed(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    db.set_setting("cors_allow_origin", "https://app.example.com")

    response = client.get("/", headers={"Origin": "https://app.example.com"})
    assert response.headers["Access-Control-Allow-Origin"] == "https://app.example.com"
    assert "Origin" in response.headers["Vary"]


def test_unknown_origin_is_not_allowed(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    db.set_setting("cors_allow_origin", "https://app.example.com")

    response = client.get("/", headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in response.headers


def test_empty_setting_disables_cors(configured_client):
    client, _ = configured_client
    client.application.extensions["midware_db"].set_setting("cors_allow_origin", "")

    response = client.get("/", headers={"Origin": "https://app.example.com"})
    assert "Access-Control-Allow-Origin" not in response.headers


def test_settings_page_saves_cors_origin(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]

    client.post(
        "/admin/settings",
        data={"request_log_limit": "10", "cors_allow_origin": "https://ui.example.com"},
    )

    assert db.cors_allow_origin() == "https://ui.example.com"
