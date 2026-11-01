from __future__ import annotations

import base64
import json
import secrets
import time

import pytest
from nacl.signing import SigningKey

from midware import adminauth, create_app, sma

BASE = "/_midware/api/v1"
ROUTE_READ = sma.READ_ROUTES
ROUTE_WRITE = sma.WRITE_ROUTES
KEY_WRITE = sma.WRITE_APIKEYS
KEY_READ = sma.READ_APIKEYS_OWNED


class _ProbeClient:
    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"object": "list", "data": []}

    def get(self, url, headers=None):
        return self._Response()


def _app(tmp_path, *, admin_auth: bool, allow_plain: bool = True):
    application = create_app(
        database_path=":memory:",
        config_overrides={
            "TESTING": True,
            "ADMIN_AUTH_ENABLED": admin_auth,
            "IMAGES_DIR": str(tmp_path / "images"),
            "SMA_ENROLL_PER_HOUR": 100,
        },
    )
    application.extensions["midware_http"] = _ProbeClient()
    db = application.extensions["midware_db"]
    db.create_route(name="Default", target_host="https://api.example.com", upstream_key="sk-secret")
    db.mark_configured()
    # Tests speak plain HTTP; the transport guard has its own tests below.
    db.set_sma_allow_plain_http(allow_plain)
    return application, db


@pytest.fixture()
def sma_env(tmp_path):
    app, db = _app(tmp_path, admin_auth=False)
    return app.test_client(), db


@pytest.fixture()
def secured(tmp_path):
    app, db = _app(tmp_path, admin_auth=True)
    with app.app_context():
        adminauth.set_credentials("admin", "s3cret")
    client = app.test_client()
    client.post("/signin", data={"username": "admin", "password": "s3cret"})
    return app, client, db


def _key() -> tuple[SigningKey, str]:
    signing = SigningKey.generate()
    return signing, base64.b64encode(bytes(signing.verify_key)).decode()


def _enroll(client, signing, public_key, *, scopes=None, name="Bot", redirect=None):
    return client.post(
        f"{BASE}/EnrollSystemAccount",
        json={
            "name": name,
            "public_key": public_key,
            "redirect_uri": redirect or "https://client.example.com/callback",
            "scopes": scopes if scopes is not None else [ROUTE_READ],
        },
    )


def _signed(
    client,
    signing,
    account_id,
    token,
    method,
    path,
    body=None,
    *,
    timestamp=None,
    nonce=None,
):
    raw = b"" if body is None else json.dumps(body).encode("utf-8")
    stamp = str(int(time.time())) if timestamp is None else str(timestamp)
    nonce = nonce or secrets.token_urlsafe(16)
    signature = signing.sign(
        sma.signature_payload(account_id, method, path, stamp, nonce, raw)
    ).signature
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Midware-Account": str(account_id),
        "X-Midware-Timestamp": stamp,
        "X-Midware-Nonce": nonce,
        "X-Midware-Signature": base64.b64encode(signature).decode("ascii"),
        "Content-Type": "application/json",
    }
    return client.open(path, method=method, data=raw, headers=headers)


def _pending(client, *, scopes=None, name="Bot"):
    signing, public_key = _key()
    response = _enroll(client, signing, public_key, scopes=scopes, name=name)
    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    return signing, payload["account_id"], payload["pending_token"]


def _approve(db, account_id):
    activation = secrets.token_urlsafe(32)
    db.update_system_account(
        account_id,
        state="approved",
        activation_token_hash=sma.hash_token(activation),
        activation_expires_at=sma.expiry(900),
    )
    return activation


def _issue_activation(db, account_id):
    """Attach a consent link without moving the account out of ``pending``."""
    activation = secrets.token_urlsafe(32)
    db.update_system_account(
        account_id,
        activation_token_hash=sma.hash_token(activation),
        activation_expires_at=sma.expiry(900),
    )
    return activation


