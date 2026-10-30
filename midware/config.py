"""Application configuration defaults.

Every value can be overridden with an environment variable of the same name.
"""

from __future__ import annotations

import os
from datetime import timedelta
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

# Where keys with image_response_mode="url" store generated images. Defaults
# next to the database file so the Docker /data volume holds both.
IMAGES_DIR = os.environ.get("MIDWARE_IMAGES_DIR") or str(Path(DATABASE).parent / "images")

# Signing key for the login session cookie. An empty value means "generate one
# once and keep it in the database", so a fresh install does not ship with a
# publicly known key. Set MIDWARE_SECRET_KEY to override (Docker Compose does).
SECRET_KEY = os.environ.get("MIDWARE_SECRET_KEY", "")

# Control-panel login. The password set here (if any) overrides the one stored in
# the database, which makes it both a headless/Docker switch and a lockout
# recovery path. Leave it unset to manage credentials from the setup page.
ADMIN_USERNAME = os.environ.get("MIDWARE_ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("MIDWARE_ADMIN_PASSWORD", "")

# Enforced for every human-facing page. Only the test fixtures turn it off.
ADMIN_AUTH_ENABLED = True

# Login sessions are signed cookies that survive closing the browser. Flask
# refreshes the expiry on every visit, so this is an idle timeout, not a fixed
# account age.
ADMIN_SESSION_DAYS = max(1, min(_env_int("MIDWARE_ADMIN_SESSION_DAYS", 30), 365))
PERMANENT_SESSION_LIFETIME = timedelta(days=ADMIN_SESSION_DAYS)
SESSION_COOKIE_NAME = "midware_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Self-hosted deployments often run on plain HTTP or behind a TLS terminator
# that Flask cannot see; opt in explicitly when the browser sees HTTPS.
SESSION_COOKIE_SECURE = _env_bool("MIDWARE_SESSION_COOKIE_SECURE", False)
SESSION_REFRESH_EACH_REQUEST = True

# Best-effort in-memory brake on password guessing. Zero attempts disables it.
ADMIN_LOGIN_MAX_ATTEMPTS = _env_int("MIDWARE_ADMIN_LOGIN_MAX_ATTEMPTS", 5)
ADMIN_LOGIN_WINDOW_SECONDS = _env_int("MIDWARE_ADMIN_LOGIN_WINDOW_SECONDS", 300)

# How long we wait on the upstream provider, in seconds.
UPSTREAM_TIMEOUT = float(os.environ.get("MIDWARE_UPSTREAM_TIMEOUT", "300"))

# Verify TLS certificates of the upstream provider.
UPSTREAM_VERIFY_TLS = _env_bool("MIDWARE_UPSTREAM_VERIFY_TLS", True)

# Fallback saved-request cap. The live value lives in the settings table and is
# editable from the control panel.
DEFAULT_REQUEST_LOG_LIMIT = _env_int("MIDWARE_REQUEST_LOG_LIMIT", 10)

# Fallback premodel addressing prefix ("<p>-slug"). The live value lives in the
# settings table and is editable from the control panel.
DEFAULT_PREMODEL_PREFIX = os.environ.get("MIDWARE_PREMODEL_PREFIX", "<p>-")

# Default upstream host used to pre-fill the setup form.
DEFAULT_TARGET_HOST = os.environ.get("MIDWARE_TARGET_HOST", "https://api.openai.com")

# Name Nano-GPT shows next to keys minted by our device-login flow.
NANOGPT_CLIENT_NAME = os.environ.get("MIDWARE_NANOGPT_CLIENT_NAME", "Midware Request Middleware")

# SQLite only ever accepts one writer; keep the wait bounded rather than infinite.
SQLITE_TIMEOUT = float(os.environ.get("MIDWARE_SQLITE_TIMEOUT", "5"))

# Hard cap on how much of an upstream response we buffer for parsing, in bytes.
MAX_CAPTURE_BYTES = _env_int("MIDWARE_MAX_CAPTURE_BYTES", 8 * 1024 * 1024)

# -- Model Service Quality (MSQ) --------------------------------------------
# MSQ periodically asks a model a fixed question and scores how well it answers
# (latency, stray symbols, slop words). The scheduler is an in-process daemon
# thread, consistent with the model-refresh thread; there is no external worker.

# Master switch. When off, no checks run and the scheduler is never started.
MSQ_ENABLED = _env_bool("MIDWARE_MSQ_ENABLED", True)

# How often the scheduler looks for recorders whose next check is due, in seconds.
MSQ_SCHEDULER_INTERVAL = _env_int("MIDWARE_MSQ_SCHEDULER_INTERVAL", 60)

# Default per-recorder check frequency, in seconds (one hour).
MSQ_DEFAULT_INTERVAL = _env_int("MIDWARE_MSQ_DEFAULT_INTERVAL", 3600)

# Default unpenalized budgets, in milliseconds (``-1`` disables either).
MSQ_DEFAULT_MAX_TTFT_MS = _env_int("MIDWARE_MSQ_DEFAULT_MAX_TTFT_MS", 30_000)
MSQ_DEFAULT_MAX_TOTAL_MS = _env_int("MIDWARE_MSQ_DEFAULT_MAX_TOTAL_MS", 60_000)

# A recorder needs this much history before it is called Working/Degraded.
MSQ_EXAMINE_HOURS = _env_int("MIDWARE_MSQ_EXAMINE_HOURS", 24)
MSQ_MIN_CHECKS = _env_int("MIDWARE_MSQ_MIN_CHECKS", 3)

# Average score below this, over the examination window, means "Degraded".
MSQ_DEGRADED_THRESHOLD = float(os.environ.get("MIDWARE_MSQ_DEGRADED_THRESHOLD", "70"))

# How many recent checks each recorder keeps for evaluation (the chart reads
# from the same window but only draws MSQ_CHART_LIMIT of them).
MSQ_HISTORY_LIMIT = _env_int("MIDWARE_MSQ_HISTORY_LIMIT", 168)

# How many of those checks each recorder's chart actually draws (newest N).
MSQ_CHART_LIMIT = _env_int("MIDWARE_MSQ_CHART_LIMIT", 10)

# Hardcore checks (opt-in per recorder). A gap between streamed chunks longer
# than this counts as a stall; the needle probe hides a code in this many
# characters of filler to exercise long-context recall.
MSQ_STALL_MS = _env_int("MIDWARE_MSQ_STALL_MS", 5_000)
MSQ_NEEDLE_CONTEXT_CHARS = _env_int("MIDWARE_MSQ_NEEDLE_CONTEXT_CHARS", 4_000)
