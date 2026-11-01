# MidWare System Management API

A signed, scoped API for automation that needs to *manage* a MidWare server:
create and edit routes, mint API keys, manage MSQ recorders, and read what the
server knows about itself.

It is deliberately harder to call than `/v1/*`. A normal MidWare key is a bearer
token, and this surface can rewrite upstream credentials and mint new keys, so a
stolen token must not be enough. Every request is signed with an Ed25519 key, and
a human approves the grant once in a browser.

This document is the whole contract. If you are writing a client (or you are an
agent implementing one), read "The wire format" and "Agent checklist" first.

- Base URL: `/_midware/api/v1`
- Consent page: `/_midware/authorize/<token>` (browser, admin only)
- Transport: HTTPS by default; plain HTTP only if the admin opts in
- Auth: `Authorization: Bearer sma_live_...` + an Ed25519 signature per request
- Dependencies: `pynacl` for signing, any HTTP client

---

## 1. Before you start

The admin turns the feature on under **Settings → System Management Accounts
API**. Two switches:

| Setting | Default | Meaning |
| --- | --- | --- |
| System Management Accounts API | on | Master switch. Off → `503 sma_disabled` everywhere. |
| Allow over plain HTTP | off | Off → HTTP gets `403 insecure_transport`; HTTPS always works. |

If MidWare runs behind a TLS terminator, the admin sets
`MIDWARE_TRUST_PROXY_HOPS` so the server sees the original scheme.

The admin also needs a control-panel login. The consent page is guarded by it,
and approval is refused while no account exists.

---

## 2. Quick start

The full lifecycle, in order. Step 1 is local; step 2 is unauthenticated; steps
3–6 are signed with the *pending* token; step 7 uses the *live* token.

```
1. generate an Ed25519 keypair                (client side only)
2. POST /EnrollSystemAccount                  -> pending_token, server public key
3. POST /ActivateSystemAccount                -> authorization_url
4. human opens the URL, clicks Allow          (browser, admin session)
5. GET  /ActivateSystemAccount/status         -> "approved"      (poll)
6. POST /ActivateSystemAccount/exchange       -> sma_live_... token  (once only)
7. GET  /interact/self, /interact/routes, ... -> the actual work
```

The return URL (`redirect_uri`) is where the *admin's browser* is offered a link
after approving. It never carries a token: the client polls `status` for that.

### The whole lifecycle in Python

```python
import base64, json, time, secrets, hashlib
import httpx
from nacl.signing import SigningKey

BASE = "https://midware.example.com"
API = "/_midware/api/v1"

sk = SigningKey.generate()
public_key = base64.b64encode(bytes(sk.verify_key)).decode()


def sign(method, path, body=b"", token="", account_id=0, stamp=None, nonce=None):
    stamp = str(int(time.time())) if stamp is None else stamp
    nonce = nonce or secrets.token_urlsafe(16)
    digest = hashlib.sha256(body).hexdigest()
    payload = "\0".join(
        ["MW-SMA-1", str(account_id), method, path, stamp, nonce, digest]
    ).encode()
    return {
        "Authorization": f"Bearer {token}",
        "X-Midware-Account": str(account_id),
        "X-Midware-Timestamp": stamp,
        "X-Midware-Nonce": nonce,
        "X-Midware-Signature": base64.b64encode(sk.sign(payload).signature).decode(),
    }


http = httpx.Client(base_url=BASE, timeout=30)

# 2. enroll
r = http.post(f"{API}/EnrollSystemAccount", json={
    "name": "Docs Bot",
    "public_key": public_key,
    "redirect_uri": "https://client.example.com/sma/callback",
    "scopes": ["midware.read:routes", "midware.read:msq"],
})
r.raise_for_status()
account_id = r.json()["account_id"]
pending = r.json()["pending_token"]

# 3. request consent
r = http.post(
    f"{API}/ActivateSystemAccount",
    headers=sign("POST", f"{API}/ActivateSystemAccount", b"", pending, account_id),
)
print("Open this as the MidWare admin:", r.json()["authorization_url"])

# 5. poll until approved
while True:
    r = http.get(
        f"{API}/ActivateSystemAccount/status",
        headers=sign("GET", f"{API}/ActivateSystemAccount/status", b"", pending, account_id),
    )
    state = r.json().get("state")
    if state in {"approved", "denied", "revoked"}:
        break
    time.sleep(2)
assert state == "approved", state

# 6. exchange (single use)
r = http.post(
    f"{API}/ActivateSystemAccount/exchange",
    headers=sign("POST", f"{API}/ActivateSystemAccount/exchange", b"", pending, account_id),
)
live = r.json()["token"]

# 7. use it
r = http.get(
    f"{API}/interact/routes",
    headers=sign("GET", f"{API}/interact/routes", b"", live, account_id),
)
print(r.json())
```

