"""Regressions for three verified SMA defects.

Each test landed before its fix and was watched to fail against the unfixed
tree (the last one is a deliberate behavior lock on the documented fallback):

1. Nonce retention was anchored to receipt, so a request timestamped inside the
   clock-skew window outlived its own nonce row, and the post-insert prune
   turned the first refused replay into a delete that let the second replay
   of the same request execute.
2. ``midware.write:msq`` could mint 1-second recorders (a paid upstream probe
   per tick) and delete recorders, yet it was one-click approved while weaker
   edit scopes demanded the admin password.
3. The consent link returned by ``ActivateSystemAccount`` was built from the
   request's Host header, so a malicious enrollment could phish the admin
   password with a pixel-perfect consent page served from its own origin.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import datetime, timedelta, timezone

import pytest

from midware import adminauth, sma
from midware import db as midware_db
from midware.blueprints import sma as sma_blueprint
from midware.db import parse_iso

from test_sma import (
    BASE,
    ROUTE_WRITE,
    _app,
    _active,
    _extract_csrf,
    _issue_activation,
    _pending,
    _signed,
)


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


def _signed_headers(
    signing, account_id, token, method, path, raw, stamp, nonce
) -> dict[str, str]:
    """Build the header set once, so a replay is byte-identical to the original."""
    signature = signing.sign(
        sma.signature_payload(account_id, method, path, stamp, nonce, raw)
    ).signature
    return {
        "Authorization": f"Bearer {token}",
        "X-Midware-Account": str(account_id),
        "X-Midware-Timestamp": stamp,
        "X-Midware-Nonce": nonce,
        "X-Midware-Signature": base64.b64encode(signature).decode("ascii"),
        "Content-Type": "application/json",
    }


# -- fix 1: the nonce replay window ------------------------------------------


def test_replay_inside_the_acceptance_window_is_refused_on_both_attempts(
    sma_env, monkeypatch
):
    client, db = sma_env
    signing, account_id, token = _active(client, db, scopes=[ROUTE_WRITE])

    # A captured request signed well ahead of receipt: the server must accept
    # it (skew 250 <= 300, leaving margin for a slow runner), and it stays
    # acceptable until the *signed* timestamp ages out at +550 -- long past a
    # receipt-anchored +360 nonce lifetime.
    start = time.time()
    stamp = str(int(start) + 250)
    nonce = secrets.token_urlsafe(16)
    path = f"{BASE}/interact/routes/create"
    raw = json.dumps({"name": "probed", "target_host": "https://up.example.com"}).encode()
    headers = _signed_headers(signing, account_id, token, "POST", path, raw, stamp, nonce)

    first = client.open(path, method="POST", data=raw, headers=headers)
    assert first.status_code == 201, first.get_json()

    # 400 s after receipt: past the old receipt+360 retention, still 110 s
    # inside the request's own acceptance window.
    future = datetime.fromtimestamp(start + 400, tz=timezone.utc)
    monkeypatch.setattr(midware_db, "utcnow", lambda: future)
    monkeypatch.setattr(sma, "utcnow", lambda: future)
    monkeypatch.setattr(sma_blueprint, "utcnow", lambda: future)

    replay_one = client.open(path, method="POST", data=raw, headers=headers)
    assert replay_one.status_code == 401
    assert replay_one.get_json()["error"]["code"] == "replayed_nonce"

    # The first refusal must not have deleted the row it refused: the second
    # identical replay has to hit the same wall, not create a second route.
    replay_two = client.open(path, method="POST", data=raw, headers=headers)
    assert replay_two.status_code == 401
    assert replay_two.get_json()["error"]["code"] == "replayed_nonce"
    assert [row["name"] for row in db.list_routes()].count("probed") == 1

    # Retention is anchored to the signed timestamp: the row outlives the
    # request's whole acceptance window, not the moment it arrived.
    row = db._rows(
        "SELECT expires_at FROM system_nonces WHERE account_id = ? AND nonce = ?",
        (account_id, nonce),
    )
    assert row, "the accepted request burned no nonce row"
    expires = parse_iso(row[0]["expires_at"])
    assert expires is not None
    anchor = datetime.fromtimestamp(int(stamp) + 360, tz=timezone.utc)
    assert expires >= anchor - timedelta(seconds=2)


# -- fix 2: write:msq is a dangerous scope; recorder interval is floored ------


def test_write_msq_consent_requires_the_admin_password(secured):
    app, client, db = secured
    signing, account_id, token = _pending(client, scopes=[sma.WRITE_MSQ], name="Probe Bot")
    activation = _issue_activation(db, account_id)

    page = client.get(f"/_midware/authorize/{activation}")
    assert b"potentially dangerous permissions" in page.data
    assert b"Can start probes that spend your upstream credit" in page.data
    assert b'name="password"' in page.data

    csrf = _extract_csrf(page.data)
    allowed = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow"},  # one click, no password
    )
    assert allowed.status_code == 401
    assert db.get_system_account(account_id)["state"] == "pending"

    right = client.post(
        f"/_midware/authorize/{activation}",
        data={"csrf": csrf, "decision": "allow", "password": "s3cret"},
    )
    assert right.status_code == 302
    assert db.get_system_account(account_id)["state"] == "approved"


def test_sma_recorder_interval_is_floored_at_the_scheduler_wake(tmp_path):
    app, db = _app(tmp_path, admin_auth=False)
    app.config["MSQ_SCHEDULER_INTERVAL"] = 90
    client = app.test_client()
    signing, account_id, token = _active(client, db, scopes=[sma.WRITE_MSQ])
    route_id = db.list_routes()[0]["id"]

    created = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/msq/create",
        {"name": "chatty", "model": "m", "route_id": route_id, "interval_seconds": 1},
    )
    assert created.status_code == 201, created.get_json()
    # A recorder is a paid upstream probe per tick; the API must not mint one
    # tighter than the scheduler's own wake interval.
    assert created.get_json()["interval_seconds"] == 90


def test_sma_recorder_interval_floor_applies_on_update_too(tmp_path):
    app, db = _app(tmp_path, admin_auth=False)
    app.config["MSQ_SCHEDULER_INTERVAL"] = 90
    client = app.test_client()
    signing, account_id, token = _active(
        client, db, scopes=[sma.WRITE_MSQ, sma.EDIT_MSQ], name="Editor"
    )
    route_id = db.list_routes()[0]["id"]

    created = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/msq/create",
        {"name": "slow", "model": "m", "route_id": route_id, "interval_seconds": 3600},
    )
    assert created.status_code == 201, created.get_json()
    recorder_id = created.get_json()["id"]

    updated = _signed(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/interact/msq/{recorder_id}/update",
        {"interval_seconds": 1},
    )
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["interval_seconds"] == 90


# -- fix 3: the consent link is pinned to PUBLIC_URL when set ----------------


def _signed_with_host(client, signing, account_id, token, method, path, *, host):
    raw = b""
    stamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    signature = signing.sign(
        sma.signature_payload(account_id, method, path, stamp, nonce, raw)
    ).signature
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Midware-Account": str(account_id),
        "X-Midware-Timestamp": stamp,
        "X-Midware-Nonce": nonce,
        "X-Midware-Signature": base64.b64encode(signature).decode("ascii"),
        "Host": host,
    }
    return client.open(path, method=method, data=raw, headers=headers)


def test_consent_link_is_pinned_to_public_url_even_with_a_forged_host(tmp_path):
    app, db = _app(tmp_path, admin_auth=False)
    app.config["PUBLIC_URL"] = "https://midware.example.com"
    client = app.test_client()
    signing, account_id, token = _pending(client)

    response = _signed_with_host(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/ActivateSystemAccount",
        host="evil.example",
    )
    assert response.status_code == 200, response.get_json()
    url = response.get_json()["authorization_url"]
    assert url.startswith("https://midware.example.com/_midware/authorize/")
    assert "evil.example" not in url


def test_consent_link_without_public_url_uses_the_request_origin(tmp_path):
    # Behavior lock, not a regression: with PUBLIC_URL unset (the default,
    # correct only when clients talk to MidWare directly) the link keeps
    # following the request.
    app, db = _app(tmp_path, admin_auth=False)
    client = app.test_client()
    signing, account_id, token = _pending(client)

    response = _signed_with_host(
        client,
        signing,
        account_id,
        token,
        "POST",
        f"{BASE}/ActivateSystemAccount",
        host="midware.local:5000",
    )
    assert response.status_code == 200, response.get_json()
    url = response.get_json()["authorization_url"]
    assert url.startswith("http://midware.local:5000/_midware/authorize/")
