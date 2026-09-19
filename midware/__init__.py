"""MidWare - an asynchronous LLM proxy that tracks usage and centralizes API keys."""

from __future__ import annotations

import os

import httpx
from flask import Flask

from . import config
from .db import Database

__version__ = "0.1.0"

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

    from .jinja import register_template_helpers

    register_template_helpers(app)

    # Kick off a model-catalogue refresh when it has gone stale (roughly daily).
    from .models import refresh_if_stale

    app.extensions["midware_model_refresh"] = lambda: refresh_if_stale(app)
    app.before_request(_maybe_refresh_models)

    return app


def _maybe_refresh_models() -> None:
    from flask import current_app

    if current_app.config.get("TESTING"):
        return
    checker = current_app.extensions.get("midware_model_refresh")
    if checker is not None:
        try:
            checker()
        except Exception:  # never block a request on catalogue bookkeeping
            current_app.logger.debug("model refresh check failed", exc_info=True)
