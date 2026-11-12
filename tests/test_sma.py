from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import timedelta

import pytest
from nacl.signing import SigningKey

from midware import adminauth, create_app, sma
from midware.db import to_iso, utcnow

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
    assert b"This will allow" in page.data
    assert b"Indie" in page.data  # default company label for an unnamed client
    assert b"Read provider routes" in page.data
    assert b"midware.read:routes" in page.data

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
        declared_any = getattr(view, "__sma_scopes__", ())
        assert declared or declared_any or rule.endpoint in open_endpoints, rule.endpoint


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


# -- read scopes: request log, usage, models ---------------------------------


def _seed_request(
    db,
    *,
    route_id=None,
    api_key_id=None,
    status: int = 200,
    model: str = "deepseek-v3.2",
    total: int = 120,
    when: str | None = None,
    prompt: str | None = "PROMPT SECRET",
    answer: str | None = "ANSWER SECRET",
) -> int:
    """Insert a request row directly so ``created_at`` can be backdated."""
    with db.write() as conn:
        cur = conn.execute(
            """
            INSERT INTO requests (
                created_at, request_path, method, model, model_raw, host_prefix,
                status_code, latency_ms, streamed, prompt_tokens, completion_tokens,
                total_tokens, token_source, upstream_request_id, error, request_body,
                response_body, route_id, api_key_id
            ) VALUES (?, '/v1/chat/completions', 'POST', ?, ?, '', ?, 120, 0, 40, 80, ?,
                      'upstream', 'gen-1', ?, ?, ?, ?, ?)
            """,
            (
                when or to_iso(utcnow()),
                model,
                model,
                status,
                total,
                "boom" if status >= 400 else None,
                prompt,
                answer,
                route_id,
                api_key_id,
            ),
        )
        return int(cur.lastrowid)


def test_new_read_scopes_can_be_granted(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    response = _enroll(
        client,
        signing,
        public_key,
        scopes=[sma.READ_REQUESTS, sma.READ_USAGE, sma.READ_MODELS],
        name="Reader",
    )
    assert response.status_code == 201, response.get_json()
    assert set(response.get_json()["scopes"]) == {
        sma.READ_REQUESTS,
        sma.READ_USAGE,
        sma.READ_MODELS,
    }


def test_read_endpoints_need_their_scope(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[sma.READ_MSQ])
    for path in (
        f"{BASE}/interact/requests",
        f"{BASE}/interact/usage",
        f"{BASE}/interact/usage/series",
        f"{BASE}/interact/usage/breakdown?by=route",
        f"{BASE}/interact/models",
    ):
        response = _signed(client, signing, account_id, token, "GET", path)
        assert response.status_code == 403, path
        assert response.get_json()["error"]["code"] == "scope_denied"


def test_request_log_never_returns_content(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    _seed_request(db, route_id=route["id"])
    signing, account_id, token = _active(client, db, scopes=[sma.READ_REQUESTS])

    response = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/requests")
    assert response.status_code == 200
    raw = response.get_data(as_text=True)
    assert "PROMPT SECRET" not in raw
    assert "ANSWER SECRET" not in raw

    row = response.get_json()["data"][0]
    assert "request_body" not in row
    assert "response_body" not in row
    assert row["model"] == "deepseek-v3.2"
    assert row["total_tokens"] == 120
    assert row["route_name"] == route["name"]


def test_request_log_filters_and_caps(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    _seed_request(db, route_id=route["id"], status=200, model="alpha")
    _seed_request(db, route_id=route["id"], status=500, model="beta")
    signing, account_id, token = _active(client, db, scopes=[sma.READ_REQUESTS])

    def call(query: str) -> dict:
        response = _signed(
            client, signing, account_id, token, "GET", f"{BASE}/interact/requests{query}"
        )
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    assert call("")["total"] == 2
    only_failed = call("?status=500")
    assert only_failed["total"] == 1
    assert only_failed["data"][0]["model"] == "beta"
    assert call("?model=alpha")["total"] == 1

    page = call("?limit=1")
    assert len(page["data"]) == 1
    assert page["total"] == 2
    assert call("?limit=9999")["limit"] == 500

    tomorrow = to_iso(utcnow() + timedelta(days=1))
    assert call(f"?since={tomorrow}")["total"] == 0
    yesterday = to_iso(utcnow() - timedelta(days=1))
    assert call(f"?since={yesterday}")["total"] == 2

    bad = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests?status=abc"
    )
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "invalid_query"


def test_request_log_since_and_until_days(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    old = to_iso(utcnow() - timedelta(days=10))
    _seed_request(db, route_id=route["id"], when=old)
    _seed_request(db, route_id=route["id"])
    signing, account_id, token = _active(client, db, scopes=[sma.READ_REQUESTS])
    yesterday = (utcnow() - timedelta(days=1)).date().isoformat()

    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests?since={yesterday}"
    )
    assert response.get_json()["total"] == 1

    cutoff = (utcnow() - timedelta(days=5)).date().isoformat()
    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests?until={cutoff}"
    )
    assert response.get_json()["total"] == 1


def test_request_detail_and_404(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    request_id = _seed_request(db, route_id=route["id"])
    signing, account_id, token = _active(client, db, scopes=[sma.READ_REQUESTS])

    one = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests/{request_id}"
    )
    assert one.status_code == 200
    assert one.get_json()["id"] == request_id

    missing = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests/999999"
    )
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "request_not_found"


