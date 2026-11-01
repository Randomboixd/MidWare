"""System Management Accounts: signed, scoped API access for automation.

A normal MidWare key is a bearer token: whoever holds it can spend the route's
upstream credential. The System Management API can rewrite routes and mint new
keys, so a bearer token alone is too weak a proof. An SMA account therefore
enrolls an Ed25519 public key and must sign every request with the matching
private key, which never reaches the server.

A granted account still has to be approved by the admin in a browser, and that
approval is the security boundary: the consent page renders exactly what was
stored at enrollment, and activation is bound to a hash of that payload so the
request cannot be rewritten between enrollment and approval.

Everything the wire protocol depends on lives here: the canonical signing
payload, scope names, key/token encoding, and the redaction helpers that keep
upstream credentials out of ``read:routes`` responses.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from .db import Database, to_iso, utcnow

# Bumped only when the signed byte layout changes; both sides must agree.
SCHEME = "MW-SMA-1"
RESPONSE_SCHEME = "MW-SMA-RESP-1"

TOKEN_LIVE_PREFIX = "sma_live_"
TOKEN_PENDING_PREFIX = "sma_pending_"

# -- permissions -------------------------------------------------------------

READ_ROUTES = "midware.read:routes"
WRITE_ROUTES = "midware.write:routes"
EDIT_ROUTES = "midware.edit:routes"
WRITE_APIKEYS = "midware.write:apikeys"
READ_APIKEYS_OWNED = "midware.read:apikeysowned"
EDIT_APIKEYS_OWNED = "midware.edit:apikeysowned"
READ_APIKEYS_STATUS = "midware.read:apikeystatus"
WRITE_APIKEYS_STATUS = "midware.write:apikeystatus"
READ_MSQ = "midware.read:msq"
WRITE_MSQ = "midware.write:msq"
EDIT_MSQ = "midware.edit:msq"
WRITE_SELF = "midware.write:self"

SCOPES: tuple[str, ...] = (
    READ_ROUTES,
    WRITE_ROUTES,
    EDIT_ROUTES,
    WRITE_APIKEYS,
    READ_APIKEYS_OWNED,
    EDIT_APIKEYS_OWNED,
    READ_APIKEYS_STATUS,
    WRITE_APIKEYS_STATUS,
    READ_MSQ,
    WRITE_MSQ,
    EDIT_MSQ,
)

# Not grantable: every account can shut itself off, and asking for it would only
# create a scope that can be withheld.
IMPLICIT_SCOPES = frozenset({WRITE_SELF})

SCOPE_DESCRIPTIONS: dict[str, str] = {
    READ_ROUTES: "List and read model provider routes. Upstream keys are never included.",
    WRITE_ROUTES: "Add new routes and delete existing ones.",
    EDIT_ROUTES: "Change a route's name, host, upstream key or active state.",
    WRITE_APIKEYS: "Create MidWare API keys, restricted to routes or models it names.",
    READ_APIKEYS_OWNED: "Read API keys it created. Tokens are never shown again.",
    EDIT_APIKEYS_OWNED: "Update and delete API keys it created.",
    READ_APIKEYS_STATUS: "Read whether any API key is enabled or disabled.",
    WRITE_APIKEYS_STATUS: (
        "Enable or disable any API key, including keys this account did not create."
    ),
    READ_MSQ: "Read Model Service Quality recorders and their results.",
    WRITE_MSQ: "Add and delete MSQ recorders, and run checks now.",
    EDIT_MSQ: "Change an MSQ recorder's prompt, model, schedule or scoring.",
}

# Names that would make the consent page lie about who is asking.
RESERVED_NAMES = frozenset(
    {
        "admin",
        "administrator",
        "midware",
        "midware admin",
        "msq measurements",
        "root",
        "system",
    }
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_BIDI_CHARS = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")

MAX_NAME_LENGTH = 64
MAX_REDIRECT_LENGTH = 2048
MAX_PUBLIC_KEY_LENGTH = 256


class SmaError(Exception):
    """A request the SMA surface refuses, carrying its OpenAI-shaped payload."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def payload(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": "midware_system_account_error",
                "code": self.code,
            }
        }


# -- tokens and keys ---------------------------------------------------------


