from __future__ import annotations

import json

from midware import adminauth, create_app


def login(client, username: str = "admin", password: str = "s3cret", *, next: str | None = None):
    data = {"username": username, "password": password}
    if next is not None:
        data["next"] = next
    return client.post("/signin", data=data)


def setup_code(app) -> str:
    return app.extensions["midware_db"].get_meta("admin_setup_code")


def seed(app, *, username: str = "admin", password: str = "s3cret"):
    db = app.extensions["midware_db"]
    db.create_route(name="Default", target_host="https://api.example.com", upstream_key="sk")
    db.create_api_key("tester")
    db.mark_configured()
    with app.app_context():
        adminauth.set_credentials(username, password)
    return db


def record_message(db) -> int:
    key = db.list_api_keys()[0]
    return db.record_request(
        api_key_id=key["id"],
        route_id=None,
        method="POST",
        request_path="chat/completions",
        model="m",
        status_code=200,
        latency_ms=1,
        streamed=False,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        request_body=json.dumps({"messages": [{"role": "user", "content": "SECRETPROMPT"}]}),
        response_body=json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "SECRETREPLY"}}]}
        ),
    )


def test_admin_bounces_to_setup_before_a_password_exists(secured_client):
    response = secured_client.get("/admin/keys")
    assert response.status_code == 302
    assert "/admin/setup" in response.headers["Location"]
    assert secured_client.get("/admin/setup").status_code == 200
    assert secured_client.get("/signin").status_code == 302
    assert "/admin/setup" in secured_client.get("/signin").headers["Location"]


def test_proxy_surface_is_never_shown_a_login_challenge(secured_client):
    response = secured_client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers
    assert response.get_json()["error"]["code"] == "invalid_api_key"


def test_setup_requires_the_console_code(secured_client):
    app = secured_client.application
    base = {
        "admin_username": "boss",
        "admin_password": "hunter2",
        "name": "OpenAI",
        "target_host": "https://api.openai.com",
    }

    missing = secured_client.post("/admin/setup", data=base)
    assert missing.status_code == 200
    assert b"Invalid setup code" in missing.data

    wrong = secured_client.post("/admin/setup", data={**base, "setup_code": "0" * 32})
    assert wrong.status_code == 200
    assert b"Invalid setup code" in wrong.data

    good = secured_client.post("/admin/setup", data={**base, "setup_code": setup_code(app)})
    assert good.status_code == 302
    assert setup_code(app) is None


def test_setup_sets_the_login_then_signs_in(secured_client):
    app = secured_client.application
    response = secured_client.post(
        "/admin/setup",
        data={
            "setup_code": setup_code(app),
            "admin_username": "boss",
            "admin_password": "hunter2",
            "name": "OpenAI",
            "target_host": "https://api.openai.com",
            "upstream_key": "sk",
            "key_name": "laptop",
            "request_log_limit": "7",
        },
    )
    assert response.status_code == 302

    # The setup POST itself signs the browser in.
    keys = secured_client.get("/admin/keys")
    assert keys.status_code == 200
    assert "Authentication required" not in keys.get_data(as_text=True)

    settings = secured_client.get("/admin/settings")
    assert settings.status_code == 200
    assert "Control-panel login" in settings.get_data(as_text=True)


def test_migration_asks_only_for_a_login(secured_app):
    db = secured_app.extensions["midware_db"]
    db.create_route(name="Default", target_host="https://api.example.com", upstream_key="sk")
    db.create_api_key("tester")
    db.mark_configured()
    client = secured_app.test_client()

    page = client.get("/admin/setup").get_data(as_text=True)
    assert "Control-panel login" in page
    assert 'name="target_host"' not in page

    response = client.post(
        "/admin/setup",
        data={
            "setup_code": setup_code(secured_app),
            "admin_username": "admin",
            "admin_password": "pw",
        },
    )
    assert response.status_code == 302
    assert len(db.list_routes()) == 1
    assert client.get("/admin/keys").status_code == 200


