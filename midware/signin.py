"""Sign-in-with-service flows for the route form.

Three handshakes:

* **Nano-GPT** uses a CLI device login — we request a code, the human approves
  it in their browser, and we poll until an API key comes back. Two route
  presets share it: the pay-as-you-go API and the subscription host.
* **OpenRouter** uses OAuth PKCE — we generate a verifier, send the human to
  OpenRouter, and exchange the code on the redirect back for an API key.
* **OpenCode Go** has no authorized key-handout endpoint (its device login ends
  in a rotating OAuth token, not a key), so it is a paste-and-verify flow: the
  human copies a Console key, we prove it with the quota endpoint, and the
  browser puts it in the route form like the other two.

Every result carries the provider id so the route form can tag the route it
saves; the tag is what later proves which service's balance API to call, and
what makes the proxy add the headers that service requires.

Half-finished flows live in a module-level table keyed by an unguessable
token. That is deliberate: MidWare is a single-process app by design, both
polling providers expire their codes after ten minutes, and nothing here needs
to survive a restart. The acquired key never touches the server — the browser
puts it straight into the route form.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from .balance import parse_go_usage

# Label shown in the UI, route name to pre-fill, and host to pre-fill for each
# provider. ``kind`` picks the handshake (and later the balance API): both
# Nano-GPT presets share one device login, so only their prefills differ. The
# route name for OpenRouter carries the (OAuth) marker so a sign-in-obtained
# route is distinguishable from a hand-configured one.
PROVIDERS: dict[str, dict[str, str]] = {
    "nanogpt": {
        "kind": "nanogpt",
        "label": "Nano-GPT",
        "name": "NanoGPT",
        "host": "https://api.nano-gpt.com/api/v1",
    },
    "nanogpt-sub": {
        "kind": "nanogpt",
        "label": "Nano-GPT (Subscription)",
        "name": "NanoGPT (Subscription)",
        "host": "https://nano-gpt.com/subscription/v1",
    },
    "openrouter": {
        "kind": "openrouter",
        "label": "OpenRouter",
        "name": "OpenRouter (OAuth)",
        "host": "https://openrouter.ai/api/v1",
    },
    "opencode-go": {
        "kind": "opencode-go",
        "label": "OpenCode Go",
        "name": "OpenCode Go",
        "host": "https://opencode.ai/inference/go/openai/v1",
    },
    "opencode-zen": {
        "kind": "opencode-zen",
        "label": "OpenCode Zen",
        "name": "OpenCode Zen",
        "host": "https://opencode.ai/inference/openai/v1",
    },
}

NANOGPT_START_URL = "https://nano-gpt.com/api/cli-login/start"
NANOGPT_POLL_URL = "https://nano-gpt.com/api/cli-login/poll"
OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
# The one call that proves both halves of an OpenCode key: 401 says it is not a
# key, 403 says the workspace it belongs to has no Go subscription. Zen has no
# equivalent meter (every plausible balance path is a 404), so its key is proven
# with the model catalogue, which is public anonymously and 401 to a bad bearer.
OPENCODE_GO_USAGE_URL = "https://opencode.ai/inference/go/v1/usage"

# Both providers expire their codes after ten minutes; prune our side to match.
FLOW_TTL_SECONDS = 600


class SignInError(Exception):
    """A provider refused or could not be reached; the message is user-facing.

    ``status`` is the HTTP status the endpoint answers with: a bad key is the
    caller's mistake (400), an unreachable provider is not (502, the default).
    """

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


_pending: dict[str, dict] = {}


def _store(entry: dict) -> str:
    _prune()
    token = secrets.token_urlsafe(32)
    entry["expires_at"] = time.time() + FLOW_TTL_SECONDS
    _pending[token] = entry
    return token


def _prune() -> None:
    now = time.time()
    for token in [token for token, entry in _pending.items() if entry["expires_at"] <= now]:
        _pending.pop(token, None)


def _lookup(token: str) -> dict | None:
    _prune()
    return _pending.get(token) if token else None


def _post_json(client, url: str, payload: dict, what: str) -> tuple[int, dict]:
    try:
        response = client.post(url, json=payload)
    except Exception as exc:  # a sign-in must never 500 the admin page
        raise SignInError(f"Could not reach {what}: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return int(response.status_code), body


def _get_json(client, url: str, headers: dict[str, str], what: str) -> tuple[int, dict]:
    try:
        response = client.get(url, headers=headers)
    except Exception as exc:  # a sign-in must never 500 the admin page
        raise SignInError(f"Could not reach {what}: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return int(response.status_code), body


def start_nanogpt(
    client, client_name: str, route_id: int | None = None, provider: str = "nanogpt"
) -> dict:
    """Begin a device login; the returned URL is what the human must open."""
    meta = PROVIDERS[provider]
    status, body = _post_json(
        client,
        NANOGPT_START_URL,
        {"client_name": client_name[:64]},
        "Nano-GPT",
    )
    device_code = body.get("device_code")
    verify_url = body.get("verification_uri_complete") or body.get("verification_uri")
    if status >= 400 or not device_code or not verify_url:
        detail = body.get("error") or f"HTTP {status}"
        raise SignInError(f"Nano-GPT login start failed ({detail}).")
    token = _store({"provider": provider, "device_code": str(device_code), "route_id": route_id})
    return {
        "provider": provider,
        "label": meta["label"],
        "name": meta["name"],
        "host": meta["host"],
        "verify_url": str(verify_url),
        "token": token,
        "interval": max(1, int(body.get("interval") or 2)),
        "expires_in": int(body.get("expires_in") or FLOW_TTL_SECONDS),
    }


def poll_nanogpt(client, token: str) -> dict:
    """Ask Nano-GPT whether the code was approved yet.

    Pending is a normal answer, so it never raises; only transport failures do.
    """
    entry = _lookup(token)
    if entry is None or PROVIDERS.get(str(entry.get("provider") or ""), {}).get("kind") != "nanogpt":
        return {"status": "expired", "error": "The sign-in session has ended. Start again."}
    status, body = _post_json(
        client,
        NANOGPT_POLL_URL,
        {"device_code": entry["device_code"]},
        "Nano-GPT",
    )
    state = str(body.get("status") or "").lower()
    if state == "approved":
        _pending.pop(token, None)
        if not body.get("key"):
            raise SignInError("Nano-GPT approved the sign-in but returned no key.")
        meta = PROVIDERS[entry["provider"]]
        return {
            "status": "approved",
            "key": str(body["key"]),
            "provider": entry["provider"],
            "label": meta["label"],
            "name": meta["name"],
            "host": meta["host"],
            "route_id": entry.get("route_id"),
        }
    if state in {"authorization_pending", "pending"} or status == 202:
        return {"status": "pending"}
    _pending.pop(token, None)
    if state == "expired" or status in {404, 410}:
        return {"status": "expired", "error": "The login code expired. Start again."}
    if status == 409:
        return {"status": "expired", "error": "That login code was already used. Start again."}
    detail = body.get("error") or state or f"HTTP {status}"
    return {"status": "expired", "error": f"Nano-GPT login failed ({detail})."}


def begin_openrouter(callback_url: str, route_id: int | None = None) -> dict:
    """Generate a PKCE verifier and the URL that starts OpenRouter's flow."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, inside RFC 7636's 43-128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    token = _store({"provider": "openrouter", "verifier": verifier, "route_id": route_id})
    authorize_url = "{}?{}".format(
        OPENROUTER_AUTH_URL,
        urlencode(
            {
                "callback_url": callback_url,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": token,
                "key_label": "MidWare",
            }
        ),
    )
    meta = PROVIDERS["openrouter"]
    return {
        "provider": "openrouter",
        "label": meta["label"],
        "name": meta["name"],
        "host": meta["host"],
        "authorize_url": authorize_url,
        "token": token,
    }


