from __future__ import annotations

from midware.db import parse_id_list
from test_proxy import StubResponse, auth_header, install_stub


def _key_id(app):
    return app.extensions["midware_db"].list_api_keys()[0]["id"]


def _create_premodel(db, slug, route_id):
    return db.create_premodel(name=slug, slug=slug, route_id=route_id, model="upstream-model")


def test_key_new_form_renders(configured_client):
    client, _ = configured_client
    page = client.get("/admin/keys/new")
    assert page.status_code == 200
    assert b"Enable Advanced Settings" in page.data


def test_key_create_stores_configuration(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]
    premodel_id = _create_premodel(db, "story", route_id)

    client.post(
        "/admin/keys/new",
        data={
            "name": "web-app",
            "description": "Browser client",
            "cors_allow_origin": "https://app.example.com",
            "allow_premodels": "1",
            "restrict_premodels": "1",
            "allowed_premodel_ids": [str(premodel_id)],
            "restrict_providers": "1",
            "allowed_route_ids": [str(route_id)],
        },
    )

    key = [row for row in db.list_api_keys() if row["name"] == "web-app"][0]
    assert key["description"] == "Browser client"
    assert key["cors_allow_origin"] == "https://app.example.com"
    assert key["restrict_premodels"] == 1
    assert key["restrict_providers"] == 1
    assert parse_id_list(key["allowed_route_ids"]) == {route_id}
    assert parse_id_list(key["allowed_premodel_ids"]) == {premodel_id}


def test_key_edit_updates_configuration(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    key_id = _key_id(client.application)

    assert client.get(f"/admin/keys/{key_id}/edit").status_code == 200

    client.post(
        f"/admin/keys/{key_id}/edit",
        data={"name": "renamed", "description": "updated", "allow_premodels": "1"},
    )

    row = db.get_api_key(key_id)
    assert row["name"] == "renamed"
    assert row["description"] == "updated"
    assert row["allow_premodels"] == 1
    assert row["restrict_providers"] == 0
    assert parse_id_list(row["allowed_route_ids"]) == set()


def test_key_edit_missing_returns_404(configured_client):
    client, _ = configured_client
    assert client.get("/admin/keys/999/edit").status_code == 404


def test_provider_limit_blocks_default_route(configured_client):
    client, token = configured_client
    app = client.application
    app.extensions["midware_db"].update_api_key(
        _key_id(app), restrict_providers=True, allowed_route_ids=[]
    )
    stub = install_stub(app, StubResponse(content=b"{}"))

    response = client.post("/v1/chat/completions", json={"model": "m"}, headers=auth_header(token))

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "provider_not_allowed"
    assert stub.sent == []


def test_provider_limit_blocks_host_prefix(multi_host):
    app, token = multi_host
    client = app.test_client()
    db = app.extensions["midware_db"]
    default_id = db.active_route()["id"]
    db.update_api_key(_key_id(app), restrict_providers=True, allowed_route_ids=[default_id])
    stub = install_stub(app, StubResponse(content=b"{}"))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "[NanoGPT]some-model"},
        headers=auth_header(token),
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "provider_not_allowed"
    assert stub.sent == []


def test_provider_limit_allows_listed_route(multi_host):
    app, token = multi_host
    client = app.test_client()
    db = app.extensions["midware_db"]
    nano = [route for route in db.list_routes() if route["name"] == "NanoGPT"][0]
    db.update_api_key(_key_id(app), restrict_providers=True, allowed_route_ids=[nano["id"]])
    install_stub(app, StubResponse(content=b"{}"))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "[NanoGPT]some-model"},
        headers=auth_header(token),
    )

    assert response.status_code == 200


def test_premodels_disabled_is_rejected(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    _create_premodel(db, "story", db.active_route()["id"])
    db.update_api_key(_key_id(app), allow_premodels=False)
    stub = install_stub(app, StubResponse(content=b"{}"))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-story", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "premodel_not_allowed"
    assert stub.sent == []


def test_premodel_allowlist_permits_listed_only(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    route_id = db.active_route()["id"]
    allowed = _create_premodel(db, "allowed", route_id)
    _create_premodel(db, "blocked", route_id)
    db.update_api_key(_key_id(app), restrict_premodels=True, allowed_premodel_ids=[allowed])
    install_stub(app, StubResponse(content=b"{}"))

    ok = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-allowed", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )
    assert ok.status_code == 200

    denied = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-blocked", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )
    assert denied.status_code == 403
    assert denied.get_json()["error"]["code"] == "premodel_not_allowed"


def test_models_catalogue_filters_restricted_providers(multi_host):
    app, token = multi_host
    client = app.test_client()
    db = app.extensions["midware_db"]
    default = db.active_route()
    nano = [route for route in db.list_routes() if route["name"] == "NanoGPT"][0]
    db.replace_models(default["id"], ["base-model"])
    db.replace_models(nano["id"], ["nano-model"])
    db.update_api_key(_key_id(app), restrict_providers=True, allowed_route_ids=[default["id"]])

    payload = client.get("/v1/models", headers=auth_header(token)).get_json()
    ids = [entry["id"] for entry in payload["data"]]

    assert "base-model" in ids
    assert "[NanoGPT]nano-model" not in ids


def test_models_catalogue_hides_premodels_when_disabled(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    db.replace_models(db.active_route()["id"], ["base-model"])
    _create_premodel(db, "story", db.active_route()["id"])
    db.update_api_key(_key_id(app), allow_premodels=False)

    payload = client.get("/v1/models", headers=auth_header(token)).get_json()
    ids = [entry["id"] for entry in payload["data"]]

    assert "base-model" in ids
    assert "<p>-story" not in ids
