"""MidWare - an asynchronous LLM proxy that tracks usage and centralizes API keys."""

from __future__ import annotations

import os

import httpx
from flask import Flask

from . import config
from .db import Database

__version__ = "0.1.1"

__all__ = ["create_app", "__version__"]


def create_app(database_path: str | None = None, config_overrides: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False, static_folder="static")
    app.config.from_object(config)

    if config_overrides:
        app.config.update(config_overrides)

    if database_path is None:
        database_path = os.environ.get("MIDWARE_DB") or app.config["DATABASE"]

    db = Database(database_path, timeout=app.config["SQLITE_TIMEOUT"])
    db.init_schema()
    app.extensions["midware_db"] = db

    from .adminauth import ensure_setup_code

    ensure_setup_code(app)

    app.extensions["midware_http"] = httpx.Client(
        timeout=httpx.Timeout(app.config["UPSTREAM_TIMEOUT"], connect=30.0),
        verify=app.config["UPSTREAM_VERIFY_TLS"],
        follow_redirects=False,
    )

    from .blueprints.admin import bp as admin_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.proxy import bp as proxy_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(proxy_bp)

    from .cors import register_cors
    from .jinja import register_template_helpers

    register_template_helpers(app)
    register_cors(app)

    # Kick off a model-catalogue refresh when it has gone stale (roughly daily),
    # and fetch it immediately on the very first proxied request after install so
    # clients never see an empty /v1/models.
    from .models import refresh_if_stale

    app.extensions["midware_model_refresh"] = lambda: refresh_if_stale(app)
    app.before_request(_maybe_refresh_models)

    return app


def _maybe_refresh_models() -> None:
    from flask import current_app

    from .auth import get_db
    from .models import refresh_in_background

    if current_app.config.get("TESTING"):
        return
    checker = current_app.extensions.get("midware_model_refresh")
    if checker is not None:
        try:
            checker()
        except Exception:  # never block a request on catalogue bookkeeping
            current_app.logger.debug("model refresh check failed", exc_info=True)
        try:
            db = get_db()
            if db.is_configured() and not db.get_meta("models_refreshed_at") and db.list_routes():
                refresh_in_background(current_app._get_current_object())
        except Exception:
            current_app.logger.debug("initial model refresh failed", exc_info=True)
