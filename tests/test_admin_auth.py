from __future__ import annotations

import base64
import json

from midware import adminauth


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


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


def test_proxy_surface_is_never_shown_a_basic_challenge(secured_client):
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


def test_setup_sets_the_login_then_locks_the_admin(secured_client):
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

    locked = secured_client.get("/admin/keys")
    assert locked.status_code == 401
    assert locked.headers["WWW-Authenticate"].startswith("Basic")
    assert "Authentication required" in locked.get_data(as_text=True)

    assert secured_client.get("/admin/keys", headers=basic("boss", "wrong")).status_code == 401

    settings = secured_client.get("/admin/settings", headers=basic("boss", "hunter2"))
    assert settings.status_code == 200
    assert "Control-panel login" in settings.get_data(as_text=True)
    assert secured_client.get("/admin/keys", headers=basic("boss", "hunter2")).status_code == 200


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
    assert client.get("/admin/keys", headers=basic("admin", "pw")).status_code == 200


def test_setup_redirects_once_configured(secured_app):
    seed(secured_app)
    response = secured_app.test_client().get("/admin/setup", headers=basic("admin", "s3cret"))
    assert response.status_code == 302
    assert "/admin/routes" in response.headers["Location"]


def test_deleting_routes_cannot_expose_the_account_form(secured_app):
    db = seed(secured_app, username="admin", password="original")
    db.delete_route(db.list_routes()[0]["id"])
    client = secured_app.test_client()

    assert client.get("/admin/setup").status_code == 401

    html = client.get("/admin/setup", headers=basic("admin", "original")).get_data(as_text=True)
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
        headers=basic("admin", "original"),
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


def test_request_detail_hides_messages_without_login(secured_app):
    db = seed(secured_app)
    request_id = record_message(db)
    html = secured_app.test_client().get(f"/requests/{request_id}").get_data(as_text=True)

    assert "Request #" in html
    assert "SECRETPROMPT" not in html
    assert "SECRETREPLY" not in html
    assert 'id="conversation-data"' not in html
    assert "Sign in to view messages" in html


def test_detail_signin_flag_challenges_then_shows(secured_app):
    db = seed(secured_app)
    request_id = record_message(db)
    client = secured_app.test_client()

    locked = client.get(f"/requests/{request_id}?messages=1")
    assert locked.status_code == 401
    assert locked.headers["WWW-Authenticate"].startswith("Basic")
    assert "You need to sign in to view that." in locked.get_data(as_text=True)

    shown = client.get(f"/requests/{request_id}?messages=1", headers=basic("admin", "s3cret"))
    assert shown.status_code == 200
    assert "SECRETPROMPT" in shown.get_data(as_text=True)
    assert 'id="conversation-data"' in shown.get_data(as_text=True)


def test_messages_json_requires_login(secured_app):
    db = seed(secured_app)
    request_id = record_message(db)
    client = secured_app.test_client()

    locked = client.get(f"/requests/{request_id}/messages.json")
    assert locked.status_code == 401
    assert locked.headers["WWW-Authenticate"].startswith("Basic")

    shown = client.get(f"/requests/{request_id}/messages.json", headers=basic("admin", "s3cret"))
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


def test_change_credentials_from_settings(secured_app):
    db = seed(secured_app, username="admin", password="oldpass")
    assert db is not None
    client = secured_app.test_client()
    payload = {
        "action": "credentials",
        "admin_username": "admin",
        "admin_password": "newpass",
        "admin_password_confirm": "newpass",
    }

    client.post(
        "/admin/settings",
        data={**payload, "current_password": "wrong"},
        headers=basic("admin", "oldpass"),
    )
    assert client.get("/admin/keys", headers=basic("admin", "newpass")).status_code == 401

    client.post(
        "/admin/settings",
        data={**payload, "current_password": "oldpass"},
        headers=basic("admin", "oldpass"),
    )
    assert client.get("/admin/keys", headers=basic("admin", "newpass")).status_code == 200
    assert client.get("/admin/keys", headers=basic("admin", "oldpass")).status_code == 401