def hash_token(token: str) -> str:
    """Store only a digest; the plaintext token is handed out exactly once."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def new_token(prefix: str = TOKEN_LIVE_PREFIX) -> str:
    return prefix + secrets.token_urlsafe(32)


def token_kind(token: str) -> str | None:
    """Classify a bearer token by its prefix, before touching the database."""
    if token.startswith(TOKEN_LIVE_PREFIX):
        return "live"
    if token.startswith(TOKEN_PENDING_PREFIX):
        return "pending"
    return None


def encode_public_key(key: VerifyKey) -> str:
    return base64.b64encode(bytes(key)).decode("ascii")


def decode_public_key(text: str) -> VerifyKey:
    """Parse a client-supplied Ed25519 key, refusing anything malformed."""
    if not isinstance(text, str) or not text or len(text) > MAX_PUBLIC_KEY_LENGTH:
        raise SmaError("invalid_public_key", "Provide a base64 Ed25519 public key.")
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SmaError("invalid_public_key", "The public key is not valid base64.") from exc
    if len(raw) != 32:
        raise SmaError(
            "invalid_public_key",
            "An Ed25519 public key is exactly 32 bytes; send the raw key, not a PEM block.",
        )
    try:
        return VerifyKey(raw)
    except ValueError as exc:
        raise SmaError("invalid_public_key", "The public key is not a usable Ed25519 key.") from exc


def key_fingerprint(public_key: str) -> str:
    digest = hashlib.sha256(public_key.encode("utf-8")).hexdigest()[:16]
    return "-".join(digest[index : index + 4] for index in range(0, 16, 4))


# -- server identity ---------------------------------------------------------


def _load_signing_key(db: Database) -> SigningKey:
    stored = db.get_meta("sma_server_signing_key")
    if stored:
        try:
            return SigningKey(base64.b64decode(stored))
        except (binascii.Error, ValueError) as exc:
            raise SmaError(
                "server_key_corrupt",
                "The server signing key in the database is unreadable.",
                500,
            ) from exc
    seed = base64.b64encode(bytes(SigningKey.generate())).decode("ascii")
    with db.write() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("sma_server_signing_key", seed),
        )
    return SigningKey(base64.b64decode(db.get_meta("sma_server_signing_key") or seed))


def server_key_payload(db: Database) -> dict[str, Any]:
    key = _load_signing_key(db)
    encoded = encode_public_key(key.verify_key)
    return {
        "public_key": encoded,
        "key_id": key_fingerprint(encoded),
        "scheme": RESPONSE_SCHEME,
    }


# -- request signing ---------------------------------------------------------


def _canonical(
    scheme: str,
    account_id: int | str,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body_digest: str,
) -> bytes:
    """The exact bytes both sides sign.

    Fields are NUL-separated and the body is summarized by its digest, so a
    signature cannot be moved to another path, method, account or body.
    """
    return "\0".join(
        [scheme, str(account_id), method.upper(), path, str(timestamp), nonce, body_digest]
    ).encode("utf-8")


def request_digest(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def signature_payload(
    account_id: int,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> bytes:
    return _canonical(SCHEME, account_id, method, path, timestamp, nonce, request_digest(body))


def verify_request_signature(
    public_key: str,
    account_id: int,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature: str,
) -> None:
    if not _NONCE_RE.match(nonce or ""):
        raise SmaError("invalid_nonce", "X-Midware-Nonce must be 16-128 base64url characters.", 401)
    try:
        signed = base64.b64decode((signature or "").strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SmaError("invalid_signature", "X-Midware-Signature is not valid base64.", 401) from exc
    if len(signed) != 64:
        raise SmaError("invalid_signature", "An Ed25519 signature is exactly 64 bytes.", 401)

    key = decode_public_key(public_key)
    payload = signature_payload(account_id, method, path, timestamp, nonce, body)
    try:
        key.verify(payload, signed)
    except BadSignatureError as exc:
        raise SmaError(
            "invalid_signature",
            "The request signature does not match the enrolled public key.",
            401,
        ) from exc


def response_signature(
    db: Database, account_id: int, method: str, path: str, timestamp: str, nonce: str, body: bytes
) -> dict[str, str]:
    key = _load_signing_key(db)
    payload = _canonical(
        RESPONSE_SCHEME, account_id, method, path, timestamp, nonce, request_digest(body)
    )
    signed = key.sign(payload).signature
    encoded = encode_public_key(key.verify_key)
    return {
        "X-Midware-Server-Signature": base64.b64encode(signed).decode("ascii"),
        "X-Midware-Server-Key": key_fingerprint(encoded),
    }


def verify_response_signature(
    server_public_key: str,
    account_id: int,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    body: bytes,
    signature: str,
) -> None:
    """Client-side check of a response, mirroring ``response_signature``."""
    key = decode_public_key(server_public_key)
    payload = _canonical(
        RESPONSE_SCHEME, account_id, method, path, timestamp, nonce, request_digest(body)
    )
    try:
        key.verify(payload, base64.b64decode(signature.strip(), validate=True))
    except (BadSignatureError, binascii.Error, ValueError) as exc:
        raise SmaError("invalid_signature", "The response signature is not valid.", 401) from exc


def clock_skew(timestamp: str, window_seconds: int, now: datetime | None = None) -> bool:
    try:
        value = float(timestamp)
    except (TypeError, ValueError):
        return False
    now = now or utcnow()
    return abs(now.timestamp() - value) <= max(1, window_seconds)


# -- enrollment validation ---------------------------------------------------


def normalize_name(raw: Any) -> str:
    if not isinstance(raw, str):
        raise SmaError("invalid_name", "name must be a string.")
    name = _BIDI_CHARS.sub("", _CONTROL_CHARS.sub("", raw)).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise SmaError("invalid_name", "name is required.")
    if len(name) > MAX_NAME_LENGTH:
        raise SmaError("invalid_name", f"name must be at most {MAX_NAME_LENGTH} characters.")
    if not any(char.isalnum() for char in name):
        raise SmaError("invalid_name", "name must contain at least one letter or digit.")
    if name.casefold() in RESERVED_NAMES:
        raise SmaError("reserved_name", f"'{name}' is reserved for the MidWare server itself.")
    return name


def normalize_redirect_uri(raw: Any) -> str:
    """Validate the browser bounce target at enrollment time.

    No credential ever travels in this URL, but it is still attacker-chosen and
    is shown on the consent page, so it must be a plain https URL (loopback may
    use http for local clients) with no userinfo and no fragment.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise SmaError("invalid_redirect_uri", "redirect_uri is required.")
    value = raw.strip()
    if len(value) > MAX_REDIRECT_LENGTH:
        raise SmaError("invalid_redirect_uri", "redirect_uri is too long.")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise SmaError("invalid_redirect_uri", "redirect_uri must be an absolute http(s) URL.")
    if parts.username or parts.password:
        raise SmaError("invalid_redirect_uri", "redirect_uri must not embed credentials.")
    if parts.fragment:
        raise SmaError("invalid_redirect_uri", "redirect_uri must not contain a fragment.")
    host = (parts.hostname or "").lower()
    loopback = host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
    if parts.scheme == "http" and not loopback:
        raise SmaError(
            "invalid_redirect_uri",
            "redirect_uri must use https unless it points at loopback.",
        )
    return value


