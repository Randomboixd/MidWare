from __future__ import annotations

import json

from test_proxy import StubResponse, auth_header, install_stub


def models_body(ids):
    return json.dumps({"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}).encode()


def test_extract_model_ids_shapes():
    from midware.models import extract_model_ids, normalize_host

    assert extract_model_ids({"data": [{"id": "a"}, {"id": "b"}]}) == ["a", "b"]
    assert extract_model_ids({"models": [{"name": "llama3:8b"}]}) == ["llama3:8b"]
    assert extract_model_ids(["a", "a", "b"]) == ["a", "b"]
    assert extract_model_ids({"data": [{"model": "x"}, {"id": "y"}]}) == ["x", "y"]
    assert extract_model_ids(None) == []
    assert extract_model_ids("nope") == []

    assert normalize_host("https://API.example.com/v1/") == "https://api.example.com"
    assert normalize_host("https://api.example.com") == "https://api.example.com"


def test_refresh_stores_and_slugs_models(multi_host):
    from midware.models import refresh_all

    app, _ = multi_host
    stub = install_stub(app, StubResponse(status_code=200, content=models_body(["gpt-4o", "text-small"])))

    result = refresh_all(app)

    assert not result["errors"]
    assert result["total"] == 6  # 3 routes, but NanoGPT + Local share 2 each
    db = app.extensions["midware_db"]
    default = db.active_route()
    assert default["name"] == "Default"
    slugs = {m["slug"] for m in db.list_models()}
    assert "gpt-4o" in slugs
    assert "[NanoGPT]gpt-4o" in slugs
    assert "[Local]gpt-4o" in slugs
    assert db.get_meta("models_refreshed_at")
    assert len(stub.gets) == 3


def test_duplicate_hosts_are_fetched_once(app):
    from midware.models import refresh_all

    db = app.extensions["midware_db"]
    db.create_route(name="A", target_host="https://same.example.com/v1", upstream_key="k")
    db.create_route(name="B", target_host="https://same.example.com/v1/", upstream_key="k")
    db.mark_configured()
    stub = install_stub(app, StubResponse(status_code=200, content=models_body(["shared-model"])))

    result = refresh_all(app)

    assert len(stub.gets) == 1
    assert result["total"] == 2
    assert {m["slug"] for m in db.list_models()} == {"shared-model", "[B]shared-model"}


def test_default_host_models_are_bare_and_prefixed(multi_host):
    from midware.models import refresh_all

    app, _ = multi_host
    install_stub(app, StubResponse(status_code=200, content=models_body(["m"])))

    refresh_all(app)

    models = app.extensions["midware_db"].list_models()
    default = [m for m in models if m["is_default"]]
    assert default[0]["slug"] == "m"
    assert "[NanoGPT]m" in {m["slug"] for m in models}


def test_refresh_replaces_previous_models(multi_host):
    from midware.models import refresh_all

    app, _ = multi_host
    install_stub(app, StubResponse(status_code=200, content=models_body(["old"])))
    refresh_all(app)
    install_stub(app, StubResponse(status_code=200, content=models_body(["new"])))

    refresh_all(app)

    slugs = {m["model_id"] for m in app.extensions["midware_db"].list_models()}
    assert slugs == {"new"}


def test_refresh_records_error_without_wiping(multi_host):
    from midware.models import refresh_all

    app, _ = multi_host
    install_stub(app, StubResponse(status_code=200, content=models_body(["keep-me"])))
    refresh_all(app)

    install_stub(app, StubResponse(status_code=500, content=b"{}"))
    result = refresh_all(app)

    assert result["errors"]
    assert "keep-me" in {m["model_id"] for m in app.extensions["midware_db"].list_models()}
    assert app.extensions["midware_db"].get_meta("models_refresh_error")


def test_v1_models_endpoint_serves_catalogue(multi_host):
    from midware.models import refresh_all

    app, token = multi_host
    install_stub(app, StubResponse(status_code=200, content=models_body(["xiaomi/mimo"])))
    refresh_all(app)
    client = app.test_client()

    response = client.get("/v1/models", headers=auth_header(token))

    assert response.status_code == 200
    payload = response.get_json()
    ids = [entry["id"] for entry in payload["data"]]
    assert "[NanoGPT]xiaomi/mimo" in ids
    assert "xiaomi/mimo" in ids  # Default route
    entry = next(e for e in payload["data"] if e["id"] == "[NanoGPT]xiaomi/mimo")
    assert entry["owned_by"] == "NanoGPT"
    assert entry["midware"]["default"] is False


def test_v1_models_requires_auth(configured_client):
    client, _ = configured_client
    assert client.get("/v1/models").status_code == 401


def test_models_slug_round_trips(multi_host):
    from midware.models import refresh_all

    app, token = multi_host
    install_stub(app, StubResponse(status_code=200, content=models_body(["xiaomi/mimo"])))
    refresh_all(app)

    client = app.test_client()
    catalogue = client.get("/v1/models", headers=auth_header(token)).get_json()
    slug = next(e["id"] for e in catalogue["data"] if e["id"].startswith("[NanoGPT]"))

    stub = install_stub(app, StubResponse(status_code=200, content=b'{"usage":{"total_tokens":1}}'))
    response = client.post(
        "/v1/chat/completions",
        json={"model": slug, "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.sent[0][0]["url"] == "https://nano-gpt.com/subscription/v1/chat/completions"
    assert json.loads(stub.sent[0][0]["content"])["model"] == "xiaomi/mimo"


def test_admin_refresh_action_fetches_models(configured_client):
    client, _ = configured_client
    app = client.application
    install_stub(app, StubResponse(status_code=200, content=models_body(["gpt-4o"])))

    response = client.post("/admin/routes", data={"action": "refresh"}, follow_redirects=True)

    assert response.status_code == 200
    assert app.extensions["midware_db"].count_models() == 1
    assert b"gpt-4o" in response.data


def test_activating_route_regenerates_slugs(multi_host):
    from midware.models import refresh_all

    app, _ = multi_host
    install_stub(app, StubResponse(status_code=200, content=models_body(["m"])))
    refresh_all(app)
    db = app.extensions["midware_db"]
    nano = [r for r in db.list_routes() if r["name"] == "NanoGPT"][0]

    client = app.test_client()
    install_stub(app, StubResponse(status_code=200, content=models_body(["m"])))
    client.post("/admin/routes", data={"action": "activate", "route_id": nano["id"]})

    slugs = {m["slug"] for m in db.list_models()}
    assert "m" in slugs                      # NanoGPT is now default → bare
    assert "[Default]m" in slugs             # old default now namespaced
    assert not any(m["slug"] == "[NanoGPT]m" for m in db.list_models())