def test_setup_redirects_once_configured(secured_app):
    seed(secured_app)
    client = secured_app.test_client()
    login(client)
    response = client.get("/admin/setup")
    assert response.status_code == 302
    assert "/admin/routes" in response.headers["Location"]


def test_deleting_routes_cannot_expose_the_account_form(secured_app):
    db = seed(secured_app, username="admin", password="original")
    db.delete_route(db.list_routes()[0]["id"])
    client = secured_app.test_client()

    assert client.get("/admin/setup").status_code == 302
    login(client, password="original")

    html = client.get("/admin/setup").get_data(as_text=True)
    assert "Control-panel login" not in html
    assert 'name="admin_password"' not in html

    client.post(
        "/admin/setup",
        data={
            "admin_username": "attacker",
            "admin_password": "hacked",
            "name": "Default",
            "target_host": "https://api.example.com",
        },
    )
    with secured_app.app_context():
        assert adminauth.verify("admin", "original")
        assert not adminauth.verify("attacker", "hacked")


def test_password_is_stored_hashed(secured_app):
    db = seed(secured_app, password="plaintextpw")
    with secured_app.app_context():
        stored = db.get_meta("admin_password_hash")
    assert "plaintextpw" not in stored
    assert stored.startswith("scrypt:")


def test_signin_page_renders(secured_app):
    seed(secured_app)
    response = secured_app.test_client().get("/signin")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'action="/signin"' in html or "Sign in" in html
    assert 'name="password"' in html


def test_wrong_password_does_not_sign_in(secured_app):
    seed(secured_app)
    client = secured_app.test_client()

    response = login(client, password="wrong")
    assert response.status_code == 401
    assert b"Incorrect username or password" in response.data
    assert client.get("/admin/keys").status_code == 302


def test_login_redirects_to_the_requested_page(secured_app):
    seed(secured_app)
    client = secured_app.test_client()

    response = login(client, next="/admin/routes")
    assert response.status_code == 302
    assert response.headers["Location"] == "/admin/routes"
    assert client.get("/admin/routes").status_code == 200


def test_login_rejects_open_redirects(secured_app):
    seed(secured_app)
    for target in ("https://evil.example", "//evil.example", "/\\evil.example"):
        client = secured_app.test_client()
        response = login(client, next=target)
        assert response.status_code == 302
        assert response.headers["Location"] == "/"


def test_session_cookie_is_persistent_and_hardened(secured_app):
    seed(secured_app)
    response = login(secured_app.test_client())
    cookie = response.headers.get("Set-Cookie", "").lower()

    assert "midware_session=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "max-age=" in cookie or "expires=" in cookie


def test_session_survives_a_new_browser(secured_app):
    seed(secured_app)
    first = secured_app.test_client()
    login(first)
    stored = first.get_cookie("midware_session")
    assert stored is not None

    second = secured_app.test_client()
    second.set_cookie("midware_session", stored.value)
    assert second.get("/admin/keys").status_code == 200


def test_logout_clears_the_session(secured_app):
    seed(secured_app)
    client = secured_app.test_client()
    login(client)
    assert client.get("/admin/keys").status_code == 200

    response = client.post("/signout")
    assert response.status_code == 302
    assert client.get("/admin/keys").status_code == 302
    assert "/signin" in client.get("/admin/keys").headers["Location"]


def test_request_detail_hides_messages_without_login(secured_app):
    db = seed(secured_app)
    request_id = record_message(db)
    html = secured_app.test_client().get(f"/requests/{request_id}").get_data(as_text=True)

    assert "Request #" in html
    assert "SECRETPROMPT" not in html
    assert "SECRETREPLY" not in html
    assert 'id="conversation-data"' not in html
    assert "Sign in to view messages" in html


