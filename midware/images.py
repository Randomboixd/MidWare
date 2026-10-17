"""Server-side storage for image responses (per-key URL mode).

When a key's ``image_response_mode`` is ``"url"``, image generations that come
back as base64 are written to disk and the response is rewritten so each item
carries a MidWare ``/img/<id>`` URL instead. The bytes never touch the
``requests`` log in base64 form, so a stored response stays small.

Files are deleted lazily: on serve (expired), on key delete, and by the sweep
that runs after every store. There is no background worker, consistent with the
rest of MidWare.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path

from flask import current_app, url_for

from .auth import get_db
from .db import parse_iso, utcnow

IMAGE_CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _images_dir() -> Path:
    path = Path(current_app.config["IMAGES_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sniff_ext(data: bytes) -> str:
    """Detect the image type from its magic bytes; default to png."""
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return "png"


def _image_file(image_id: str, ext: str) -> Path:
    return _images_dir() / f"{image_id}.{ext}"


def store_image(api_key_id: int, data: bytes, retention_days: int) -> str | None:
    """Write image bytes to disk and register the row; returns the public id."""
    if not data:
        return None
    ext = _sniff_ext(data)
    db = get_db()
    image_id = db.create_stored_image(api_key_id, ext, retention_days)
    _image_file(image_id, ext).write_bytes(data)
    sweep()
    return image_id


def unlink_image(image_id: str, ext: str) -> None:
    try:
        _image_file(image_id, ext).unlink()
    except OSError:
        pass


def sweep() -> int:
    """Delete expired stored images (rows and files). Returns how many."""
    rows = get_db().prune_expired_stored_images()
    for row in rows:
        unlink_image(row["id"], row["ext"])
    return len(rows)


def load_image(image_id: str) -> tuple[bytes, str] | None:
    """Return ``(bytes, ext)`` for a stored image, or None if gone/expired."""
    db = get_db()
    row = db.get_stored_image(image_id)
    if row is None:
        return None
    expires = parse_iso(row["expires_at"])
    if expires is not None and expires <= utcnow():
        db.delete_stored_image(image_id)
        unlink_image(row["id"], row["ext"])
        return None
    try:
        data = _image_file(row["id"], row["ext"]).read_bytes()
    except OSError:
        db.delete_stored_image(image_id)
        return None
    return data, row["ext"]


def rewrite_image_body(
    body: bytes, api_key_id: int, retention_days: int, force_https: bool = False
) -> bytes:
    """Replace every ``b64_json`` item in an image response with a stored URL.

    Items that already carry a URL (or undecodable base64) are left alone; a
    body with nothing to rewrite is returned unchanged. ``force_https`` flips
    the advertised scheme for keys behind a TLS-terminating proxy.
    """
    if not body:
        return body
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return body
    changed = False
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        encoded = item.get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            continue
        try:
            data = base64.b64decode(encoded)
        except (binascii.Error, ValueError):
            continue
        image_id = store_image(api_key_id, data, retention_days)
        if image_id is None:
            continue
        url = url_for("proxy.serve_image", image_id=image_id, _external=True)
        if force_https and url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        item.pop("b64_json", None)
        item["url"] = url
        changed = True
    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