def _seed_usage(db, *, route_id=None, api_key_id=None, tokens=100, latency=200) -> None:
    db.record_request(
        api_key_id=api_key_id,
        route_id=route_id,
        method="POST",
        request_path="/v1/chat/completions",
        model="deepseek-v3.2",
        status_code=200,
        latency_ms=latency,
        streamed=False,
        prompt_tokens=tokens // 4,
        completion_tokens=tokens - tokens // 4,
        total_tokens=tokens,
        log_messages=False,
    )


def test_usage_totals_by_period(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    _seed_usage(db, route_id=route["id"], tokens=100)
    _seed_usage(db, route_id=route["id"], tokens=50)
    signing, account_id, token = _active(client, db, scopes=[sma.READ_USAGE])

    day = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/usage")
    body = day.get_json()
    assert body["period"] == "day"
    assert body["requests"] == 2
    assert body["total_tokens"] == 150

    every = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/usage?period=all"
    )
    assert every.get_json()["total_tokens"] == 150

    bad = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/usage?period=century"
    )
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "invalid_query"


def test_usage_series_is_zero_filled(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    _seed_usage(db, route_id=route["id"], tokens=40)
    signing, account_id, token = _active(client, db, scopes=[sma.READ_USAGE])

    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/usage/series?days=7"
    )
    body = response.get_json()
    assert body["days"] == 7
    assert len(body["data"]) == 7
    assert body["data"][-1]["day"] == utcnow().date().isoformat()
    assert sum(row["total_tokens"] for row in body["data"]) == 40
    assert body["data"][0]["requests"] == 0


def test_usage_breakdown_dimensions(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    _seed_usage(db, route_id=route["id"], tokens=100)
    signing, account_id, token = _active(client, db, scopes=[sma.READ_USAGE])

    by_model = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/usage/breakdown?by=model"
    )
    assert by_model.status_code == 200
    assert by_model.get_json()["data"][0]["model"] == "deepseek-v3.2"

    by_route = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/usage/breakdown?by=route"
    ).get_json()["data"][0]
    assert by_route["id"] == route["id"]
    assert by_route["name"] == route["name"]

    by_prefix = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/usage/breakdown?by=prefix"
    )
    assert by_prefix.status_code == 200
    assert "prefix" in by_prefix.get_json()["data"][0]

    by_key = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/usage/breakdown?by=key"
    ).get_json()["data"][0]
    assert by_key["id"] is None and by_key["name"] == "unknown"

    bad = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/usage/breakdown?by=vibes"
    )
    assert bad.status_code == 400
    assert bad.get_json()["error"]["code"] == "invalid_query"


def test_model_catalogue_and_key_round_trip(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    db.replace_models(route["id"], ["deepseek-v3.2", "mistral-large"])
    db.set_meta("models_refreshed_at", "2026-11-11T04:00:12+00:00")
    db.set_meta("models_refresh_error", "")

    signing, account_id, token = _active(
        client, db, scopes=[sma.READ_MODELS, sma.WRITE_APIKEYS, ROUTE_READ]
    )
    response = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/models")
    assert response.status_code == 200
    body = response.get_json()
    assert body["meta"]["refreshed_at"] == "2026-11-11T04:00:12+00:00"
    assert len(body["data"]) == 2
    entry = body["data"][0]
    assert entry["key"] == f"{route['id']}:{entry['model_id']}"
    assert entry["host"] == route["target_host"]
    assert entry["route_name"] == route["name"]

    created = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/apikeys/create",
        {
            "name": "pinned",
            "restrict_models": True,
            "allowed_model_ids": [entry["key"]],
        },
    )
    assert created.status_code == 201, created.get_json()


