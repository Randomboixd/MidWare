"""Feature toggles for premodels and MSQ, both defaulting on.

A toggle hides a surface while the machinery underneath stays functional.
Premodels off only hides the listing and the admin pages; an existing
client's ``<p>-slug`` chat call keeps resolving. MSQ off also stops the
monitoring loop, and the setting is re-read every tick so re-enabling
resumes without a restart.
"""

from __future__ import annotations

import json
import re

from midware import sma
from midware.msq import run_due
from test_msq import make_recorder, sse_chunks
from test_premodels import create_premodel
from test_proxy import StubResponse, install_stub
from test_sma import BASE, _active, _app as _sma_app, _signed


def _db(client):
    return client.application.extensions["midware_db"]


# -- persistence ---------------------------------------------------------------


def test_fresh_db_defaults_both_features_on(app):
    """No settings row means enabled, so a new database and a production
    database that predates the toggles behave identically."""
    database = app.extensions["midware_db"]
    assert database.msq_enabled() is True
    assert database.premodels_enabled() is True


def test_feature_toggles_roundtrip_in_settings_table(db):
    """The toggles are '1'/'0' settings rows, mirroring sma_enabled."""
    db.set_msq_enabled(False)
    db.set_premodels_enabled(False)
    assert db.msq_enabled() is False
    assert db.premodels_enabled() is False

    db.set_msq_enabled(True)
    db.set_premodels_enabled(True)
    assert db.msq_enabled() is True
    assert db.premodels_enabled() is True


# -- model catalogue ----------------------------------------------------------


def _listing_ids(client, token):
    response = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
    return [entry["id"] for entry in response.get_json()["data"]]


def test_models_catalogue_hides_premodels_when_disabled_but_chat_still_applies(configured):
    """The toggle hides the listing surface only: a slug already configured
    in a client keeps resolving, because premodel routing is machinery."""
    app, token = configured
    db = app.extensions["midware_db"]
    create_premodel(db, system_prompt="You are a pirate.", accept_mwvars=False)
    db.set_premodels_enabled(False)
    client = app.test_client()

    assert "<p>-autistic-glm" not in _listing_ids(client, token)

    stub = install_stub(app, StubResponse(content=json.dumps({"model": "m"}).encode()))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-autistic-glm", "messages": [{"role": "user", "content": "hello"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    upstream = json.loads(stub.sent[0][0]["content"])
    assert upstream["model"] == "xiaomi/mimo"
    assert upstream["messages"][0] == {"role": "system", "content": "You are a pirate."}
    assert upstream["messages"][1] == {"role": "user", "content": "hello"}


