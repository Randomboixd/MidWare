from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from test_admin_auth import login, seed
from test_proxy import StubResponse

START_BODY = {
    "device_code": "dev-123",
    "user_code": "VS8Q-ZY3Q",
    "verification_uri": "https://nano-gpt.com/cli-login/verify",
    "verification_uri_complete": "https://nano-gpt.com/cli-login/verify?code=VS8Q-ZY3Q",
    "expires_in": 600,
    "interval": 2,
}


class FakeUpstream:
    """Stands in for both providers' endpoints and records what MidWare sent.

    The balance endpoints live here too so the balance tests can reuse the
    fake: each attribute is the body to answer with, or None for a refusal.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []
        self.header_log: list[tuple[str, dict | None]] = []
        self.nanogpt_approved = False
        self.nanogpt_balance: dict | None = {"usd_balance": "129.46956147"}
        self.nanogpt_sub_usage: dict | None = {
            "active": True,
            "state": "active",
            "limits": {"dailyInputTokens": 500000, "weeklyInputTokens": 3500000, "dailyImages": None},
            "dailyInputTokens": {
                "used": 87700, "remaining": 412300, "percentUsed": 0.1754,
                "resetAt": 1760000000000, "degraded": False,
            },
            "weeklyInputTokens": {
                "used": 1200000, "remaining": 2300000, "percentUsed": 0.3428,
                "resetAt": 1760300000000, "degraded": False,
            },
            "period": {"currentPeriodEnd": None},
        }
        self.openrouter_credits: dict | None = {
            "data": {"total_credits": 100.5, "total_usage": 25.75}
        }
        self.openrouter_key: dict | None = {"data": {"limit_remaining": 12.5}}
        self.raise_on_balance = False

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json))
        self.header_log.append((url, headers))
        if "cli-login/start" in url:
            return StubResponse(json_body=dict(START_BODY))
        if "cli-login/poll" in url:
            if self.nanogpt_approved:
                return StubResponse(json_body={"status": "approved", "key": "sk-nano-granted"})
            return StubResponse(status_code=202, json_body={"status": "authorization_pending"})
        if "auth/keys" in url:
            return StubResponse(json_body={"key": "sk-or-granted"})
        if "check-balance" in url:
            if self.raise_on_balance:
                raise OSError("connection refused")
            if self.nanogpt_balance is None:
                return StubResponse(status_code=401, json_body={"error": "bad key"})
            return StubResponse(json_body=dict(self.nanogpt_balance))
        raise AssertionError(f"unexpected upstream call: {url}")

    def get(self, url, headers=None):
        self.calls.append((url, None))
        self.header_log.append((url, headers))
        if "check-balance" in url:
            raise AssertionError("check-balance is a POST")
        if self.raise_on_balance:
            raise OSError("connection refused")
        if "subscription" in url and "usage" in url:
            if self.nanogpt_sub_usage is None:
                return StubResponse(status_code=401, json_body={})
            return StubResponse(json_body=dict(self.nanogpt_sub_usage))
        if "/api/v1/credits" in url:
            if self.openrouter_credits is None:
                return StubResponse(status_code=403, json_body={"error": {"code": 403}})
            return StubResponse(json_body=dict(self.openrouter_credits))
        if "/api/v1/key" in url:
            if self.openrouter_key is None:
                return StubResponse(status_code=401, json_body={})
            return StubResponse(json_body=dict(self.openrouter_key))
        raise AssertionError(f"unexpected upstream call: {url}")


def install(app) -> FakeUpstream:
    fake = FakeUpstream()
    app.extensions["midware_http"] = fake
    return fake


def start_nanogpt(client) -> dict:
    response = client.post("/admin/signin/start", json={"provider": "nanogpt"})
    assert response.status_code == 200
    return response.get_json()


def test_nanogpt_start_sends_client_name_and_returns_approval_url(client, app):
    fake = install(app)

    body = start_nanogpt(client)

    assert body["ok"] is True
    assert body["verify_url"] == START_BODY["verification_uri_complete"]
    assert body["interval"] == 2
    assert body["token"]
    assert body["host"] == "https://api.nano-gpt.com/api/v1"
    assert body["name"] == "NanoGPT"
    sent_url, sent_body = fake.calls[0]
    assert "cli-login/start" in sent_url
    assert sent_body == {"client_name": "Midware Request Middleware"}


def test_nanogpt_poll_is_pending_then_grants_the_key(client, app):
    fake = install(app)
    token = start_nanogpt(client)["token"]

    pending = client.post("/admin/signin/poll", json={"token": token})
    assert pending.get_json()["status"] == "pending"

    fake.nanogpt_approved = True
    approved = client.post("/admin/signin/poll", json={"token": token})
    body = approved.get_json()
    assert body["status"] == "approved"
    assert body["key"] == "sk-nano-granted"
    assert body["label"] == "Nano-GPT"
    assert body["name"] == "NanoGPT"
    # The tag travels with the key so the form can name the service it saved.
    assert body["provider"] == "nanogpt"

    # The key goes to the browser only; nothing about it is persisted.
    assert app.extensions["midware_db"].list_routes() == []


def test_nanogpt_poll_with_unknown_token_reports_expired(client):
    response = client.post("/admin/signin/poll", json={"token": "nope"})
    body = response.get_json()
    assert body["ok"] is True
    assert body["status"] == "expired"
    assert body["error"]


def test_nanogpt_signin_survives_without_an_admin_account(secured_client):
    install(secured_client.application)
    assert secured_client.get("/admin/keys").status_code == 302

    response = secured_client.post("/admin/signin/start", json={"provider": "nanogpt"})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_signin_requires_login_once_claimed(secured_app):
    seed(secured_app)
    install(secured_app)
    client = secured_app.test_client()

    locked = client.post("/admin/signin/start", json={"provider": "nanogpt"})
    assert locked.status_code == 401
    assert locked.get_json()["error"]["code"] == "authentication_required"

    login(client)
    opened = client.post("/admin/signin/start", json={"provider": "nanogpt"})
    assert opened.status_code == 200
    assert opened.get_json()["ok"] is True


def test_openrouter_start_builds_the_pkce_authorize_url(client, app):
    install(app)

    response = client.post("/admin/signin/start", json={"provider": "openrouter"})
    body = response.get_json()

    assert body["ok"] is True
    assert body["label"] == "OpenRouter"
    assert body["name"] == "OpenRouter (OAuth)"
    assert body["host"] == "https://openrouter.ai/api/v1"
    parsed = urlparse(body["authorize_url"])
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "openrouter.ai", "/auth")
    query = parse_qs(parsed.query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["callback_url"] == ["http://localhost/admin/signin/openrouter/callback"]
    assert query["key_label"] == ["MidWare"]
    assert len(query["code_challenge"][0]) > 40
    assert query["state"][0] == body["token"]


def test_openrouter_callback_exchanges_the_code_for_a_key(client, app):
    fake = install(app)
    body = client.post("/admin/signin/start", json={"provider": "openrouter"}).get_json()
    state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]

    response = client.get(f"/admin/signin/openrouter/callback?code=abc123&state={state}")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Signed in with OpenRouter" in page
    assert "You can close this tab now" in page
    assert "already created the API key" in page
    # The key itself is never rendered — no field, no copy button — it rides
    # only inside the handoff payload that the route-form tab consumes.
    assert 'id="signin-key"' not in page
    assert "Copy key" not in page
    assert "sk-or-granted" in page
    # The handoff names the flow so the route-form tab can match its own popup.
    assert '"flow"' in page
    assert '"provider": "openrouter"' in page
    assert state in page
    sent_url, sent_body = fake.calls[-1]
    assert "openrouter.ai/api/v1/auth/keys" in sent_url
    assert sent_body["code"] == "abc123"
    assert sent_body["code_challenge_method"] == "S256"
    assert len(sent_body["code_verifier"]) >= 43

    # The exchange consumed the flow: the same link cannot be replayed.
    replay = client.get(f"/admin/signin/openrouter/callback?code=abc123&state={state}")
    assert replay.status_code == 400


def test_openrouter_callback_rejects_unknown_state(client, app):
    fake = install(app)

    response = client.get("/admin/signin/openrouter/callback?code=abc&state=unknown")

    assert response.status_code == 400
    assert "Start again" in response.get_data(as_text=True)
    assert not any("auth/keys" in url for url, _ in fake.calls)


def test_openrouter_callback_reports_a_denied_authorization(client, app):
    install(app)

    response = client.get("/admin/signin/openrouter/callback?error=access_denied")

    assert response.status_code == 400
    assert "denied" in response.get_data(as_text=True)


def test_openrouter_callback_requires_login_once_claimed(secured_app):
    seed(secured_app)
    install(secured_app)
    client = secured_app.test_client()

    locked = client.get("/admin/signin/openrouter/callback?code=a&state=b")
    assert locked.status_code == 302
    assert "/signin" in locked.headers["Location"]

    login(client)
    assert client.get("/admin/signin/openrouter/callback?code=a&state=b").status_code == 400


def test_routes_page_offers_service_signin(configured_client):
    client, _ = configured_client
    html = client.get("/admin/routes").get_data(as_text=True)

    assert html.count("Sign in with service") == 2  # create form + edit drawer
    assert 'data-signin-provider="nanogpt"' in html
    assert 'data-signin-provider="openrouter"' in html
    assert "window.MidWareSignin" in html
    assert "signin.js" in html


def test_setup_page_offers_service_signin(client):
    html = client.get("/admin/setup").get_data(as_text=True)

    assert "Sign in with service" in html
    assert 'data-signin-provider="nanogpt"' in html
    assert 'data-signin-provider="openrouter"' in html
    assert "signin.js" in html


def test_nanogpt_route_name_dedups_except_for_the_edited_route(client, app):
    db = app.extensions["midware_db"]
    nano_id = db.create_route(name="NanoGPT", target_host="https://example.com", upstream_key="x")
    fake = install(app)

    fresh = start_nanogpt(client)
    fake.nanogpt_approved = True
    first = client.post("/admin/signin/poll", json={"token": fresh["token"]}).get_json()
    assert first["name"] == "NanoGPT (2)"

    editing = client.post(
        "/admin/signin/start", json={"provider": "nanogpt", "route_id": nano_id}
    ).get_json()
    second = client.post("/admin/signin/poll", json={"token": editing["token"]}).get_json()
    assert second["name"] == "NanoGPT"
    assert "route_id" not in second


def test_openrouter_callback_route_name_dedups(client, app):
    db = app.extensions["midware_db"]
    db.create_route(name="OpenRouter (OAuth)", target_host="https://example.com", upstream_key="x")
    install(app)

    body = client.post("/admin/signin/start", json={"provider": "openrouter"}).get_json()
    state = parse_qs(urlparse(body["authorize_url"]).query)["state"][0]
    page = client.get(
        f"/admin/signin/openrouter/callback?code=abc&state={state}"
    ).get_data(as_text=True)

    assert "OpenRouter (OAuth) (2)" in page


def test_nanogpt_subscription_signin_prefills_the_subscription_host(client, app):
    fake = install(app)

    started = client.post("/admin/signin/start", json={"provider": "nanogpt-sub"})
    body = started.get_json()

    assert body["ok"] is True
    assert body["provider"] == "nanogpt-sub"
    assert body["label"] == "Nano-GPT (Subscription)"
    assert body["name"] == "NanoGPT (Subscription)"
    assert body["host"] == "https://nano-gpt.com/subscription/v1"
    assert body["verify_url"] == START_BODY["verification_uri_complete"]
    # Same handshake as the pay-as-you-go preset: one device login.
    assert "cli-login/start" in fake.calls[0][0]
    assert fake.calls[0][1] == {"client_name": "Midware Request Middleware"}

    fake.nanogpt_approved = True
    approved = client.post("/admin/signin/poll", json={"token": body["token"]}).get_json()
    assert approved["status"] == "approved"
    assert approved["provider"] == "nanogpt-sub"
    assert approved["host"] == "https://nano-gpt.com/subscription/v1"
    assert approved["name"] == "NanoGPT (Subscription)"


def test_route_create_stores_the_provider_tag_and_rejects_bogus_ones(client, app):
    db = app.extensions["midware_db"]

    client.post(
        "/admin/routes",
        data={
            "action": "create",
            "name": "OpenRouter (OAuth)",
            "target_host": "https://openrouter.ai/api/v1",
            "upstream_key": "sk-or-x",
            "provider": "openrouter",
            "test_connection": "0",
        },
    )
    client.post(
        "/admin/routes",
        data={
            "action": "create",
            "name": "Hand-configured",
            "target_host": "https://llm.example.com/v1",
            "upstream_key": "sk-x",
            "provider": "evil-provider",
            "test_connection": "0",
        },
    )

    routes = db.list_routes()
    assert routes[0]["provider"] == "openrouter"
    assert routes[1]["provider"] == ""


def test_route_update_retags_only_after_another_signin(client, app):
    db = app.extensions["midware_db"]
    route_id = db.create_route(
        name="NanoGPT", target_host="https://api.nano-gpt.com/api/v1",
        upstream_key="k", provider="nanogpt",
    )

    client.post(
        "/admin/routes",
        data={
            "action": "update",
            "route_id": route_id,
            "name": "Renamed",
            "target_host": "https://api.nano-gpt.com/api/v1",
            "test_connection": "0",
        },
    )
    assert db.get_route(route_id)["provider"] == "nanogpt"

    client.post(
        "/admin/routes",
        data={
            "action": "update",
            "route_id": route_id,
            "name": "Renamed",
            "target_host": "https://openrouter.ai/api/v1",
            "upstream_key": "sk-or-new",
            "provider": "openrouter",
            "test_connection": "0",
        },
    )
    assert db.get_route(route_id)["provider"] == "openrouter"


def test_routes_page_auth_type_column_shows_how_the_key_arrived(client, app):
    db = app.extensions["midware_db"]
    db.create_route(
        name="OpenRouter (OAuth)", target_host="https://openrouter.ai/api/v1",
        upstream_key="sk-or-x", provider="openrouter",
    )
    db.create_route(
        name="NanoGPT (Subscription)", target_host="https://nano-gpt.com/subscription/v1",
        upstream_key="sk-nano-x", provider="nanogpt-sub",
    )
    db.create_route(
        name="OpenAI", target_host="https://api.openai.com", upstream_key="sk-openai-x"
    )
    db.create_route(name="No key", target_host="https://none.example.com", upstream_key="")

    html = client.get("/admin/routes").get_data(as_text=True)

    assert "<th>Auth Type</th>" in html
    assert "<th>Key</th>" not in html
    assert '<span class="pill blue">OAuth · OpenRouter</span>' in html
    assert '<span class="pill blue">OAuth · Nano-GPT (Subscription)</span>' in html
    assert '<span class="pill ok">API key</span>' in html
    assert '<span class="pill warn">none</span>' in html


def test_setup_route_stores_the_provider_tag(client, app):
    response = client.post(
        "/admin/setup",
        data={
            "name": "OpenRouter (OAuth)",
            "target_host": "https://openrouter.ai/api/v1",
            "upstream_key": "sk-or-x",
            "key_name": "first",
            "provider": "openrouter",
            "test_connection": "0",
        },
    )

    assert response.status_code == 302
    routes = app.extensions["midware_db"].list_routes()
    assert len(routes) == 1
    assert routes[0]["provider"] == "openrouter"