def finish_openrouter(client, code: str, state: str) -> dict:
    """Exchange the redirected code for a key; the flow is single-use."""
    entry = _lookup(state)
    if entry is None or entry.get("provider") != "openrouter":
        raise SignInError("This sign-in link is unknown or has expired. Start again.")
    _pending.pop(state, None)
    status, body = _post_json(
        client,
        OPENROUTER_KEYS_URL,
        {
            "code": code,
            "code_verifier": entry["verifier"],
            "code_challenge_method": "S256",
        },
        "OpenRouter",
    )
    if status >= 400 or not body.get("key"):
        detail = body.get("error") or body.get("message") or f"HTTP {status}"
        raise SignInError(f"OpenRouter code exchange failed ({detail}).")
    meta = PROVIDERS["openrouter"]
    return {
        "key": str(body["key"]),
        "provider": "openrouter",
        "label": meta["label"],
        "name": meta["name"],
        "host": meta["host"],
        "route_id": entry.get("route_id"),
    }


def connect_key(client, provider: str, key: str, route_id: int | None = None) -> dict:
    """Prove a pasted OpenCode Console key belongs to the gateway it claims.

    One endpoint serves both gateways because the human action is identical and
    the only difference is what proves the key: Go answers its quota endpoint
    (401 not a key, 403 no subscription), Zen has no meter at all, so its model
    catalogue — public anonymously, 401 to a bad bearer — is the check. Neither
    can see a zero balance without spending money, so neither tries: the first
    real request is what reports an exhausted workspace.
    """
    meta = PROVIDERS.get(provider)
    if meta is None or meta["kind"] not in {"opencode-go", "opencode-zen"}:
        raise SignInError("Unknown sign-in provider.", status=400)
    key = (key or "").strip()
    if not key:
        raise SignInError("Paste an OpenCode API key first.", status=400)
    if key.lower().startswith("http"):
        raise SignInError("Paste the key itself, not the Console URL.", status=400)
    if any(character.isspace() for character in key):
        raise SignInError("An API key never contains spaces. Copy it again.", status=400)

    if provider == "opencode-go":
        quota = _verify_opencode_go(client, key)
    else:
        _verify_opencode_zen(client, key)
        quota = None

    return {
        "key": key,
        "provider": provider,
        "label": meta["label"],
        "name": meta["name"],
        "host": meta["host"],
        "route_id": route_id,
        "quota": quota,
    }


