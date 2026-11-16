from __future__ import annotations

import json

from midware import __version__

from test_proxy import StubResponse, auth_header, install_stub

GO_HOST = "https://opencode.ai/inference/go/openai/v1"
ZEN_HOST = "https://opencode.ai/inference/openai/v1"
BODY = json.dumps({"id": "cmpl-1", "usage": {}}).encode()


def _tagged(app, provider: str = "opencode-go") -> int:
    db = app.extensions["midware_db"]
    route = db.active_route()
    db.update_route(
        route["id"],
        target_host=ZEN_HOST if provider == "opencode-zen" else GO_HOST,
        upstream_key="oc_sk_live_abc",
        provider=provider,
    )
    return route["id"]


def _go_route(app) -> int:
    return _tagged(app)


def _chat(app, token: str, *, model: str = "deepseek-v4.1-flash", headers=None) -> dict:
    client = app.test_client()
    response = client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
        headers={**auth_header(token), **(headers or {})},
    )
    assert response.status_code == 200
    response.get_data()  # drain the streamed/normal body so the request is complete
    return app.extensions["midware_http"].sent[-1][0]["headers"]


def _install(app) -> None:
    install_stub(app, StubResponse(status_code=200, content=BODY))


def _header(headers: dict, name: str) -> str | None:
    """Werkzeug re-cases incoming header names; look them up like the wire does."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def test_go_requests_carry_a_session_id_and_midware_user_agent(configured):
    app, token = configured
    _go_route(app)
    _install(app)

    headers = _chat(app, token, headers={"User-Agent": "python-httpx/0.27.0"})

    session = _header(headers, "x-opencode-session")
    assert session and session.startswith("mw-") and len(session) == 35
    assert _header(headers, "user-agent") == f"MidWare/{__version__}"


def test_session_id_survives_context_edits_and_is_stable_across_turns(configured):
    app, token = configured
    _go_route(app)
    _install(app)

    first = _chat(app, token)
    # A trimmed, summarised or entirely rewritten history is the same session:
    # the id is derived from the key, the route and the model, never messages.
    client = app.test_client()
    client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek-v4.1-flash",
            "messages": [
                {"role": "system", "content": "new summarised context"},
                {"role": "user", "content": "carry on"},
            ],
        },
        headers=auth_header(token),
    ).get_data()
    second = app.extensions["midware_http"].sent[-1][0]["headers"]

    assert _header(first, "x-opencode-session") == _header(second, "x-opencode-session")


def test_session_id_differs_per_model_and_per_key(configured):
    app, token = configured
    _go_route(app)
    _install(app)
    db = app.extensions["midware_db"]
    other = db.create_api_key("someone else")

    base = _header(_chat(app, token), "x-opencode-session")
    other_model = _header(_chat(app, token, model="kimi-k3"), "x-opencode-session")
    other_key = _header(_chat(app, other), "x-opencode-session")

    assert base != other_model
    assert base != other_key
    assert other_model != other_key


def test_a_client_supplied_session_id_is_never_overwritten(configured):
    app, token = configured
    _go_route(app)
    _install(app)

    headers = _chat(app, token, headers={"x-opencode-session": "ses_from_the_harness"})

    assert _header(headers, "x-opencode-session") == "ses_from_the_harness"


def test_a_harness_user_agent_is_left_alone(configured):
    app, token = configured
    _go_route(app)
    _install(app)

    headers = _chat(app, token, headers={"User-Agent": "claude-cli/2.0.1 (external, cli)"})

    assert _header(headers, "user-agent") == "claude-cli/2.0.1 (external, cli)"


def test_zen_routes_get_the_session_header_too(configured):
    app, token = configured
    _tagged(app, "opencode-zen")
    _install(app)

    headers = _chat(app, token)

    session = _header(headers, "x-opencode-session")
    assert session and session.startswith("mw-") and len(session) == 35
    assert _header(headers, "user-agent") == f"MidWare/{__version__}"


def test_other_routes_gain_neither_header(configured):
    app, token = configured
    _install(app)

    headers = _chat(app, token, headers={"User-Agent": "python-httpx/0.27.0"})

    assert _header(headers, "x-opencode-session") is None
    assert _header(headers, "user-agent") == "python-httpx/0.27.0"


def test_go_route_still_forwards_the_route_key_not_the_client_key(configured):
    app, token = configured
    _go_route(app)
    _install(app)

    headers = _chat(app, token)

    assert _header(headers, "Authorization") == "Bearer oc_sk_live_abc"
