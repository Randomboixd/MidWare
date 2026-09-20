"""Application configuration defaults.

Every value can be overridden with an environment variable of the same name.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# SQLite database file. Use ":memory:" for an ephemeral database.
DATABASE = os.environ.get("MIDWARE_DB") or str(BASE_DIR / "midware.db")

SECRET_KEY = os.environ.get("MIDWARE_SECRET_KEY", "midware-dev-secret-change-me")

# How long we wait on the upstream provider, in seconds.
UPSTREAM_TIMEOUT = float(os.environ.get("MIDWARE_UPSTREAM_TIMEOUT", "300"))

# Verify TLS certificates of the upstream provider.
UPSTREAM_VERIFY_TLS = _env_bool("MIDWARE_UPSTREAM_VERIFY_TLS", True)

# Fallback saved-request cap. The live value lives in the settings table and is
# editable from the control panel.
DEFAULT_REQUEST_LOG_LIMIT = _env_int("MIDWARE_REQUEST_LOG_LIMIT", 10)

# Default upstream host used to pre-fill the setup form.
DEFAULT_TARGET_HOST = os.environ.get("MIDWARE_TARGET_HOST", "https://api.openai.com")

# SQLite only ever accepts one writer; keep the wait bounded rather than infinite.
SQLITE_TIMEOUT = float(os.environ.get("MIDWARE_SQLITE_TIMEOUT", "5"))

# Hard cap on how much of an upstream response we buffer for parsing, in bytes.
MAX_CAPTURE_BYTES = _env_int("MIDWARE_MAX_CAPTURE_BYTES", 8 * 1024 * 1024)

# CORS allow-origin default. The live value lives in the settings table and is
# editable from the control panel. "*" allows every origin.
CORS_ALLOW_ORIGIN = os.environ.get("MIDWARE_CORS_ALLOW_ORIGIN", "*")
