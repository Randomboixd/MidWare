from __future__ import annotations


def test_setup_redirects_when_unconfigured(app):
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 302
    assert "/admin/setup" in response.headers["Location"]


def test_setup_creates_route_and_key(app):
    client = app.test_client()
    response = client.post(
        "/admin/setup",
        data={
            "name": "OpenAI",
            "target_host": "https://api.openai.com",
            "upstream_key": "sk-secret",
            "key_name": "laptop",
            "request_log_limit": "7",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    db = app.extensions["midware_db"]
    assert db.is_configured()
    route = db.active_route()
    assert route["target_host"] == "https://api.openai.com"
    assert route["upstream_key"] == "sk-secret"
    assert db.request_log_limit() == 7
    assert db.count_api_keys() == 1


def test_setup_rejects_bad_host(app):
    client = app.test_client()
    response = client.post(
        "/admin/setup",
        data={"target_host": "api.openai.com"},
    )
    assert response.status_code == 200
    assert b"must start with http" in response.data
    assert not app.extensions["midware_db"].is_configured()


def test_dashboard_renders_after_setup(configured_client):
    client, _ = configured_client
    response = client.get("/")
    assert response.status_code == 200
    assert b"Dashboard" in response.data
    assert b"heatmap" in response.data


def test_activity_and_requests_pages_render(configured_client):
    client, _ = configured_client
    assert client.get("/activity").status_code == 200
    assert client.get("/requests").status_code == 200


def test_request_detail_404_for_missing(configured_client):
    client, _ = configured_client
    assert client.get("/requests/999").status_code == 404


def test_create_and_delete_key(configured_client):
    client, _ = configured_client
    app = client.application
    client.post("/admin/keys", data={"action": "create", "name": "second"})
    db = app.extensions["midware_db"]
    assert db.count_api_keys() == 2
    key = [row for row in db.list_api_keys() if row["name"] == "second"][0]
    client.post("/admin/keys", data={"action": "delete", "key_id": key["id"]})
    assert db.count_api_keys() == 1


def test_settings_updates_limit_and_purges(configured_client):
    client, token = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    db.record_request(
        api_key_id=db.list_api_keys()[0]["id"], route_id=None, method="POST",
        request_path="p", model="m", status_code=200, latency_ms=1, streamed=False,
        prompt_tokens=1, completion_tokens=1, total_tokens=2,
    )

    client.post("/admin/settings", data={"request_log_limit": "3"})
    assert db.request_log_limit() == 3

    client.post("/admin/settings", data={"action": "purge"})
    assert db.count_requests() == 0


def test_route_activation_switches_active_route(configured_client):
    client, _ = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    client.post(
        "/admin/routes",
        data={"action": "create", "name": "Anthropic", "target_host": "https://api.anthropic.com"},
    )
    routes = db.list_routes()
    assert len(routes) == 2
    second = routes[1]
    assert db.active_route()["id"] == routes[0]["id"]

    client.post("/admin/routes", data={"action": "activate", "route_id": second["id"]})
    assert db.active_route()["id"] == second["id"]


def test_new_route_is_not_activated_by_default(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    first_id = db.active_route()["id"]

    client.post(
        "/admin/routes",
        data={"action": "create", "name": "Second", "target_host": "https://second.example.com"},
    )

    routes = db.list_routes()
    assert [r["is_active"] for r in routes] == [1, 0]
    assert db.active_route()["id"] == first_id
    assert sum(r["is_active"] for r in routes) == 1


def test_routes_page_marks_exactly_one_default(configured_client):
    client, _ = configured_client
    client.post(
        "/admin/routes",
        data={"action": "create", "name": "Second", "target_host": "https://second.example.com"},
    )

    html = client.get("/admin/routes").get_data(as_text=True)
    assert html.count('<span class="pill ok">default</span>') == 1
    assert html.count(">Make default</button>") >= 1
    assert html.count(">Edit</button>") == 2
    assert html.count(">Delete</button>") == 2
    assert "edit-" + str(client.application.extensions["midware_db"].active_route()["id"]) in html


def test_route_edit_updates_name_and_target(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route = db.active_route()

    client.post(
        "/admin/routes",
        data={
            "action": "update",
            "route_id": route["id"],
            "name": "Renamed",
            "target_host": "https://renamed.example.com/v1",
            "upstream_key": "",
        },
    )

    updated = db.get_route(route["id"])
    assert updated["name"] == "Renamed"
    assert updated["target_host"] == "https://renamed.example.com/v1"
    assert updated["upstream_key"] == route["upstream_key"]