def test_model_catalogue_filters(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    db.replace_models(route["id"], ["only-model"])
    signing, account_id, token = _active(client, db, scopes=[sma.READ_MODELS])

    def call(query: str):
        return _signed(
            client, signing, account_id, token, "GET", f"{BASE}/interact/models{query}"
        )

    assert len(call(f"?route_id={route['id']}").get_json()["data"]) == 1
    assert len(call(f"?host={route['target_host']}").get_json()["data"]) == 1
    assert call("?host=https://elsewhere.example").get_json()["data"] == []
    assert call("?route_id=9999").status_code == 404


def test_msq_check_history(sma_env):
    client, db = sma_env
    route = db.list_routes()[0]
    recorder_id = db.create_msq_recorder(
        name="probe", route_id=route["id"], model="deepseek-v3.2"
    )
    for index in range(3):
        db.record_msq_check(
            recorder_id=recorder_id,
            status_code=200,
            ok=True,
            ttft_ms=100 + index,
            total_ms=200,
            completion_tokens=10,
            tokens_per_second=55.0,
            score=0.9,
            response_excerpt="fine",
        )
    signing, account_id, token = _active(client, db, scopes=[sma.READ_MSQ])

    response = _signed(
        client,
        signing,
        account_id,
        token,
        "GET",
        f"{BASE}/interact/msq/{recorder_id}/checks?limit=2",
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert len(data) == 2
    assert data[0]["id"] > data[1]["id"]  # newest first
    assert data[0]["tokens_per_second"] == 55.0

    missing = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/msq/9999/checks"
    )
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "msq_not_found"


# -- company label -----------------------------------------------------------


def test_company_defaults_to_indie(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    response = _enroll(client, signing, public_key, name="Anon")
    assert response.status_code == 201
    assert response.get_json()["company"] == "Indie"


def test_company_is_stored_and_returned(sma_env):
    client, db = sma_env
    signing, public_key = _key()
    response = client.post(
        f"{BASE}/EnrollSystemAccount",
        json={
            "name": "Hermes Agent Skill",
            "company": "  Torch  ",
            "public_key": public_key,
            "redirect_uri": "https://client.example.com/callback",
            "scopes": [sma.READ_USAGE],
        },
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["company"] == "Torch"
    assert db.get_system_account(body["account_id"])["company"] == "Torch"


def test_company_accepts_the_company_id_alias(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    response = client.post(
        f"{BASE}/EnrollSystemAccount",
        json={
            "name": "Alias Client",
            "company_id": "Torch",
            "public_key": public_key,
            "redirect_uri": "https://client.example.com/callback",
            "scopes": [sma.READ_USAGE],
        },
    )
    assert response.status_code == 201
    assert response.get_json()["company"] == "Torch"


def test_company_rejects_reserved_and_overlong(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    reserved = client.post(
        f"{BASE}/EnrollSystemAccount",
        json={
            "name": "Sneaky",
            "company": "MidWare",
            "public_key": public_key,
            "redirect_uri": "https://client.example.com/callback",
            "scopes": [sma.READ_USAGE],
        },
    )
    assert reserved.status_code == 400
    assert reserved.get_json()["error"]["code"] == "reserved_company"

    signing2, public_key2 = _key()
    too_long = client.post(
        f"{BASE}/EnrollSystemAccount",
        json={
            "name": "Wordy",
            "company": "x" * 49,
            "public_key": public_key2,
            "redirect_uri": "https://client.example.com/callback",
            "scopes": [sma.READ_USAGE],
        },
    )
    assert too_long.status_code == 400
    assert too_long.get_json()["error"]["code"] == "invalid_company"


def test_company_strips_invisible_characters(sma_env):
    client, _ = sma_env
    signing, public_key = _key()
    response = client.post(
        f"{BASE}/EnrollSystemAccount",
        json={
            "name": "Bidi Client",
            "company": "Tor\u202ech\u200b",
            "public_key": public_key,
            "redirect_uri": "https://client.example.com/callback",
            "scopes": [sma.READ_USAGE],
        },
    )
    assert response.status_code == 201
    assert response.get_json()["company"] == "Torch"


def test_company_is_covered_by_the_tamper_hash(secured):
    app, client, db = secured
    signing, account_id, token = _pending(client)
    activation = _issue_activation(db, account_id)
    db.update_system_account(account_id, company="Evil Corp")

    page = client.get(f"/_midware/authorize/{activation}")
    assert b"does not match the details that were enrolled" in page.data


def test_consent_page_headline_names_the_client_and_its_company(secured):
    app, client, db = secured
    signing, public_key = _key()
    enrolled = client.post(
        f"{BASE}/EnrollSystemAccount",
        json={
            "name": "Hermes Agent Skill",
            "company": "Torch",
            "public_key": public_key,
            "redirect_uri": "https://client.example.com/callback",
            "scopes": [sma.READ_USAGE],
        },
    )
    assert enrolled.status_code == 201
    account_id = enrolled.get_json()["account_id"]
    activation = _issue_activation(db, account_id)

    page = client.get(f"/_midware/authorize/{activation}")
    assert b'This will allow "Hermes Agent Skill" by "Torch" to:' in page.data


def test_consent_page_marks_dangerous_scopes(secured):
    app, client, db = secured
    signing, account_id, token = _pending(
        client, scopes=[sma.WRITE_APIKEYS, sma.READ_REQUESTS], name="Mixed"
    )
    activation = _issue_activation(db, account_id)
    page = client.get(f"/_midware/authorize/{activation}")
    data = page.data

    assert data.count(b"DANGEROUS") == 1
    assert b"Can mint credentials that spend your upstream quota" in data
    assert b"Read request data" in data
    assert b"midware.read:requests" in data


# -- request content: the dangerous scope ------------------------------------


CONTENT = sma.READ_REQUEST_CONTENT


def _owned_request(db, account_id, *, prompt="PROMPT SECRET", answer="ANSWER SECRET", body=True):
    """A request made with a key this account owns, with JSON bodies."""
    route = db.list_routes()[0]
    db.create_api_key(
        "owned",
        restrict_providers=True,
        allowed_route_ids=[route["id"]],
        owner_system_account_id=account_id,
    )
    key = [row for row in db.list_api_keys() if row["owner_system_account_id"] == account_id][-1]
    request_body = json.dumps(
        {"model": "deepseek-v3.2", "messages": [{"role": "user", "content": prompt}]}
    )
    response_body = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": answer}}]}
    )
    return _seed_request(
        db,
        route_id=route["id"],
        api_key_id=key["id"],
        prompt=request_body if body else None,
        answer=response_body if body else None,
    )


def test_content_needs_the_dangerous_scope(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[sma.READ_REQUESTS])
    request_id = _owned_request(db, account_id)
    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests/{request_id}/content"
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "scope_denied"


def test_content_reads_only_its_own_keys(sma_env):
    client, db = sma_env
    signing_a, account_a, token_a = _active(client, db, scopes=[CONTENT], name="Owner")
    signing_b, account_b, token_b = _active(client, db, scopes=[CONTENT], name="Stranger")

    mine = _owned_request(db, account_a)

    own = _signed(
        client, signing_a, account_a, token_a, "GET", f"{BASE}/interact/requests/{mine}/content"
    )
    assert own.status_code == 200
    assert "PROMPT SECRET" in own.get_json()["text"]

    theirs = _signed(
        client, signing_b, account_b, token_b, "GET", f"{BASE}/interact/requests/{mine}/content"
    )
    assert theirs.status_code == 404
    assert theirs.get_json()["error"]["code"] == "request_not_found"


def test_content_refuses_rows_whose_key_was_deleted(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[CONTENT])
    route = db.list_routes()[0]
    orphan = _seed_request(db, route_id=route["id"])  # api_key_id is NULL
    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests/{orphan}/content"
    )
    assert response.status_code == 404


def test_content_returns_conversation_text_and_raw(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[CONTENT])
    request_id = _owned_request(db, account_id)
    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests/{request_id}/content"
    )
    assert response.status_code == 200
    body = response.get_json()

    assert "PROMPT SECRET" in body["text"]
    assert "ANSWER SECRET" in body["text"]
    assert "PROMPT SECRET" in body["raw"]["request_body"]
    assert "ANSWER SECRET" in body["raw"]["response_body"]
    assert body["raw"]["truncated"] is False
    assert body["reason"] is None
    assert body["conversation"]["messages"]


def test_content_reports_uncaptured_rows(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[CONTENT])
    request_id = _owned_request(db, account_id, body=False)
    response = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests/{request_id}/content"
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["reason"] == "not_captured"
    assert body["raw"] is None and body["conversation"] is None


def test_content_honours_max_bytes(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[CONTENT])
    request_id = _owned_request(db, account_id, prompt="x" * 5000)
    response = _signed(
        client,
        signing,
        account_id,
        token,
        "GET",
        f"{BASE}/interact/requests/{request_id}/content?max_bytes=1024",
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["max_bytes"] == 1024
    assert len(body["raw"]["request_body"]) == 1024
    assert body["raw"]["truncated"] is True
    assert body["raw"]["request_bytes"] > 1024


def test_content_scope_implies_the_log_view(sma_env):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[CONTENT])
    listed = _signed(client, signing, account_id, token, "GET", f"{BASE}/interact/requests")
    assert listed.status_code == 200

    detail = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/requests/{_owned_request(db, account_id)}"
    )
    assert detail.status_code == 200


# -- consent gate for dangerous scopes --------------------------------------


def test_dangerous_approval_needs_the_password(secured):
    app, client, db = secured
    signing, account_id, token = _pending(client, scopes=[CONTENT], name="Content Bot")
    activation = _issue_activation(db, account_id)
    page = client.get(f"/_midware/authorize/{activation}")
    assert b"potentially dangerous permissions" in page.data
    assert b'name="password"' in page.data

    csrf = _extract_csrf(page.data)
    missing = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow", "password": ""},
    )
    assert missing.status_code == 401
    assert db.get_system_account(account_id)["state"] == "pending"

    wrong = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow", "password": "not-the-password"},
    )
    assert wrong.status_code == 401
    assert b"Incorrect password" in wrong.data
    assert db.get_system_account(account_id)["state"] == "pending"

    right = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow", "password": "s3cret"},
    )
    assert right.status_code == 302
    assert db.get_system_account(account_id)["state"] == "approved"

    events = db._rows(
        "SELECT action FROM system_account_events WHERE account_id = ? ORDER BY id", (account_id,)
    )
    assert "consent.danger_accept" in {row["action"] for row in events}