def _verify_opencode_go(client, key: str) -> str | None:
    status, body = _get_json(
        client,
        OPENCODE_GO_USAGE_URL,
        {"Authorization": f"Bearer {key}", "Accept": "application/json"},
        "OpenCode",
    )
    if status == 401:
        raise SignInError(
            "OpenCode rejected that key. Create a personal API key in the Console and copy it again.",
            status=400,
        )
    if status == 403:
        raise SignInError(
            "That key works, but its workspace has no OpenCode Go subscription.",
            status=400,
        )
    if status < 200 or status >= 300:
        raise SignInError(f"OpenCode Go key check failed ({_detail(body, status)}).")
    return parse_go_usage(body)


def _verify_opencode_zen(client, key: str) -> None:
    """Zen keys authenticate against the catalogue; there is no balance to read."""
    status, body = _get_json(
        client,
        f"{PROVIDERS['opencode-zen']['host']}/models",
        {"Authorization": f"Bearer {key}", "Accept": "application/json"},
        "OpenCode",
    )
    if status == 401:
        raise SignInError(
            "OpenCode rejected that key. Create a personal API key in the Console and copy it again.",
            status=400,
        )
    if status < 200 or status >= 300:
        raise SignInError(f"OpenCode Zen key check failed ({_detail(body, status)}).")


def _detail(body: dict, status: int) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or f"HTTP {status}")
    return f"HTTP {status}"