def _active(client, db, *, scopes=None, name="Bot"):
    signing, account_id, token = _pending(client, scopes=scopes, name=name)
    _approve(db, account_id)
    response = _signed(
        client, signing, account_id, token, "POST", f"{BASE}/ActivateSystemAccount/exchange"
    )
    assert response.status_code == 200, response.get_json()
    return signing, account_id, response.get_json()["token"]


# -- discovery and enrollment ------------------------------------------------


def test_server_key_is_stable_and_well_formed(sma_env):
    client, _ = sma_env
    first = client.get(f"{BASE}/server-key").get_json()
    second = client.get(f"{BASE}/server-key").get_json()
    assert first["server"]["public_key"] == second["server"]["public_key"]
    assert len(base64.b64decode(first["server"]["public_key"])) == 32
    assert first["server"]["key_id"] == second["server"]["key_id"]


def test_enroll_creates_pending_account(sma_env):
    client, db = sma_env
    signing, public_key = _key()
    response = _enroll(client, signing, public_key)
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["state"] == "pending"
    assert payload["pending_token"].startswith(sma.TOKEN_PENDING_PREFIX)
    assert payload["server"]["public_key"]
    assert db.get_system_account(payload["account_id"])["state"] == "pending"


def test_enroll_rejects_unknown_scope(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    response = _enroll(client, signing, public_key, scopes=["midware.root"])
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "unknown_scope"


def test_enroll_rejects_reserved_name(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    response = _enroll(client, signing, public_key, name="Admin")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "reserved_name"


def test_enroll_rejects_non_https_redirect(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    response = _enroll(client, signing, public_key, redirect="http://evil.example.com/cb")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_redirect_uri"


def test_enroll_allows_loopback_http(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    response = _enroll(client, signing, public_key, redirect="http://127.0.0.1:8765/cb")
    assert response.status_code == 201


def test_enroll_rejects_duplicate_public_key(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    assert _enroll(client, signing, public_key).status_code == 201
    again = _enroll(client, signing, public_key, name="Other")
    assert again.status_code == 409
    assert again.get_json()["error"]["code"] == "public_key_already_enrolled"


def test_enroll_throttles_per_address(tmp_path):
    app, db = _app(tmp_path, admin_auth=False)
    app.config["SMA_ENROLL_PER_HOUR"] = 2
    client = app.test_client()
    for index in range(2):
        signing, public_key = _key()
        assert _enroll(client, signing, public_key, name=f"Bot {index}").status_code == 201
    signing, public_key = _key()
    blocked = _enroll(client, signing, public_key, name="Bot 3")
    assert blocked.status_code == 429
    assert blocked.get_json()["error"]["code"] == "enrollment_throttled"


# -- signing -----------------------------------------------------------------


def test_pending_token_cannot_reach_interact(sma_env):
    client, db = sma_env
    signing, account_id, token = _pending(client)
    response = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/routes")
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "wrong_account_state"


def test_signature_replay_is_rejected(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    nonce = secrets.token_urlsafe(16)
    first = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/routes", nonce=nonce
    )
    assert first.status_code == 200
    second = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/routes", nonce=nonce
    )
    assert second.status_code == 401
    assert second.get_json()["error"]["code"] == "replayed_nonce"


def test_signature_cannot_be_moved_to_another_path(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    nonce = secrets.token_urlsafe(16)
    stamp = str(int(time.time()))
    signature = signing.sign(
        sma.signature_payload(account_id, "GET", f"{BASE}/interact/self", stamp, nonce, b"")
    ).signature
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Midware-Account": str(account_id),
        "X-Midware-Timestamp": stamp,
        "X-Midware-Nonce": nonce,
        "X-Midware-Signature": base64.b64encode(signature).decode("ascii"),
    }
    response = client.get(f"{BASE}/interact/routes", headers=headers)
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_signature"


def test_stale_timestamp_is_rejected(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    response = _signed(
        client,
        signing,
        account_id,
        token,
        "GET",
        f"{BASE}/interact/routes",
        timestamp=int(time.time()) - 4000,
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "stale_timestamp"


def test_account_header_must_match_token(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    signature = signing.sign(
        sma.signature_payload(
            account_id,
            "GET",
            f"{BASE}/interact/self",
            str(int(time.time())),
            secrets.token_urlsafe(16),
            b"",
        )
    ).signature
    response = client.get(
        f"{BASE}/interact/self",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Midware-Account": str(account_id + 1),
            "X-Midware-Timestamp": str(int(time.time())),
            "X-Midware-Nonce": secrets.token_urlsafe(16),
            "X-Midware-Signature": base64.b64encode(signature).decode("ascii"),
        },
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] in {"account_mismatch", "invalid_token"}


def test_response_carries_a_verifiable_signature(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    nonce = secrets.token_urlsafe(16)
    stamp = str(int(time.time()))
    response = _signed(
        client,
        signing,
        account_id,
        token,
        "GET",
        f"{BASE}/interact/self",
        timestamp=stamp,
        nonce=nonce,
    )
    assert response.status_code == 200
    server = client.get(f"{BASE}/server-key").get_json()["server"]
    sma.verify_response_signature(
        server["public_key"],
        account_id,
        "GET",
        f"{BASE}/interact/self",
        stamp,
        nonce,
        response.get_data(),
        response.headers["X-Midware-Server-Signature"],
    )
    assert response.headers["Cache-Control"] == "no-store"


def test_auth_failures_are_signed(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    stamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    # Deliberately sign a different path so the server rejects it.
    signature = signing.sign(
        sma.signature_payload(account_id, "GET", f"{BASE}/interact/self", stamp, nonce, b"")
    ).signature
    response = client.get(
        f"{BASE}/interact/routes",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Midware-Account": str(account_id),
            "X-Midware-Timestamp": stamp,
            "X-Midware-Nonce": nonce,
            "X-Midware-Signature": base64.b64encode(signature).decode("ascii"),
        },
    )
    assert response.status_code == 401
    server = client.get(f"{BASE}/server-key").get_json()["server"]
    sma.verify_response_signature(
        server["public_key"],
        account_id,
        "GET",
        f"{BASE}/interact/routes",
        stamp,
        nonce,
        response.get_data(),
        response.headers["X-Midware-Server-Signature"],
    )


def test_unknown_api_path_is_signed_json(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    response = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/nope")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"
    assert "X-Midware-Server-Signature" in response.headers


def test_midware_key_is_rejected_on_sma(sma_env):
    client, db = sma_env
    token = db.create_api_key("client")
    response = client.get(
        f"{BASE}/interact/routes", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_token"


def test_sma_token_is_rejected_on_the_proxy(sma_env):
    client, db = sma_env
    _, _, token = _active(client, db)
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt", "messages": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_api_key"


# -- scopes and data ---------------------------------------------------------


def test_scope_denied_is_explicit(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[sma.READ_MSQ])
    response = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/routes")
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "scope_denied"


def test_read_routes_redacts_the_upstream_key(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    response = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/routes")
    assert response.status_code == 200
    row = response.get_json()["data"][0]
    assert "upstream_key" not in row
    assert row["has_upstream_key"] is True


def test_route_create_and_delete(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[ROUTE_WRITE])
    created = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/routes/create",
        {"name": "Second", "target_host": "https://second.example.com/v1"},
    )
    assert created.status_code == 201, created.get_json()
    route_id = created.get_json()["id"]
    deleted = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/routes/{route_id}/delete",
    )
    assert deleted.status_code == 200
    assert db.get_route(route_id) is None


def test_apikey_create_requires_a_restriction(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[KEY_WRITE])
    response = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/apikeys/create",
        {"name": "broad"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "apikey_scope_too_broad"


def test_apikey_create_cannot_name_routes_without_read_routes(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[KEY_WRITE])
    route = db.list_routes()[0]
    response = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/apikeys/create",
        {"name": "scoped", "restrict_providers": True, "allowed_route_ids": [route["id"]]},
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "scope_denied"


def test_apikey_create_is_owned_and_token_shown_once(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(
        client, db, scopes=[KEY_WRITE, KEY_READ, ROUTE_READ]
    )
    route = db.list_routes()[0]
    created = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/apikeys/create",
        {
            "name": "scoped",
            "restrict_providers": True,
            "allowed_route_ids": [route["id"]],
        },
    )
    assert created.status_code == 201, created.get_json()
    body = created.get_json()
    assert body["token"].startswith("mw-")
    assert db.get_api_key(body["id"])["owner_system_account_id"] == account_id

    listed = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/apikeys")
    assert listed.status_code == 200
    row = listed.get_json()["data"][0]
    assert "token" not in row


def test_owned_key_update_cannot_widen_to_unrestricted(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(
        client,
        db,
        scopes=[KEY_WRITE, KEY_READ, sma.EDIT_APIKEYS_OWNED, ROUTE_READ],
    )
    route = db.list_routes()[0]
    created = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/apikeys/create",
        {"name": "scoped", "restrict_providers": True, "allowed_route_ids": [route["id"]]},
    )
    key_id = created.get_json()["id"]
    widened = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/apikeys/{key_id}/update",
        {"restrict_providers": False},
    )
    assert widened.status_code == 400
    assert widened.get_json()["error"]["code"] == "apikey_scope_too_broad"


def test_another_account_cannot_read_an_owned_key(sma_env):
    client, db = sma_env
    owner_sign, owner_id, owner_token = _active(
        client, db, scopes=[KEY_WRITE, KEY_READ, ROUTE_READ], name="Owner"
    )
    route = db.list_routes()[0]
    created = _signed(
        client,
        owner_sign,
        owner_id,
        owner_token,
        "POST",
        f"{BASE}/interact/apikeys/create",
        {"name": "scoped", "restrict_providers": True, "allowed_route_ids": [route["id"]]},
    )
    key_id = created.get_json()["id"]

    other_sign, other_id, other_token = _active(
        client, db, scopes=[KEY_WRITE, KEY_READ, sma.EDIT_APIKEYS_OWNED], name="Other"
    )
    denied = _signed(
        client, other_sign, other_id, other_token, "GET", f"{BASE}/interact/apikeys/{key_id}"
    )
    assert denied.status_code == 404
    assert denied.get_json()["error"]["code"] == "api_key_not_found"


def test_msq_create_requires_an_existing_route(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[sma.WRITE_MSQ])
    response = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/msq/create",
        {"name": "probe", "model": "m", "route_id": 9999},
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "route_not_found"


# -- consent -----------------------------------------------------------------


def test_activate_returns_a_consent_link(sma_env):
    client, _ = sma_env
    signing, account_id, token = _pending(client)
    response = _signed(
        client, signing, account_id, token, "POST", f"{BASE}/ActivateSystemAccount"
    )
    assert response.status_code == 200
    assert "/_midware/authorize/" in response.get_json()["authorization_url"]


def test_consent_page_requires_the_admin(secured):
    app, client, db = secured
    anon = app.test_client()
    signing, account_id, token = _pending(anon)
    activation = _approve(db, account_id)
    response = anon.get(f"/_midware/authorize/{activation}")
    assert response.status_code == 302
    assert "/signin" in response.headers["Location"]


def test_consent_allows_then_exchange_issues_a_live_token(secured):
    app, client, db = secured
    signing, account_id, token = _pending(client)
    activation = _issue_activation(db, account_id)

    page = client.get(f"/_midware/authorize/{activation}")
    assert page.status_code == 200
    assert b"wants to access this MidWare server" in page.data

    csrf = _extract_csrf(page.data)
    allowed = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow"},
    )
    assert allowed.status_code == 302
    assert db.get_system_account(account_id)["state"] == "approved"

    exchanged = _signed(
        client, signing, account_id, token, "POST", f"{BASE}/ActivateSystemAccount/exchange"
    )
    assert exchanged.status_code == 200
    live = exchanged.get_json()["token"]
    assert live.startswith(sma.TOKEN_LIVE_PREFIX)
    assert db.get_system_account(account_id)["state"] == "active"


def test_consent_post_without_csrf_is_rejected(secured):
    app, client, db = secured
    _, account_id, _ = _pending(client)
    activation = _approve(db, account_id)
    response = client.post(
        f"/_midware/authorize/{activation}", data={"decision": "allow"}
    )
    assert response.status_code == 400
    assert db.get_system_account(account_id)["state"] == "approved"


def test_consent_get_cannot_activate(secured):
    app, client, db = secured
    _, account_id, _ = _pending(client)
    activation = _approve(db, account_id)
    client.get(f"/_midware/authorize/{activation}")
    assert db.get_system_account(account_id)["state"] == "approved"


def test_consent_refuses_a_tampered_payload(secured):
    app, client, db = secured
    _, account_id, _ = _pending(client)
    activation = _approve(db, account_id)
    db.update_system_account(account_id, scopes=json.dumps([sma.WRITE_ROUTES]))

    page = client.get(f"/_midware/authorize/{activation}")
    assert b"does not match the details that were enrolled" in page.data
    csrf = _extract_csrf(page.data)
    response = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow"},
    )
    assert response.status_code == 409
    assert db.get_system_account(account_id)["state"] == "approved"


def test_result_page_reflects_the_decision_and_resists_forgery(secured):
    app, client, db = secured
    _, account_id, _ = _pending(client)
    activation = _issue_activation(db, account_id)
    page = client.get(f"/_midware/authorize/{activation}")
    assert page.headers["X-Frame-Options"] == "DENY"
    csrf = _extract_csrf(page.data)
    decided = client.post(
        f"/_midware/authorize/{activation}", data={"csrf": csrf, "decision": "deny"}
    )
    result = client.get(decided.headers["Location"])
    assert b"Denied" in result.data

    # A fresh browser cannot fabricate an approval through the query string.
    forged = app.test_client().get(
        f"/_midware/authorize/{activation}/result?state=approved"
    )
    assert b"Denied" not in forged.data
    assert b"Approved" not in forged.data


def test_denied_account_cannot_exchange(secured):
    app, client, db = secured
    signing, account_id, token = _pending(client)
    activation = _issue_activation(db, account_id)
    page = client.get(f"/_midware/authorize/{activation}")
    csrf = _extract_csrf(page.data)
    client.post(f"/_midware/authorize/{activation}", data={"csrf": csrf, "decision": "deny"})
    assert db.get_system_account(account_id)["state"] == "denied"

    exchanged = _signed(
        client, signing, account_id, token, "POST", f"{BASE}/ActivateSystemAccount/exchange"
    )
    assert exchanged.status_code == 403
    assert exchanged.get_json()["error"]["code"] == "wrong_account_state"


def _extract_csrf(page: bytes) -> str:
    marker = b'name="csrf" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index(b'"', start)].decode("utf-8")


# -- lifecycle ---------------------------------------------------------------


def test_disabled_switch_refuses_every_call(sma_env):
    client, db = sma_env
    db.set_sma_enabled(False)
    response = client.get(f"{BASE}/server-key")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "sma_disabled"


def test_self_disable_revokes_the_account(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    disabled = _signed(
        client, signing, account_id, token, "POST", f"{BASE}/DisableSystemAccount"
    )
    assert disabled.status_code == 200
    again = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/self")
    assert again.status_code == 401


def test_revoke_deletes_the_keys_it_minted(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(
        client, db, scopes=[KEY_WRITE, KEY_READ, ROUTE_READ]
    )
    route = db.list_routes()[0]
    created = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/apikeys/create",
        {"name": "scoped", "restrict_providers": True, "allowed_route_ids": [route["id"]]},
    )
    key_id = created.get_json()["id"]
    sma.revoke_account(db, account_id, "test")
    assert db.get_api_key(key_id) is None
    assert db.get_system_account(account_id)["token_hash"] is None


def test_admin_revoke_takes_effect_immediately(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db)
    db.update_system_account(
        account_id, state="revoked", token_hash=None, pending_token_hash=None
    )
    response = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/self")
    assert response.status_code == 401


def test_status_reports_a_denial(secured):
    app, client, db = secured
    signing, account_id, token = _pending(client)
    activation = _issue_activation(db, account_id)
    page = client.get(f"/_midware/authorize/{activation}")
    csrf = _extract_csrf(page.data)
    client.post(f"/_midware/authorize/{activation}", data={"csrf": csrf, "decision": "deny"})

    status = _signed(
        client,
        signing,
        account_id,
        token,
        "GET",
        f"{BASE}/ActivateSystemAccount/status",
    )
    assert status.status_code == 200
    assert status.get_json()["state"] == "denied"


def test_expired_pending_token_is_refused(sma_env):
    client, db = sma_env
    signing, account_id, token = _pending(client)
    db.update_system_account(account_id, expires_at=sma.since(60))
    db.expire_system_accounts()
    response = _signed(
        client, signing, account_id, token, "POST", f"{BASE}/ActivateSystemAccount"
    )
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "invalid_token"


def test_key_status_needs_its_own_scope(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[sma.READ_ROUTES])
    key_id = db.get_api_key_by_token(db.create_api_key("human"))["id"]

    for method, path in (
        ("GET", f"{BASE}/interact/apikeys/{key_id}/status"),
        ("POST", f"{BASE}/interact/apikeys/{key_id}/disable"),
    ):
        response = _signed(client, signing, account_id, token, method, path)
        assert response.status_code == 403
        assert response.get_json()["error"]["code"] == "scope_denied"


def test_key_status_toggles_any_key(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(
        client, db, scopes=[sma.READ_APIKEYS_STATUS, sma.WRITE_APIKEYS_STATUS]
    )
    human_token = db.create_api_key("human")
    key_id = db.get_api_key_by_token(human_token)["id"]

    status = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/apikeys/{key_id}/status"
    )
    assert status.status_code == 200
    assert status.get_json()["enabled"] is True

    disabled = _signed(
        client, signing, account_id, token, "POST", f"{BASE}/interact/apikeys/{key_id}/disable"
    )
    assert disabled.status_code == 200
    assert disabled.get_json()["enabled"] is False
    assert db.get_api_key(key_id)["enabled"] == 0

    blocked = client.get("/v1/models", headers={"Authorization": f"Bearer {human_token}"})
    assert blocked.status_code == 403
    assert blocked.get_json()["error"]["code"] == "api_key_disabled"

    enabled = _signed(
        client, signing, account_id, token, "POST", f"{BASE}/interact/apikeys/{key_id}/enable"
    )
    assert enabled.status_code == 200
    allowed = client.get("/v1/models", headers={"Authorization": f"Bearer {human_token}"})
    assert allowed.status_code == 200


def test_system_key_status_is_hidden(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(
        client, db, scopes=[sma.READ_APIKEYS_STATUS, sma.WRITE_APIKEYS_STATUS]
    )
    key_id = db.get_or_create_api_key("MSQ Measurements")

    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/apikeys/{key_id}/status"
    )
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "api_key_not_found"


def test_key_status_listing_covers_every_key(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[sma.READ_APIKEYS_STATUS])
    first = db.get_api_key_by_token(db.create_api_key("one"))["id"]
    second = db.get_api_key_by_token(db.create_api_key("two"))["id"]
    db.set_api_key_enabled(second, False)
    db.get_or_create_api_key("MSQ Measurements")

    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/apikeys/status"
    )
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.get_json()["data"]}
    assert set(rows) == {first, second}
    assert rows[first]["enabled"] is True
    assert rows[second]["enabled"] is False
    assert "token" not in rows[first]


def test_every_sma_endpoint_declares_a_scope():
    app, _ = _app(__import__("pathlib").Path("/tmp"), admin_auth=False)
    open_endpoints = {
        "sma.server_key",
        "sma.enroll",
        "sma.activate",
        "sma.status",
        "sma.exchange",
        "sma.self_info",
        "sma.disable_self",
    }
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith("sma."):
            continue
        view = app.view_functions[rule.endpoint]
        declared = getattr(view, "__sma_scope__", None)
        assert declared or rule.endpoint in open_endpoints, rule.endpoint


def test_plain_http_is_refused_by_default(tmp_path):
    app, db = _app(tmp_path, admin_auth=False, allow_plain=False)
    client = app.test_client()
    response = client.get(f"{BASE}/server-key")
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "insecure_transport"


def test_https_is_accepted_without_the_plain_http_switch(tmp_path):
    app, db = _app(tmp_path, admin_auth=False, allow_plain=False)
    client = app.test_client()
    response = client.get(
        f"{BASE}/server-key", environ_overrides={"wsgi.url_scheme": "https"}
    )
    assert response.status_code == 200
    assert response.get_json()["transport"] == "tls"
    assert db.sma_allow_plain_http(True) is False


def test_plain_http_opt_in_unlocks_the_api(tmp_path):
    app, db = _app(tmp_path, admin_auth=False, allow_plain=False)
    db.set_sma_allow_plain_http(True)
    response = app.test_client().get(f"{BASE}/server-key")
    assert response.status_code == 200
    assert response.get_json()["transport"] == "plain"


def test_plain_http_opt_in_round_trips_through_settings(secured):
    app, client, db = secured
    db.set_sma_allow_plain_http(False)
    client.post(
        "/admin/settings",
        data={
            "sma_present": "1",
            "sma_enabled": "1",
            "sma_allow_present": "1",
            "sma_allow_plain_http": "1",
            "request_log_limit": "10",
        },
    )
    assert db.sma_allow_plain_http(False) is True

    # Turning the master switch off also clears the transport opt-in.
    client.post(
        "/admin/settings",
        data={"sma_present": "1", "request_log_limit": "10"},
    )
    assert db.sma_enabled(True) is False
    assert db.sma_allow_plain_http(False) is False


def test_settings_hides_retired_accounts(secured):
    app, client, db = secured
    _, hidden_id, _ = _pending(client, name="Hidden Bot")
    db.update_system_account(hidden_id, state="revoked", revoked_at=sma.expiry(1))
    _pending(client, name="Visible Bot")

    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert b"Visible Bot" in page.data
    assert b"Hidden Bot" not in page.data


def test_retired_accounts_are_pruned_after_30_days(sma_env):
    client, db = sma_env
    _, fresh_id, _ = _pending(client, name="Fresh")
    _, stale_id, _ = _pending(client, name="Stale")
    db.update_system_account(fresh_id, state="revoked", revoked_at=sma.expiry(1))
    db.update_system_account(stale_id, state="revoked", revoked_at=sma.since(31 * 86_400))

    db.expire_system_accounts()
    assert db.get_system_account(fresh_id) is not None
    assert db.get_system_account(stale_id) is None


def test_settings_toggle_round_trips(secured):
    app, client, db = secured
    page = client.get("/admin/settings")
    assert page.status_code == 200
    assert b"System Management Accounts API" in page.data
    assert db.sma_enabled(True) is True
    response = client.post(
        "/admin/settings",
        data={"sma_present": "1", "request_log_limit": "10"},
    )
    assert response.status_code == 302
    assert db.sma_enabled(True) is False
    client.post(
        "/admin/settings",
        data={"sma_present": "1", "sma_enabled": "1", "request_log_limit": "10"},
    )
    assert db.sma_enabled(True) is True