def test_detail_signin_redirects_then_shows(secured_app):
    db = seed(secured_app)
    request_id = record_message(db)
    client = secured_app.test_client()

    locked = client.get(f"/requests/{request_id}?messages=1")
    assert locked.status_code == 302
    assert "/signin" in locked.headers["Location"]
    assert "WWW-Authenticate" not in locked.headers

    login(client)
    shown = client.get(f"/requests/{request_id}?messages=1")
    assert shown.status_code == 200
    assert "SECRETPROMPT" in shown.get_data(as_text=True)
    assert 'id="conversation-data"' in shown.get_data(as_text=True)


def test_messages_json_requires_login(secured_app):
    db = seed(secured_app)
    request_id = record_message(db)
    client = secured_app.test_client()

    locked = client.get(
        f"/requests/{request_id}/messages.json", headers={"Accept": "application/json"}
    )
    assert locked.status_code == 401
    assert locked.get_json()["error"]["code"] == "authentication_required"

    login(client)
    shown = client.get(f"/requests/{request_id}/messages.json")
    assert shown.status_code == 200
    assert shown.get_json()["messages"]


def test_env_credentials_override_the_database(secured_app):
    db = secured_app.extensions["midware_db"]
    with secured_app.app_context():
        adminauth.set_credentials("dblogin", "dbpass")
    secured_app.config["ADMIN_USERNAME"] = "envlogin"
    secured_app.config["ADMIN_PASSWORD"] = "envpass"
    with secured_app.app_context():
        assert adminauth.credentials_configured()
        assert adminauth.current_username() == "envlogin"
        assert adminauth.verify("envlogin", "envpass")
        assert not adminauth.verify("dblogin", "dbpass")

    client = secured_app.test_client()
    assert login(client, "envlogin", "envpass").status_code == 302
    assert client.get("/admin/keys").status_code == 200


def test_change_credentials_signs_out_other_browsers(secured_app):
    seed(secured_app, username="admin", password="oldpass")
    first = secured_app.test_client()
    second = secured_app.test_client()
    login(first, password="oldpass")
    login(second, password="oldpass")

    payload = {
        "action": "credentials",
        "admin_username": "admin",
        "admin_password": "newpass",
        "admin_password_confirm": "newpass",
    }
    first.post("/admin/settings", data={**payload, "current_password": "wrong"})
    assert first.get("/admin/keys").status_code == 200

    first.post("/admin/settings", data={**payload, "current_password": "oldpass"})
    # The browser that changed the password is re-issued a fresh session.
    assert first.get("/admin/keys").status_code == 200
    # Every other browser's stored fingerprint is now stale.
    assert second.get("/admin/keys").status_code == 302

    fresh = secured_app.test_client()
    assert login(fresh, password="newpass").status_code == 302
    assert login(fresh, password="oldpass").status_code == 401


def test_login_failures_are_throttled(secured_app):
    seed(secured_app)
    secured_app.config["ADMIN_LOGIN_MAX_ATTEMPTS"] = 2
    secured_app.config["ADMIN_LOGIN_WINDOW_SECONDS"] = 300
    client = secured_app.test_client()
    with secured_app.app_context():
        adminauth.reset_login_throttle()

    assert login(client, password="wrong").status_code == 401
    assert login(client, password="wrong").status_code == 401
    blocked = login(client, password="s3cret")
    assert blocked.status_code == 429
    assert b"Too many failed attempts" in blocked.data

    with secured_app.app_context():
        adminauth.reset_login_throttle()
    assert login(client).status_code == 302


def test_secret_key_is_generated_and_persisted(tmp_path):
    path = str(tmp_path / "midware.db")
    first = create_app(
        database_path=path, config_overrides={"TESTING": True, "SECRET_KEY": ""}
    )
    secret = first.config["SECRET_KEY"]
    assert secret
    assert first.extensions["midware_db"].get_meta("admin_session_secret") == secret

    second = create_app(
        database_path=path, config_overrides={"TESTING": True, "SECRET_KEY": ""}
    )
    assert second.config["SECRET_KEY"] == secret