def test_premodels_reenable_restores_catalogue_listing(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_premodel(db)
    client = app.test_client()

    db.set_premodels_enabled(False)
    assert "<p>-autistic-glm" not in _listing_ids(client, token)

    db.set_premodels_enabled(True)
    assert "<p>-autistic-glm" in _listing_ids(client, token)


# -- admin: premodel pages ----------------------------------------------------


def test_admin_premodels_pages_disabled_state_and_refused_writes(configured_client):
    """Off hides the list and refuses every write with a flash + redirect;
    the rows survive and the pages reopen the moment it is back on."""
    client, _ = configured_client
    db = _db(client)
    create_premodel(db, name="Hidden Preset", slug="hidden-preset")
    db.set_premodels_enabled(False)

    listing = client.get("/admin/premodels")
    assert listing.status_code == 200
    html = listing.get_data(as_text=True)
    assert "Premodels are turned off" in html
    assert 'href="/admin/settings"' in html
    assert "Hidden Preset" not in html

    refused = client.get("/admin/premodels/new", follow_redirects=True)
    assert refused.status_code == 200
    assert "Enable them in Settings to manage presets" in refused.get_data(as_text=True)

    assert client.post("/admin/premodels/new", data={"name": "Sneaky"}).status_code == 302
    assert len(db.list_premodels()) == 1

    assert client.get("/admin/premodels/1/edit").status_code == 302
    assert client.post("/admin/premodels/1/edit", data={"name": "Sneaky"}).status_code == 302
    assert db.get_premodel(1)["name"] == "Hidden Preset"

    delete = client.post("/admin/premodels", data={"action": "delete", "premodel_id": "1"})
    assert delete.status_code == 302
    assert db.get_premodel(1) is not None

    db.set_premodels_enabled(True)
    reopened = client.get("/admin/premodels/new")
    assert reopened.status_code == 200
    assert "Hidden Preset" in client.get("/admin/premodels").get_data(as_text=True)


# -- admin: MSQ pages ---------------------------------------------------------


def test_admin_msq_pages_disabled_state_and_refused_writes(configured_client):
    """Off closes the panel and refuses create/edit/delete/run; the rows
    survive and re-enabling reopens the pages."""
    client, _ = configured_client
    db = _db(client)
    recorder_id = make_recorder(db, db.active_route()["id"], name="Watcher")
    db.set_msq_enabled(False)
    app = client.application

    listing = client.get("/admin/msq/")
    assert listing.status_code == 200
    html = listing.get_data(as_text=True)
    assert "Model Service Quality is turned off" in html
    assert 'href="/admin/settings"' in html
    assert "Watcher" not in html

    refused = client.get("/admin/msq/new", follow_redirects=True)
    assert refused.status_code == 200
    assert "Enable it in Settings to manage recorders" in refused.get_data(as_text=True)

    assert client.post("/admin/msq/new", data={"name": "Sneaky"}).status_code == 302
    assert db.count_msq_recorders() == 1

    assert client.get(f"/admin/msq/{recorder_id}/edit").status_code == 302
    assert client.post(f"/admin/msq/{recorder_id}/edit", data={"name": "Sneaky"}).status_code == 302
    assert db.get_msq_recorder(recorder_id)["name"] == "Watcher"

    assert client.post(f"/admin/msq/{recorder_id}/delete").status_code == 302
    assert db.get_msq_recorder(recorder_id) is not None

    stub = install_stub(app, StubResponse(stream_chunks=sse_chunks("hi")))
    assert client.post(f"/admin/msq/{recorder_id}/run").status_code == 302
    assert stub.sent == []  # monitoring stopped: no probe left the building
    assert db.latest_msq_check(recorder_id) is None

    db.set_msq_enabled(True)
    assert client.get("/admin/msq/new").status_code == 200
    assert "Watcher" in client.get("/admin/msq/").get_data(as_text=True)


# -- SMA: the msq surface -----------------------------------------------------


def test_sma_msq_endpoints_answer_feature_disabled(tmp_path):
    """Every interact/msq endpoint refuses with the distinct feature_disabled
    code while the toggle is off; the surface returns with the toggle."""
    app, db = _sma_app(tmp_path, admin_auth=False)
    client = app.test_client()
    route_id = db.list_routes()[0]["id"]
    recorder_id = db.create_msq_recorder(name="probe", route_id=route_id, model="m")
    db.set_msq_enabled(False)
    signing, account_id, token = _active(
        client, db, scopes=[sma.READ_MSQ, sma.WRITE_MSQ, sma.EDIT_MSQ]
    )

    calls = [
        ("GET", f"{BASE}/interact/msq", None),
        ("GET", f"{BASE}/interact/msq/{recorder_id}", None),
        ("POST", f"{BASE}/interact/msq/create", {"name": "New", "model": "m"}),
        ("POST", f"{BASE}/interact/msq/{recorder_id}/update", {"name": "Renamed"}),
        ("POST", f"{BASE}/interact/msq/{recorder_id}/delete", None),
        ("POST", f"{BASE}/interact/msq/{recorder_id}/run", None),
        ("GET", f"{BASE}/interact/msq/{recorder_id}/checks", None),
    ]
    for method, path, body in calls:
        response = _signed(client, signing, account_id, token, method, path, body)
        assert response.status_code == 403, (path, response.get_json())
        assert response.get_json()["error"]["code"] == "feature_disabled"

    assert db.count_msq_recorders() == 1  # no create slipped through
    db.set_msq_enabled(True)
    restored = _signed(
        client, signing, account_id, token, "GET", f"{BASE}/interact/msq/{recorder_id}/checks"
    )
    assert restored.status_code == 200


# -- scheduler ----------------------------------------------------------------


def test_run_due_noop_while_disabled_and_resumes_after_reenable(configured):
    """Disabling MSQ stops the monitoring, and the setting is read fresh every
    tick so re-enabling resumes without a restart."""
    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, db.active_route()["id"])
    db.set_msq_enabled(False)
    stub = install_stub(app, StubResponse(stream_chunks=sse_chunks("hi")))

    assert run_due(app) == []
    assert db.latest_msq_check(recorder_id) is None
    assert stub.sent == []  # monitoring stopped: no probe left the building

    db.set_msq_enabled(True)
    assert len(run_due(app)) == 1
    assert db.latest_msq_check(recorder_id) is not None
    assert stub.sent != []


# -- settings ------------------------------------------------------------------


def _checked(page_html, name):
    return re.search(rf'name="{name}"[^>]*\bchecked\b', page_html)


def test_settings_features_form_persists_both_toggles(configured_client):
    """A Features section on the settings page carries both checkboxes,
    saves them under its own action, and renders the stored values."""
    client, _ = configured_client
    db = _db(client)

    page = client.get("/admin/settings").get_data(as_text=True)
    assert 'name="action" value="features"' in page
    assert _checked(page, "msq_enabled")
    assert _checked(page, "premodels_enabled")
    # the labels state the consequences of turning each toggle off
    assert "stops the scheduled monitoring" in page
    assert "keep working" in page

    saved = client.post("/admin/settings", data={"action": "features"}, follow_redirects=True)
    assert saved.status_code == 200
    assert "Features saved" in saved.get_data(as_text=True)
    assert db.msq_enabled() is False
    assert db.premodels_enabled() is False

    page = client.get("/admin/settings").get_data(as_text=True)
    assert not _checked(page, "msq_enabled")
    assert not _checked(page, "premodels_enabled")

    client.post(
        "/admin/settings",
        data={"action": "features", "msq_enabled": "1", "premodels_enabled": "1"},
    )
    assert db.msq_enabled() is True
    assert db.premodels_enabled() is True
