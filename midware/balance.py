"""Account-balance lookups for routes tagged by the sign-in flow.

Only routes carrying a ``routes.provider`` tag are queried: the sign-in is
what proves the upstream really is that service, and the balance call doubles
as a live check that the stored key still belongs to it. Everything is
best-effort — a provider that rejects the call (OpenRouter reserves its
credits endpoint for management keys, NanoGPT may be down) simply yields no
line under the route name.

A subscription-tagged route shows its token quota first (the usage endpoint
answers with the same inference key the sign-in produced) and falls back to
the cash balance when no quota is configured for the key.

Results are cached briefly so reloading the routes page does not hammer the
providers; the cache key includes a digest of the upstream key, so rotating
the key fetches afresh.
"""

from __future__ import annotations

import hashlib
import time

NANOGPT_CHECK_URL = "https://api.nano-gpt.com/api/check-balance"
NANOGPT_SUB_USAGE_URL = "https://api.nano-gpt.com/api/subscription/v1/usage"
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

BALANCE_TTL_SECONDS = 60

# (provider, key digest) -> (expires_at, formatted text)
_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _request(client, method: str, url: str, **kwargs) -> tuple[int, dict]:
    try:
        response = getattr(client, method)(url, **kwargs)
    except Exception:  # a balance lookup must never 500 the admin page
        return 0, {}
    try:
        body = response.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return int(getattr(response, "status_code", 0) or 0), body


def _money(amount: float, suffix: str = "left") -> str:
    return f"${amount:,.2f} {suffix}"


def _nanogpt_balance(client, key: str) -> str | None:
    status, body = _request(
        client,
        "post",
        NANOGPT_CHECK_URL,
        headers={"x-api-key": key, "Authorization": f"Bearer {key}"},
    )
    if status < 200 or status >= 300:
        return None
    try:
        return _money(float(body.get("usd_balance")))
    except (TypeError, ValueError):
        return None


def _quota_line(quota, window: str) -> str | None:
    """One quota counter as a remaining-tokens line, or None if unusable."""
    if not isinstance(quota, dict):
        return None
    remaining = quota.get("remaining")
    if remaining is None:
        return None
    try:
        count = max(0, int(float(remaining)))
    except (TypeError, ValueError):
        return None
    return f"{count:,} tokens left {'today' if window == 'day' else 'this week'}"


def _nanogpt_sub_balance(client, key: str) -> str | None:
    """Subscription token quota first, cash balance when no quota applies.

    A null quota means "not configured for this key" (or degraded lookup), and
    a subscription key may still hold spendable cash, so the cash line is the
    fallback rather than a blank.
    """
    headers = {"x-api-key": key, "Authorization": f"Bearer {key}"}
    status, body = _request(client, "get", NANOGPT_SUB_USAGE_URL, headers=headers)
    if 200 <= status < 300:
        for quota, window in (
            (body.get("dailyInputTokens"), "day"),
            (body.get("weeklyInputTokens"), "week"),
        ):
            line = _quota_line(quota, window)
            if line:
                return line
    return _nanogpt_balance(client, key)


def _openrouter_balance(client, key: str) -> str | None:
    headers = {"Authorization": f"Bearer {key}"}
    status, body = _request(client, "get", OPENROUTER_CREDITS_URL, headers=headers)
    if 200 <= status < 300:
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        try:
            remaining = float(data["total_credits"]) - float(data["total_usage"])
        except (KeyError, TypeError, ValueError):
            return None
        return _money(remaining)
    # The credits endpoint wants a management key, so fall back to the per-key
    # view: it always answers and tells us how much of a spending cap is left.
    status, body = _request(client, "get", OPENROUTER_KEY_URL, headers=headers)
    if 200 <= status < 300:
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        remaining = data.get("limit_remaining")
        if remaining is None:
            return None
        try:
            return _money(float(remaining), "left on this key")
        except (TypeError, ValueError):
            return None
    return None


def fetch_balance(client, provider: str, key: str) -> str | None:
    """Return a formatted "money left" line, or None when nothing can be shown."""
    if provider not in {"nanogpt", "nanogpt-sub", "openrouter"} or not key:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    cache_key = (provider, digest)
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    if provider == "openrouter":
        text = _openrouter_balance(client, key)
    elif provider == "nanogpt-sub":
        text = _nanogpt_sub_balance(client, key)
    else:
        text = _nanogpt_balance(client, key)
    if text is not None:
        _prune(now)
        _cache[cache_key] = (now + BALANCE_TTL_SECONDS, text)
    return text


def _prune(now: float) -> None:
    for cache_key in [cache_key for cache_key, (until, _) in _cache.items() if until <= now]:
        _cache.pop(cache_key, None)