def normalize_scopes(raw: Any) -> list[str]:
    """Keep only known, grantable scopes; an unknown name fails the request."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise SmaError("invalid_scopes", "scopes must be a list of strings.")
    cleaned: list[str] = []
    for item in raw:
        scope = item.strip()
        if scope in IMPLICIT_SCOPES:
            continue
        if scope not in SCOPES:
            raise SmaError("unknown_scope", f"Unknown scope '{scope}'.")
        if scope not in cleaned:
            cleaned.append(scope)
    if not cleaned:
        raise SmaError("invalid_scopes", "Request at least one permission.")
    return cleaned


def parse_scopes(raw: Any) -> list[str]:
    """Read scopes back from storage, tolerating an older/unknown entry."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, str) and item in SCOPES]


def enrollment_payload_hash(payload: dict[str, Any]) -> str:
    """Freeze what the admin is asked to approve.

    Approval compares this digest against a freshly recomputed one, so a row
    edited between enrollment and consent fails instead of silently expanding
    the grant.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- presentation ------------------------------------------------------------


def public_account(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "state": row["state"],
        "scopes": parse_scopes(row["scopes"]),
        "redirect_uri": row["redirect_uri"],
        "key_fingerprint": row["key_fingerprint"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "activated_at": row["activated_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
        "revoked_reason": row["revoked_reason"],
    }


def redact_route(row: Any) -> dict[str, Any]:
    """A route as the SMA sees it: everything but the upstream credential."""
    data = {key: row[key] for key in row.keys()}
    upstream = data.pop("upstream_key", "") or ""
    data["has_upstream_key"] = bool(upstream)
    return data


def redact_api_key(row: Any) -> dict[str, Any]:
    data = {key: row[key] for key in row.keys()}
    data.pop("token", None)
    return data


def revoke_account(db: Database, account_id: int, reason: str) -> int:
    """Retire an account and delete every key it minted.

    Revoking the management grant but leaving its keys alive would mean the
    account can keep spending upstream tokens through the proxy forever, which
    is exactly what an admin revoking an account is trying to stop.
    """
    from .images import unlink_image

    deleted = 0
    for key in db.list_api_keys_owned(account_id):
        for image in db.delete_api_key(int(key["id"])):
            unlink_image(image["id"], image["ext"])
        deleted += 1
    db.update_system_account(
        account_id,
        state="revoked",
        revoked_at=to_iso(utcnow()),
        revoked_reason=reason,
        token_hash=None,
        pending_token_hash=None,
        activation_token_hash=None,
        activation_expires_at=None,
    )
    return deleted


def expiry(seconds: int, now: datetime | None = None) -> str:
    return to_iso((now or utcnow()) + timedelta(seconds=max(1, seconds)))


def since(seconds: int, now: datetime | None = None) -> str:
    """The timestamp ``seconds`` in the past, for "recent activity" queries."""
    return to_iso((now or utcnow()) - timedelta(seconds=max(1, seconds)))