def test_safe_approval_does_not_ask_for_a_password(secured):
    app, client, db = secured
    signing, account_id, token = _pending(client, scopes=[sma.READ_USAGE], name="Safe Bot")
    activation = _issue_activation(db, account_id)
    page = client.get(f"/_midware/authorize/{activation}")
    assert b"potentially dangerous" not in page.data

    csrf = _extract_csrf(page.data)
    allowed = client.post(
        f"/_midware/authorize/{activation}", data={"csrf": csrf, "decision": "allow"}
    )
    assert allowed.status_code == 302
    assert db.get_system_account(account_id)["state"] == "approved"


def test_repeated_wrong_passwords_burn_the_link(secured):
    app, client, db = secured
    app.config["ADMIN_LOGIN_MAX_ATTEMPTS"] = 2
    adminauth.reset_login_throttle()
    signing, account_id, token = _pending(client, scopes=[CONTENT], name="Probe Bot")
    activation = _issue_activation(db, account_id)
    csrf = _extract_csrf(client.get(f"/_midware/authorize/{activation}").data)

    first = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow", "password": "nope"},
    )
    assert first.status_code == 401

    second = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow", "password": "nope"},
    )
    assert second.status_code == 401

    # The budget is now spent; the next attempt is refused and the link dies.
    third = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow", "password": "nope"},
    )
    assert third.status_code == 429
    assert db.get_system_account(account_id)["state"] == "pending"
    assert db.get_system_account(account_id)["activation_token_hash"] is None
    # The link is dead: 410 Gone, the same answer any spent token gets.
    assert client.get(f"/_midware/authorize/{activation}").status_code == 410
    adminauth.reset_login_throttle()
