from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from midware import create_app  # noqa: E402


class _ProbeClient:
    """Upstream stand-in for admin connection tests: every host answers with an
    empty (but valid) model list, so probes pass without touching the network."""

    def get(self, url, headers=None):
        class _Response:
            status_code = 200

            @staticmethod
            def json():
                return {"object": "list", "data": []}

        return _Response()


def _make_app(*, admin_auth: bool):
    application = create_app(
        database_path=":memory:",
        config_overrides={
            "TESTING": True,
            "MAX_CAPTURE_BYTES": 64 * 1024,
            "ADMIN_AUTH_ENABLED": admin_auth,
        },
    )
    application.extensions["midware_http"] = _ProbeClient()
    return application


@pytest.fixture()
def app():
    # The bulk of the suite exercises other behaviour, so the login is off by
    # default; tests/test_admin_auth.py builds an auth-enabled app explicitly.
    return _make_app(admin_auth=False)


@pytest.fixture()
def secured_app():
    return _make_app(admin_auth=True)


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def secured_client(secured_app):
    return secured_app.test_client()


@pytest.fixture()
def db(app):
    return app.extensions["midware_db"]


@pytest.fixture()
def configured(app):
    """A ready-to-use app: one default route, one client key."""
    database = app.extensions["midware_db"]
    database.set_setting("request_log_limit", "10")
    database.create_route(name="Default", target_host="https://api.example.com", upstream_key="sk-upstream")
    token = database.create_api_key("tester")
    database.mark_configured()
    return app, token


@pytest.fixture()
def multi_host(app):
    """Three routes: 'Default' is active, plus 'NanoGPT' and 'Local' on demand."""
    database = app.extensions["midware_db"]
    database.set_setting("request_log_limit", "10")
    default_id = database.create_route(
        name="Default", target_host="https://api.example.com", upstream_key="sk-upstream"
    )
    database.create_route(
        name="NanoGPT",
        target_host="https://nano-gpt.com/subscription/v1",
        upstream_key="sk-nano",
    )
    database.create_route(
        name="Local", target_host="http://127.0.0.1:11434/v1", upstream_key="", is_active=False
    )
    database.update_route(default_id, is_active=True)
    for route in database.list_routes():
        if route["id"] != default_id:
            database.update_route(route["id"], is_active=False)
    token = database.create_api_key("tester")
    database.mark_configured()
    return app, token


@pytest.fixture()
def configured_client(configured):
    app, token = configured
    return app.test_client(), token
