from __future__ import annotations

from test_admin_auth import basic, seed
from test_signin import install


def _tagged_route(app, provider: str, key: str) -> int:
    db = app.extensions["midware_db"]
    return db.create_route(
        name=provider,
        target_host="https://upstream.example.com",
        upstream_key=key,
        provider=provider,
    )


def _get_balance(client, route_id: int) -> dict:
    response = client.get(f"/admin/routes/{route_id}/balance")
    assert response.status_code == 200
    return response.get_json()


def test_openrouter_balance_shows_remaining_credits(client, app):
    fake = install(app)
    route_id = _tagged_route(app, "openrouter", "sk-or-credits-1")

    body = _get_balance(client, route_id)

    assert body == {"ok": True, "text": "$74.75 left"}
    assert any("/api/v1/credits" in url for url, _ in fake.calls)
    assert any(
        "/api/v1/credits" in url
        and (headers or {}).get("Authorization") == "Bearer sk-or-credits-1"
        for url, headers in fake.header_log
    )


def test_openrouter_balance_falls_back_to_the_key_view(client, app):
    fake = install(app)
    fake.openrouter_credits = None  # management-key-only endpoint
    fake.openrouter_key = {"data": {"limit_remaining": 12.5}}
    route_id = _tagged_route(app, "openrouter", "sk-or-keycap-1")

    body = _get_balance(client, route_id)

    assert body == {"ok": True, "text": "$12.50 left on this key"}
    assert any("/api/v1/key" in url for url, _ in fake.calls)


def test_openrouter_balance_is_silent_when_nothing_can_be_shown(client, app):
    fake = install(app)
    fake.openrouter_credits = None
    fake.openrouter_key = None
    route_id = _tagged_route(app, "openrouter", "sk-or-dead-1")

    assert _get_balance(client, route_id) == {"ok": False}


def test_nanogpt_balance_shows_usd(client, app):
    fake = install(app)
    route_id = _tagged_route(app, "nanogpt", "sk-nano-bal-1")

    assert _get_balance(client, route_id) == {"ok": True, "text": "$129.47 left"}
    assert any(
        "check-balance" in url and (headers or {}).get("x-api-key") == "sk-nano-bal-1"
        for url, headers in fake.header_log
    )


def test_subscription_route_shows_remaining_daily_quota(client, app):
    fake = install(app)
    route_id = _tagged_route(app, "nanogpt-sub", "sk-nano-sub-1")

    body = _get_balance(client, route_id)

    assert body == {"ok": True, "text": "412,300 tokens left today"}
    assert any(
        "subscription" in url and (headers or {}).get("x-api-key") == "sk-nano-sub-1"
        for url, headers in fake.header_log
    )
    # The quota answered, so the cash endpoint is not consulted.
    assert not any("check-balance" in url for url, _ in fake.calls)


def test_subscription_route_falls_back_to_the_weekly_quota(client, app):
    fake = install(app)
    fake.nanogpt_sub_usage = {
        "active": True,
        "state": "active",
        "dailyInputTokens": None,  # not configured for this key
        "weeklyInputTokens": {
            "used": 1200000, "remaining": 2300000, "percentUsed": 0.3428,
            "resetAt": 1760300000000, "degraded": False,
        },
        "period": {"currentPeriodEnd": None},
    }
    route_id = _tagged_route(app, "nanogpt-sub", "sk-nano-sub-2")

    assert _get_balance(client, route_id) == {"ok": True, "text": "2,300,000 tokens left this week"}


def test_subscription_route_falls_back_to_cash_when_quota_is_unavailable(client, app):
    fake = install(app)
    fake.nanogpt_sub_usage = None  # e.g. key without an active subscription
    route_id = _tagged_route(app, "nanogpt-sub", "sk-nano-sub-3")

    assert _get_balance(client, route_id) == {"ok": True, "text": "$129.47 left"}
    assert any("check-balance" in url for url, _ in fake.calls)


def test_untagged_routes_are_never_queried(client, app):
    fake = install(app)
    db = app.extensions["midware_db"]
    plain = db.create_route(
        name="Plain", target_host="https://p.example.com", upstream_key="sk-plain"
    )
    bogus = db.create_route(
        name="Bogus", target_host="https://b.example.com", upstream_key="sk-bogus",
        provider="not-a-provider",
    )

    assert _get_balance(client, plain) == {"ok": False}
    assert _get_balance(client, bogus) == {"ok": False}
    assert fake.calls == []


def test_failed_balance_lookup_never_500s(client, app):
    fake = install(app)
    fake.nanogpt_balance = None  # provider rejects the stored key
    route_id = _tagged_route(app, "nanogpt", "sk-nano-bad-1")

    assert _get_balance(client, route_id) == {"ok": False}


def test_balance_transport_error_is_swallowed(client, app):
    fake = install(app)
    fake.raise_on_balance = True
    route_id = _tagged_route(app, "openrouter", "sk-or-offline-1")

    assert _get_balance(client, route_id) == {"ok": False}


def test_routes_page_links_only_tagged_routes(client, app):
    install(app)
    db = app.extensions["midware_db"]
    tagged = _tagged_route(app, "openrouter", "sk-or-page-1")
    plain = db.create_route(
        name="Plain", target_host="https://p.example.com", upstream_key="sk-plain"
    )

    html = client.get("/admin/routes").get_data(as_text=True)

    assert html.count("data-balance-url") == 1
    assert f"/admin/routes/{tagged}/balance" in html
    assert f"/admin/routes/{plain}/balance" not in html


def test_balance_requires_login_once_claimed(secured_app):
    seed(secured_app)
    install(secured_app)
    db = secured_app.extensions["midware_db"]
    route_id = db.create_route(
        name="OpenRouter (OAuth)", target_host="https://openrouter.ai/api/v1",
        upstream_key="sk-or-guard-1", provider="openrouter",
    )
    client = secured_app.test_client()

    assert client.get(f"/admin/routes/{route_id}/balance").status_code == 401
    opened = client.get(
        f"/admin/routes/{route_id}/balance", headers=basic("admin", "s3cret")
    )
    assert opened.status_code == 200