---

## 3. The wire format

### 3.1 Headers

Every signed request carries all five:

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer <token>` — `sma_pending_...` during activation, `sma_live_...` after |
| `X-Midware-Account` | The numeric account id. Must match the token. |
| `X-Midware-Timestamp` | Unix time in **seconds**, within 300 s of the server clock |
| `X-Midware-Nonce` | 16–128 characters from `A–Z a–z 0–9 _ -`, unique per request |
| `X-Midware-Signature` | Base64 of the 64-byte Ed25519 signature |

POST/PUT requests with a body must send it as JSON with a
`Content-Type: application/json` header. Bodyless POSTs need no content type.

### 3.2 The canonical string

Both sides sign this exact byte sequence, NUL-separated:

```
"MW-SMA-1" \0 account_id \0 METHOD \0 path(+query) \0 timestamp \0 nonce \0 sha256_hex(body)
```

Rules that bite:

- **`METHOD` is upper case**, exactly as sent (`GET`, `POST`).
- **`path` is the full request path**, starting with `/`, including the query
  string if there is one (`/_midware/api/v1/interact/routes?limit=5`). There is
  no API parameter that needs a query string today, but a signature is only valid
  for the exact URL you sign.
- **`body` is the exact bytes on the wire.** Sign what you send, then send those
  same bytes. Do not sign a dict and let the HTTP library re-serialize it
  differently: `json.dumps(obj, separators=(",", ":"))` on one side and
  `json.dumps(obj)` on the other produce different digests. Using
  `httpx`/`requests`'s `json=` keyword is fine *if* you build the body once and
  pass it as `data=`/`content=`. The library in section 7 does that.
- **An empty body is `b""`**, digest
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
- A nonce may be used **once per account, forever**. Reuse is
  `401 replayed_nonce`. Do not reuse a nonce across a retry: generate a fresh one
  and a fresh timestamp for every attempt.

### 3.3 Server responses

Responses are signed in return, so a client can prove MidWare answered:

| Header | Value |
| --- | --- |
| `X-Midware-Server-Signature` | Base64 Ed25519 signature |
| `X-Midware-Server-Key` | Fingerprint of the signing key |

The signed payload uses the client's own request values, which is why they are
echoed back in the headers only once:

```
"MW-SMA-RESP-1" \0 account_id \0 METHOD \0 path(+query) \0 <X-Midware-Timestamp you sent> \0 <your nonce> \0 sha256_hex(response_body)
```

Verify with the server key from `GET /server-key` (or the copy returned by
enroll/exchange). `sma.verify_response_signature` in the MidWare source is the
reference implementation. Responses also carry `Cache-Control: no-store`.

**Errors are signed too**, including `401 invalid_signature`, `403 scope_denied`
and routing 404s — as long as the request carried an `X-Midware-Account` and an
`X-Midware-Nonce` to bind the signature to. A request with no usable nonce
cannot be signed; that is the one case where you get an unsigned error.

### 3.4 Errors

Every failure is JSON:

```json
{"error": {"message": "...", "type": "midware_system_account_error", "code": "..."}}
```

`code` is the stable identifier; branch on it, not on the message. Everything
under `/_midware/` answers JSON, including an unknown path or method.

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 400 | `invalid_body` | Missing/wrong-typed field |
| 400 | `invalid_name` / `reserved_name` | Account name rejected |
| 400 | `invalid_public_key` | Not 32 raw bytes in base64 (PEM is rejected) |
| 400 | `invalid_redirect_uri` | Not https (or loopback http), or has userinfo/fragment |
| 400 | `invalid_scopes` / `unknown_scope` | Empty list, or a scope not in the closed set |
| 400 | `invalid_target_host` | Route host is not a bare http(s) base URL |
| 400 | `apikey_scope_too_broad` | Minted key would be unrestricted |
| 401 | `missing_token` | No bearer token |
| 401 | `invalid_token` | Unknown/retired token, or wrong namespace |
| 401 | `account_mismatch` | `X-Midware-Account` ≠ the token's account |
| 401 | `stale_timestamp` | Clock skew over 300 s |
| 401 | `invalid_nonce` | Nonce malformed |
| 401 | `invalid_signature` | Signature does not verify |
| 401 | `replayed_nonce` | This nonce was already used |
| 403 | `insecure_transport` | Plain HTTP and the opt-in is off |
| 403 | `scope_denied` | Account lacks the endpoint's permission |
| 403 | `wrong_account_state` | e.g. pending account calling `interact/*` |
| 403 | `endpoint_unscoped` | Server-side invariant; shouldn't happen |
| 404 | `route_not_found` / `api_key_not_found` / `msq_not_found` | No such object, or not yours |
| 409 | `name_taken` / `public_key_already_enrolled` | |
| 410 | `activation_expired` | Consent link aged out; call `ActivateSystemAccount` again |
| 413 | `body_too_large` | Over 64 KiB |
| 429 | `enrollment_throttled` / `too_many_pending` | Enrollment limits |
| 429 | `rate_limit_exceeded` | Per-account budget; see `Retry-After` |
| 429 | `probe_throttled` | MSQ run less than 30 s after the last one |
| 503 | `sma_disabled` | Master switch is off |

---

## 4. Endpoints

### 4.1 Public

#### `GET /server-key`

No auth. Returns the server identity and whether the API is reachable from this
transport.

```json
{
  "api_version": "v1",
  "server": {"public_key": "<base64, 32 bytes>", "key_id": "abcd-1234-...", "scheme": "MW-SMA-RESP-1"},
  "enabled": true,
  "transport": "tls"
}
```

The server key is stable across restarts. Pin it on first enrollment if you want
to detect a substituted server later.

#### `POST /EnrollSystemAccount`

No auth. Creates a `pending` account. Rate-limited to 5/hour per source address,
with at most 50 pending accounts server-wide.

```json
{
  "name": "Docs Bot",
  "description": "optional, max 500 chars",
  "public_key": "<base64 of the raw 32-byte Ed25519 public key>",
  "redirect_uri": "https://client.example.com/sma/callback",
  "scopes": ["midware.read:routes", "midware.write:apikeys"]
}
```

- `name`: 1–64 characters, no control characters, no right-to-left overrides,
  must contain a letter or digit. `admin`, `midware`, `system`, `root`,
  `msq measurements` are reserved. Unique among pending/approved/active accounts.
- `public_key`: the raw key, not PEM. One key may only back one live account.
- `redirect_uri`: absolute `https://`, or `http://` on loopback
  (`localhost`, `127.0.0.1`, `::1`, `*.localhost`). No credentials, no fragment.
  Only the host is shown to the admin; no token is ever sent here.
- `scopes`: at least one, all from the table in section 5. Unknown names reject
  the whole request.

`201 Created`:

```json
{
  "account_id": 3,
  "name": "Docs Bot",
  "state": "pending",
  "scopes": ["midware.read:routes", "midware.write:apikeys"],
  "key_fingerprint": "ab12-cd34-ef56-7890",
  "pending_token": "sma_pending_...",
  "expires_at": "2026-11-02T12:00:00+00:00",
  "server": {"public_key": "...", "key_id": "...", "scheme": "MW-SMA-RESP-1"},
  "transport": "tls"
}
```

Store `pending_token` securely. It is only accepted by the three activation
endpoints, and it dies at `expires_at` (24 h) or at exchange.

### 4.2 Activation (signed with the pending token)

#### `POST /ActivateSystemAccount`

No body. Returns a fresh consent link. Calling it again invalidates the previous
link (only the newest works).

```json
{
  "state": "pending",
  "authorization_url": "https://midware.example.com/_midware/authorize/AbC...",
  "expires_at": "2026-11-01T12:15:00+00:00"
}
```

The admin must be signed into the MidWare control panel to open it, and must be
on HTTPS (or the plain-HTTP opt-in must be on). The link is single-use and lives
15 minutes.

#### `GET /ActivateSystemAccount/status`

No body. Allowed in every state, so a client can learn its fate.

```json
{"state": "pending", "account_id": 3}
```

`pending` → still waiting; `approved` → call exchange; `denied` → the admin said
no; `revoked` → the account was retired. Poll no more than every few seconds;
each call costs rate budget.

#### `POST /ActivateSystemAccount/exchange`

No body. Only valid in `approved`, and only once. Rotates the pending token into
a live token and clears the pending/activation hashes.

```json
{
  "account_id": 3,
  "name": "Docs Bot",
  "state": "active",
  "token": "sma_live_...",
  "scopes": ["midware.read:routes"],
  "server": {"public_key": "...", "key_id": "...", "scheme": "MW-SMA-RESP-1"}
}
```

There is no token rotation endpoint. To rotate, the admin revokes the account and
the client enrolls again. If the exchange response is lost, the account is active
with a token you never saw: revoke and re-enroll.

### 4.3 Account self-service (signed with the live token)

#### `GET /interact/self`

No permission required.

```json
{
  "id": 3, "name": "Docs Bot", "description": "",
  "state": "active",
  "scopes": ["midware.read:routes"],
  "redirect_uri": "https://client.example.com/sma/callback",
  "key_fingerprint": "ab12-cd34-ef56-7890",
  "created_at": "2026-11-01T11:00:00+00:00",
  "expires_at": null,
  "activated_at": "2026-11-01T11:05:00+00:00",
  "last_used_at": "2026-11-01T11:09:00+00:00",
  "revoked_at": null, "revoked_reason": null
}
```

#### `POST /DisableSystemAccount`

No permission required. Revokes this account immediately, **deletes every API key
it minted**, and returns `{"state": "revoked", "account_id": 3}`. The next call
with the old token is `401`. There is no undo; re-enroll to come back.

### 4.4 Routes

| Method + path | Scope | Notes |
| --- | --- | --- |
| `GET /interact/routes` | `midware.read:routes` | `{"data": [...]}` |
| `GET /interact/routes/<id>` | `midware.read:routes` | one route |
| `POST /interact/routes/create` | `midware.write:routes` | create |
| `POST /interact/routes/<id>/update` | `midware.edit:routes` | partial update |
| `POST /interact/routes/<id>/delete` | `midware.write:routes` | delete |

A route as returned (`upstream_key` is never included):

```json
{
  "id": 1, "name": "Default", "target_host": "https://api.example.com",
  "api_key_id": null, "is_active": 1, "provider": "",
  "created_at": "2026-10-01T09:00:00+00:00",
  "has_upstream_key": true
}
```

Create body:

```json
{
  "name": "NanoGPT",
  "target_host": "https://nano-gpt.com/subscription/v1",
  "upstream_key": "sk-...",
  "is_active": false,
  "provider": "",
  "api_key_id": null
}
```

`target_host` must be a bare `http(s)` base URL: no query, fragment or userinfo.
`is_active` defaults to `true` on create. Notes:

- `read:routes` tells you whether a credential exists, not what it is.
- `edit:routes` can change `upstream_key`, `target_host`, `name`, `provider`,
  `is_active` and `api_key_id`.
- Deleting a route cascades to its cached model list and nulls `requests.route_id`.

### 4.5 Owned API keys

| Method + path | Scope | Notes |
| --- | --- | --- |
| `GET /interact/apikeys` | `midware.read:apikeysowned` | only keys this account minted |
| `GET /interact/apikeys/<id>` | `midware.read:apikeysowned` | |
| `POST /interact/apikeys/create` | `midware.write:apikeys` | returns the token **once** |
| `POST /interact/apikeys/<id>/update` | `midware.edit:apikeysowned` | |
| `POST /interact/apikeys/<id>/delete` | `midware.edit:apikeysowned` | also removes stored images |

**Keys must be restricted.** A key that can reach every route would bypass every
scope you were granted, so a minted key must carry either:

- `"restrict_providers": true` with a non-empty `allowed_route_ids`, or
- `"restrict_models": true` with a non-empty `allowed_model_ids`.

Anything else is `400 apikey_scope_too_broad`, including widening an existing
owned key during `update`.

Naming route ids (or `"<route_id>:<model_id>"` entries) also requires
`midware.read:routes`, and each id must exist.

```json
{
  "name": "nightly-batch",
  "description": "batch worker",
  "restrict_providers": true,
  "allowed_route_ids": [1],
  "log_requests": true,
  "log_message_contents": false,
  "limit_requests_enabled": true,
  "limit_requests_hour": true,
  "max_requests_hour": 500
}
```

Other accepted fields mirror the admin key form: `cors_allow_origin`,
`allow_premodels`, `restrict_premodels`, `allowed_premodel_ids`,
`restrict_models`, `allowed_model_ids`, `image_response_mode` (`b64`/`url`),
`image_retention_days`, `image_url_https`, `log_requests`,
`log_message_contents`, and the `limit_*`/`max_*` token and request windows.

The create response is the key row plus `"token": "mw-..."`. Every later read
strips `token`, so save it when you create it.

Keys are tagged with the account that made them; other accounts get
`404 api_key_not_found` (deliberately indistinguishable from a missing id).

#### Key on/off switch

| Method + path | Scope | Notes |
| --- | --- | --- |
| `GET /interact/apikeys/status` | `midware.read:apikeystatus` | `{"data": [...]}` — every real key, no tokens |
| `GET /interact/apikeys/<id>/status` | `midware.read:apikeystatus` | `{"id", "name", "enabled", "owner_system_account_id"}` |
| `POST /interact/apikeys/<id>/enable` | `midware.write:apikeystatus` | |
| `POST /interact/apikeys/<id>/disable` | `midware.write:apikeystatus` | |

A disabled key is still a real key: it is recognised and refused with
`403 api_key_disabled`, and re-enabling restores service with the same token.
Nothing is deleted — usage history, limits and allowlists all survive.

These two scopes are **global**, not `...owned`: they act on any API key,
including ones this account did not create. The consent page says so explicitly.
The hidden `MSQ Measurements` system key is never visible or toggleable here
(`404 api_key_not_found`).

### 4.6 MSQ recorders

| Method + path | Scope | Notes |
| --- | --- | --- |
| `GET /interact/msq` | `midware.read:msq` | `{"data": [...]}` |
| `GET /interact/msq/<id>` | `midware.read:msq` | |
| `POST /interact/msq/create` | `midware.write:msq` | |
| `POST /interact/msq/<id>/update` | `midware.edit:msq` | |
| `POST /interact/msq/<id>/delete` | `midware.write:msq` | |
| `POST /interact/msq/<id>/run` | `midware.write:msq` | probe **now**; 30 s cooldown |

```json
{
  "name": "Default quality",
  "route_id": 1,
  "model": "some-model",
  "system_prompt": "",
  "user_prompt": "Hello!",
  "interval_seconds": 3600,
  "enabled": true,
  "penalize_symbols": true,
  "slop_list": "",
  "max_ttft_ms": 30000,
  "max_total_ms": 60000,
  "collect_requests": false,
  "ignore_thinking": true,
  "hardcore_json": {"stream_integrity": true, "text_forensics": false}
}
```

`route_id` must exist. `max_ttft_ms`/`max_total_ms` use `-1` to disable a budget.
`hardcore_json` may be an object (stored canonically) or a JSON string. A probe
spends real upstream tokens, so `run` is throttled and every run is audited.

---

## 5. Permissions

Deny by default. Granularity is the resource, not the object; `write` includes
delete.

| Scope | Grants |
| --- | --- |
| `midware.read:routes` | List/get routes (never the upstream key) |
| `midware.write:routes` | Create and delete routes |
| `midware.edit:routes` | Update route fields |
| `midware.write:apikeys` | Mint keys, restricted to named routes/models |
| `midware.read:apikeysowned` | Read keys this account minted |
| `midware.edit:apikeysowned` | Update/delete keys this account minted |
| `midware.read:apikeystatus` | Read whether any API key is enabled or disabled |
| `midware.write:apikeystatus` | Enable/disable any API key, including ones it did not create |
| `midware.read:msq` | Read recorders |
| `midware.write:msq` | Create/delete recorders, run a probe |
| `midware.edit:msq` | Update a recorder |

Not grantable: `midware.write:self`. Every account can call
`DisableSystemAccount` and read `interact/self` without asking.

`GET /_midware/api/v1/interact/self`, the activation endpoints, `server-key` and
enrollment need no scope.

---

## 6. States, limits and lifetimes

```
pending --(admin Allow)--> approved --(exchange)--> active --(self/admin revoke)--> revoked
   |                           |
   +--(admin Deny)--> denied   +--(link expires)--> still approved; call Activate again
```

An untouched enrollment, or an approved one that is never exchanged, becomes
`revoked` with `revoked_reason: expired` after its 24 h window. A denial stays
`denied` for the audit trail.

| Thing | Value (env override) |
| --- | --- |
| Clock skew allowed | 300 s (`MIDWARE_SMA_NONCE_WINDOW_SECONDS`) |
| Pending account lifetime | 24 h (`MIDWARE_SMA_PENDING_TTL_SECONDS`) |
| Consent link lifetime | 15 min (`MIDWARE_SMA_ACTIVATION_TTL_SECONDS`) |
| Enrollments per address | 5/hour (`MIDWARE_SMA_ENROLL_PER_HOUR`) |
| Pending accounts server-wide | 50 (`MIDWARE_SMA_MAX_PENDING`) |
| Request body cap | 64 KiB (`MIDWARE_SMA_MAX_BODY_BYTES`) |
| Calls per account | 120/min (`MIDWARE_SMA_REQUESTS_PER_MINUTE`) |
| MSQ probe cooldown | 30 s |
| Audit retention | 30 days |

A denied account keeps its pending token so `status` can report `denied`, but
nothing else accepts it. An expired enrollment has its token hashes cleared, so
the client simply sees `401 invalid_token`.

---

## 7. Reference client

Copy this into your project. It wraps the signing rules so callers only deal in
methods and paths. `pip install httpx pynacl`.

```python
"""Minimal MidWare System Management API client."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

SCHEME = "MW-SMA-1"
RESPONSE_SCHEME = "MW-SMA-RESP-1"
API = "/_midware/api/v1"


class SmaError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class SmaClient:
    def __init__(
        self,
        base_url: str,
        signing_key: SigningKey,
        account_id: int,
        token: str,
        *,
        server_public_key: str | None = None,
        verify_responses: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.http = httpx.Client(base_url=base_url, timeout=timeout)
        self.key = signing_key
        self.account_id = account_id
        self.token = token
        self.server_public_key = server_public_key
        self.verify_responses = verify_responses

    # -- plumbing --------------------------------------------------------

    @staticmethod
    def _digest(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def _signature(self, scheme: str, method: str, path: str, stamp: str, nonce: str, body: bytes) -> bytes:
        payload = "\0".join(
            [scheme, str(self.account_id), method, path, stamp, nonce, self._digest(body)]
        ).encode()
        return self.key.sign(payload).signature

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
        stamp = str(int(time.time()))
        nonce = secrets.token_urlsafe(16)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Midware-Account": str(self.account_id),
            "X-Midware-Timestamp": stamp,
            "X-Midware-Nonce": nonce,
            "X-Midware-Signature": base64.b64encode(
                self._signature(SCHEME, method, path, stamp, nonce, raw)
            ).decode(),
        }
        if raw:
            headers["Content-Type"] = "application/json"

        response = self.http.request(method, path, content=raw, headers=headers)

        if self.verify_responses and self.server_public_key:
            signature = response.headers.get("X-Midware-Server-Signature")
            if not signature:
                raise SmaError(0, "unsigned_response", "MidWare did not sign this response")
            payload = "\0".join(
                [RESPONSE_SCHEME, str(self.account_id), method, path, stamp, nonce,
                 self._digest(response.content)]
            ).encode()
            try:
                VerifyKey(base64.b64decode(self.server_public_key)).verify(
                    payload, base64.b64decode(signature)
                )
            except (BadSignatureError, ValueError) as exc:
                raise SmaError(0, "bad_response_signature", "Response signature did not verify") from exc

        if response.status_code >= 400:
            data = response.json() if response.content else {}
            error = data.get("error", {})
            raise SmaError(
                response.status_code,
                error.get("code", "http_error"),
                error.get("message", response.text),
            )
        return response.json()

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def enroll(
        cls, base_url: str, signing_key: SigningKey, name: str, scopes: list[str],
        redirect_uri: str, description: str = "",
    ) -> dict:
        public_key = base64.b64encode(bytes(signing_key.verify_key)).decode()
        with httpx.Client(base_url=base_url, timeout=30) as http:
            response = http.post(f"{API}/EnrollSystemAccount", json={
                "name": name, "description": description, "public_key": public_key,
                "redirect_uri": redirect_uri, "scopes": scopes,
            })
        data = response.json()
        if response.status_code >= 400:
            error = data.get("error", {})
            raise SmaError(response.status_code, error.get("code", "http_error"),
                           error.get("message", response.text))
        return data

    @classmethod
    def activate(cls, base_url: str, signing_key: SigningKey, account_id: int, pending_token: str) -> dict:
        pending = cls(base_url, signing_key, account_id, pending_token)
        return pending.request("POST", f"{API}/ActivateSystemAccount")

    def status(self) -> str:
        return self.request("GET", f"{API}/ActivateSystemAccount/status")["state"]

    def exchange(self) -> dict:
        data = self.request("POST", f"{API}/ActivateSystemAccount/exchange")
        self.token = data["token"]
        return data

    def disable(self) -> dict:
        return self.request("POST", f"{API}/DisableSystemAccount")

    def whoami(self) -> dict:
        return self.request("GET", f"{API}/interact/self")

    # -- routes ----------------------------------------------------------

    def routes(self) -> list[dict]:
        return self.request("GET", f"{API}/interact/routes")["data"]

    def route(self, route_id: int) -> dict:
        return self.request("GET", f"{API}/interact/routes/{route_id}")

    def create_route(self, **fields) -> dict:
        return self.request("POST", f"{API}/interact/routes/create", fields)

    def update_route(self, route_id: int, **fields) -> dict:
        return self.request("POST", f"{API}/interact/routes/{route_id}/update", fields)

    def delete_route(self, route_id: int) -> dict:
        return self.request("POST", f"{API}/interact/routes/{route_id}/delete")

    # -- owned keys ------------------------------------------------------

    def keys(self) -> list[dict]:
        return self.request("GET", f"{API}/interact/apikeys")["data"]

    def key(self, key_id: int) -> dict:
        return self.request("GET", f"{API}/interact/apikeys/{key_id}")

    def create_key(self, **fields) -> dict:
        return self.request("POST", f"{API}/interact/apikeys/create", fields)

    def update_key(self, key_id: int, **fields) -> dict:
        return self.request("POST", f"{API}/interact/apikeys/{key_id}/update", fields)

    def delete_key(self, key_id: int) -> dict:
        return self.request("POST", f"{API}/interact/apikeys/{key_id}/delete")

    # -- MSQ -------------------------------------------------------------

    def recorders(self) -> list[dict]:
        return self.request("GET", f"{API}/interact/msq")["data"]

    def recorder(self, recorder_id: int) -> dict:
        return self.request("GET", f"{API}/interact/msq/{recorder_id}")

    def create_recorder(self, **fields) -> dict:
        return self.request("POST", f"{API}/interact/msq/create", fields)

    def update_recorder(self, recorder_id: int, **fields) -> dict:
        return self.request("POST", f"{API}/interact/msq/{recorder_id}/update", fields)

    def delete_recorder(self, recorder_id: int) -> dict:
        return self.request("POST", f"{API}/interact/msq/{recorder_id}/delete")

    def run_recorder(self, recorder_id: int) -> dict:
        return self.request("POST", f"{API}/interact/msq/{recorder_id}/run")
```

Usage:

```python
key = SigningKey.generate()
enrolled = SmaClient.enroll(
    "https://midware.example.com", key, "Docs Bot",
    ["midware.read:routes", "midware.read:msq"],
    "https://client.example.com/sma/callback",
)
client = SmaClient(
    "https://midware.example.com", key, enrolled["account_id"], enrolled["pending_token"],
    server_public_key=enrolled["server"]["public_key"], verify_responses=True,
)
print(client.activate("https://midware.example.com", key,
                      enrolled["account_id"], enrolled["pending_token"])["authorization_url"])
# ... admin approves ...
client.exchange()
print(client.routes())
```

`activate` is a classmethod only for symmetry; on the instance you can simply
call `client.request("POST", f"{API}/ActivateSystemAccount")` before exchange.

---

## 8. Agent checklist

If you are an LLM agent driving this API, these are the mistakes that actually
happen:

1. **Sign exactly what you send.** Build `body` bytes once, sign their digest,
   then send those same bytes. Never sign a dict and send a re-serialized copy.
2. **One nonce per request, always.** Fresh `secrets.token_urlsafe(16)` per call,
   including retries. A reused nonce is a permanent `401 replayed_nonce`.
3. **Fresh timestamp per request.** `int(time.time())`. A cached header set goes
   stale after 300 s.
4. **The path in the signature is the path you request**, including any query
   string. `/interact/routes` ≠ `/interact/routes/`.
5. **`X-Midware-Account` must equal the account the token belongs to.** Don't
   copy it from another session.
6. **Tokens are namespaced.** `sma_pending_` only works for activation;
   `sma_live_` only for `interact/*` and self-disable. An `mw-` key is a
   different surface entirely.
7. **Never print or log the private key.** The public key is fine; the seed is
   not. Store it outside the repo.
8. **`exchange` once.** If you lose the response, revoke and re-enroll; do not
   loop on exchange (it will be `403 wrong_account_state`).
9. **Handle 401 by code.** `invalid_signature` means bad signing; `stale_timestamp`
   means clock; `replayed_nonce` means reuse; `invalid_token` means revoked/expired.
10. **Respect 429.** Read `Retry-After`; polling `status` more often than every
    couple of seconds just burns the per-account budget.
11. **A minted key must be restricted.** Always pass `restrict_providers` with
    route ids, or `restrict_models` with `"<route_id>:<model>"` ids, and hold
    `midware.read:routes` to name them.
12. **Don't assume `upstream_key` is readable.** It never is. Use
    `has_upstream_key` to know whether one exists.
13. **The consent step needs a human.** You cannot approve your own enrollment
    unless you also hold the MidWare admin session; if you do, open the
    `authorization_url`, read the CSRF field from the form, `POST` it back, and
    stop impersonating a browser afterwards.
14. **HTTPS by default.** If you get `insecure_transport`, switch the base URL to
    `https://`; don't ask for the opt-in.

---

## 9. Troubleshooting

| Symptom | Cause |
| --- | --- |
| `503 sma_disabled` | Master switch is off in Settings. |
| `403 insecure_transport` | Plain HTTP and the opt-in is off (or a proxy isn't trusted: set `MIDWARE_TRUST_PROXY_HOPS`). |
| `401 invalid_signature` on every call | Body bytes changed after signing, or the method/path/timestamp differ. |
| `401 stale_timestamp` | Host clock drift over 300 s. |
| `401 replayed_nonce` | A nonce was reused (often a retried request). |
| `403 wrong_account_state` | Pending account calling `interact/*`, or a second `exchange`. |
| `403 scope_denied` | The grant does not include that endpoint; re-enroll with the scope. |
| `410 activation_expired` | Consent link older than 15 minutes; call `ActivateSystemAccount` again. |
| `404 api_key_not_found` | The key belongs to another account (or was deleted). |
| `400 apikey_scope_too_broad` | The key would be unrestricted; name routes or models. |

---

## 10. Versioning

`MW-SMA-1` and `MW-SMA-RESP-1` are the signed byte layouts. The scheme is fixed
by the API version you call, never negotiated from a response header. `/v1` in the
URL is the HTTP API version. Both are additive-stable: new fields may appear in
responses, new scopes may be added (never silently granted), and unknown scopes
are rejected at enrollment.
