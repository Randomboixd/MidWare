"""MidWare - an asynchronous LLM proxy that tracks usage and centralizes API keys."""

from __future__ import annotations

import os

import httpx
from flask import Flask

from . import config
from .db import Database

__version__ = "0.1.3"

__all__ = ["create_app", "__version__"]


def create_app(database_path: str | None = None, config_overrides: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False, static_folder="static")
    app.config.from_object(config)

    if config_overrides:
        app.config.update(config_overrides)

    proxy_hops = int(app.config.get("TRUST_PROXY_HOPS") or 0)
    if proxy_hops > 0:
        # Only trust X-Forwarded-* when the operator says a proxy is in front;
        # otherwise a client could claim "https" and an arbitrary address.
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=proxy_hops,
            x_proto=proxy_hops,
            x_host=proxy_hops,
            x_port=proxy_hops,
        )

    if database_path is None:
        database_path = os.environ.get("MIDWARE_DB") or app.config["DATABASE"]

    db = Database(database_path, timeout=app.config["SQLITE_TIMEOUT"])
    db.init_schema()
    app.extensions["midware_db"] = db

    from .adminauth import ensure_secret_key, ensure_setup_code, register_template_context

    ensure_secret_key(app)
    ensure_setup_code(app)

    app.extensions["midware_http"] = httpx.Client(
        timeout=httpx.Timeout(app.config["UPSTREAM_TIMEOUT"], connect=30.0),
        verify=app.config["UPSTREAM_VERIFY_TLS"],
        follow_redirects=False,
    )

    from .blueprints.account import bp as account_bp
    from .blueprints.admin import bp as admin_bp
    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.msq import bp as msq_bp
    from .blueprints.proxy import bp as proxy_bp
    from .blueprints.sma import api_bp as sma_api_bp
    from .blueprints.sma import consent_bp as sma_consent_bp
    from .blueprints.sma import sign_api_response

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(msq_bp)
    app.register_blueprint(proxy_bp)
    app.register_blueprint(sma_api_bp)
    app.register_blueprint(sma_consent_bp)

    # App-level so it also signs routing errors and 404s that never match the
    # System Management blueprint.
    app.after_request(sign_api_response)

    @app.errorhandler(404)
    @app.errorhandler(405)
    def _sma_http_error(exc):
        from flask import jsonify, request

        if request.path.startswith("/_midware/"):
            return (
                jsonify(
                    {
                        "error": {
                            "message": exc.description or exc.name,
                            "type": "midware_system_account_error",
                            "code": (exc.name or "http_error").lower().replace(" ", "_"),
                        }
                    }
                ),
                exc.code or 500,
            )
        return exc

    from .cors import register_cors
    from .jinja import register_template_helpers

    register_template_helpers(app)
    register_cors(app)
    register_template_context(app)

    @app.after_request
    def _identify(response):
        # Stamped on every response (including passed-through upstream errors) so
        # "is this actually MidWare answering?" is always one `curl -i` away.
        response.headers["Server"] = "MidWare"
        return response

    # Kick off a model-catalogue refresh when it has gone stale (roughly daily),
    # and fetch it immediately on the very first proxied request after install so
    # clients never see an empty /v1/models.
    from .models import refresh_if_stale

    app.extensions["midware_model_refresh"] = lambda: refresh_if_stale(app)
    app.before_request(_maybe_refresh_models)

    from .msq import start_scheduler

    start_scheduler(app)

    return app


def _maybe_refresh_models() -> None:
    from flask import current_app, request

    from .auth import get_db
    from .models import refresh_in_background

    if current_app.config.get("TESTING"):
        return
    if request.path.startswith("/_midware/"):
        # The System Management API is server-to-server chatter; it must never
        # sit behind a model-catalogue fetch (a known way to hang a request).
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
