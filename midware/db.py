"""SQLite persistence layer for MidWare.

The whole app talks to one ``Database`` instance kept in
``app.extensions["midware_db"]``. Connections are per-thread (``check_same_thread``
stays enabled) so the threaded Flask dev server and the test client are safe.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    token       TEXT    NOT NULL UNIQUE,
    cors_allow_origin   TEXT    NOT NULL DEFAULT '*',
    allow_premodels     INTEGER NOT NULL DEFAULT 1,
    restrict_premodels  INTEGER NOT NULL DEFAULT 0,
    allowed_premodel_ids TEXT,
    restrict_providers  INTEGER NOT NULL DEFAULT 0,
    allowed_route_ids   TEXT,
    restrict_models     INTEGER NOT NULL DEFAULT 0,
    allowed_model_ids   TEXT,
    created_at  TEXT    NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS routes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    target_host     TEXT    NOT NULL,
    upstream_key    TEXT    NOT NULL DEFAULT '',
    api_key_id      INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL,
    request_path        TEXT    NOT NULL DEFAULT '',
    method              TEXT    NOT NULL DEFAULT 'POST',
    model               TEXT,
    model_raw           TEXT,
    host_prefix         TEXT,
    status_code         INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    streamed            INTEGER NOT NULL DEFAULT 0,
    prompt_tokens       INTEGER NOT NULL DEFAULT 0,
    completion_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    token_source        TEXT    NOT NULL DEFAULT 'upstream',
    upstream_request_id TEXT,
    error               TEXT,
    request_body        TEXT,
    response_body       TEXT,
    route_id            INTEGER REFERENCES routes(id) ON DELETE SET NULL,
    api_key_id          INTEGER REFERENCES api_keys(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_requests_created_at ON requests(created_at);
CREATE INDEX IF NOT EXISTS idx_requests_api_key    ON requests(api_key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_requests_route      ON requests(route_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id   INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    model_id   TEXT    NOT NULL,
    fetched_at TEXT    NOT NULL,
    UNIQUE(route_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_models_route ON models(route_id);

CREATE TABLE IF NOT EXISTS premodels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    slug          TEXT    NOT NULL UNIQUE,
    description   TEXT    NOT NULL DEFAULT '',
    route_id      INTEGER REFERENCES routes(id) ON DELETE SET NULL,
    model         TEXT    NOT NULL DEFAULT '',
    merge_mode    TEXT    NOT NULL DEFAULT 'append',
    accept_mwvars INTEGER NOT NULL DEFAULT 0,
    prompt_mode   TEXT    NOT NULL DEFAULT 'simple',
    system_prompt TEXT    NOT NULL DEFAULT '',
    preset_json   TEXT,
    params_enabled INTEGER NOT NULL DEFAULT 0,
    params_json   TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_premodels_route ON premodels(route_id);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def window_start(period: str, now: datetime | None = None) -> datetime:
    """Return the inclusive UTC start of ``period`` in {"day", "month", "year"}."""
    now = now or utcnow()
    if period == "day":
        return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    if period == "month":
        return datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    if period == "year":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    raise ValueError(f"unknown period: {period!r}")


class Database:
    def __init__(self, path: str | Path, timeout: float = 5.0):
        self.path = str(path)
        self.timeout = timeout
        self._local = threading.local()

        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._anchor = self._connect()

    # -- connection plumbing ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._anchor if self.path == ":memory:" else self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        conn = self.conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def init_schema(self) -> None:
        self._migrate()
        with self.write() as conn:
            conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Bring pre-existing databases up to the current schema.

        ``executescript`` runs its ``CREATE TABLE IF NOT EXISTS`` statements
        verbatim, so an old ``requests`` table keeps its old shape and the newer
        indexes fail. We update the table *before* the script runs.
        """
        existing = {row["name"] for row in self._rows("PRAGMA table_info(requests)")}
        if existing:
            self._add_columns(
                "requests",
                existing,
                {
                    "model_raw": "TEXT",
                    "host_prefix": "TEXT",
                    "upstream_request_id": "TEXT",
                    "error": "TEXT",
                    "request_body": "TEXT",
                    "response_body": "TEXT",
                    "route_id": "INTEGER",
                    "api_key_id": "INTEGER",
                    "token_source": "TEXT NOT NULL DEFAULT 'upstream'",
                },
            )

        existing_premodels = {row["name"] for row in self._rows("PRAGMA table_info(premodels)")}
        if existing_premodels:
            self._add_columns(
                "premodels",
                existing_premodels,
                {
                    "params_enabled": "INTEGER NOT NULL DEFAULT 0",
                    "params_json": "TEXT",
                },
            )

        existing_keys = {row["name"] for row in self._rows("PRAGMA table_info(api_keys)")}
        if existing_keys:
            self._add_columns(
                "api_keys",
                existing_keys,
                {
                    "description": "TEXT NOT NULL DEFAULT ''",
                    "cors_allow_origin": "TEXT NOT NULL DEFAULT '*'",
                    "allow_premodels": "INTEGER NOT NULL DEFAULT 1",
                    "restrict_premodels": "INTEGER NOT NULL DEFAULT 0",
                    "allowed_premodel_ids": "TEXT",
                    "restrict_providers": "INTEGER NOT NULL DEFAULT 0",
                    "allowed_route_ids": "TEXT",
                    "restrict_models": "INTEGER NOT NULL DEFAULT 0",
                    "allowed_model_ids": "TEXT",
                },
            )

    def _add_columns(self, table: str, existing: set[str], added: dict[str, str]) -> None:
        with self.write() as conn:
            for column, definition in added.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- helpers ------------------------------------------------------------

    def _row(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params).fetchall())

    def _scalar(self, sql: str, params: tuple = (), default: Any = 0) -> Any:
        row = self._row(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    # -- settings -----------------------------------------------------------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._row("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_int_setting(self, key: str, default: int) -> int:
        raw = self.get_setting(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._row("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def issue_setup_code(self) -> str:
        """Return the persisted claim code, creating it once if absent.

        Stored in ``meta`` rather than memory so multiple worker processes share
        the same code and a restart does not invalidate what was logged.
        """
        code = secrets.token_hex(16)
        with self.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("admin_setup_code", code),
            )
        return self.get_meta("admin_setup_code") or code

    def clear_setup_code(self) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", ("admin_setup_code",))

    def request_log_limit(self, default: int = 10) -> int:
        return max(0, self.get_int_setting("request_log_limit", default))

    # -- setup / routes -----------------------------------------------------

    def is_configured(self) -> bool:
        return self.get_meta("setup_complete") == "1"

    def mark_configured(self) -> None:
        self.set_meta("setup_complete", "1")
        self.set_meta("configured_at", to_iso(utcnow()))

    def create_route(
        self,
        name: str,
        target_host: str,
        upstream_key: str,
        api_key_id: int | None = None,
        is_active: bool = True,
    ) -> int:
        with self.write() as conn:
            cur = conn.execute(
                "INSERT INTO routes (name, target_host, upstream_key, api_key_id, is_active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, target_host.rstrip("/"), upstream_key, api_key_id, int(is_active), to_iso(utcnow())),
            )
            return int(cur.lastrowid)

    def update_route(self, route_id: int, **fields: Any) -> None:
        allowed = {"name", "target_host", "upstream_key", "api_key_id", "is_active"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "is_active" in updates:
            updates["is_active"] = int(bool(updates["is_active"]))
        if "target_host" in updates and updates["target_host"]:
            updates["target_host"] = str(updates["target_host"]).rstrip("/")
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.write() as conn:
            conn.execute(f"UPDATE routes SET {assignments} WHERE id = ?", (*updates.values(), route_id))

    def delete_route(self, route_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM routes WHERE id = ?", (route_id,))

    def get_route(self, route_id: int) -> sqlite3.Row | None:
        return self._row("SELECT * FROM routes WHERE id = ?", (route_id,))

    def list_routes(self) -> list[sqlite3.Row]:
        return self._rows("SELECT * FROM routes ORDER BY id")

    def active_route(self) -> sqlite3.Row | None:
        route = self._row("SELECT * FROM routes WHERE is_active = 1 ORDER BY id LIMIT 1")
        return route or self._row("SELECT * FROM routes ORDER BY id LIMIT 1")

    # -- discovered models --------------------------------------------------

    def replace_models(self, route_id: int, model_ids: list[str]) -> int:
        """Store the model list for a route, replacing whatever was there."""
        cleaned: list[str] = []
        for model_id in model_ids:
            text = str(model_id).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        stamp = to_iso(utcnow())
        with self.write() as conn:
            conn.execute("DELETE FROM models WHERE route_id = ?", (route_id,))
            conn.executemany(
                "INSERT INTO models (route_id, model_id, fetched_at) VALUES (?, ?, ?)",
                [(route_id, model_id, stamp) for model_id in cleaned],
            )
        return len(cleaned)

    def list_models(self, route_id: int | None = None, *, active_only: bool = False) -> list[dict[str, Any]]:
        """Discovered models as ``{"model_id", "route_id", ...}`` dicts.

        For the default route the ``model_id`` is returned bare; every other
        route prefixes it with ``[Name]`` so it can be sent back verbatim. With
        ``active_only`` only the default route's models plus the other routes'
        namespaced ones are kept (which is always the case today, but keeps the
        default/prefix contract explicit).
        """
        routes = {row["id"]: row for row in self.list_routes()}
        active = self.active_route()
        active_id = active["id"] if active else None
        params: list[Any] = []
        sql = "SELECT * FROM models"
        if route_id is not None:
            sql += " WHERE route_id = ?"
            params.append(route_id)
        sql += " ORDER BY route_id, model_id"

        models: list[dict[str, Any]] = []
        for row in self._rows(sql, tuple(params)):
            route = routes.get(row["route_id"])
            if route is None:
                continue
            if active_only and row["route_id"] == active_id:
                pass
            model_id = row["model_id"]
            slug = model_id if row["route_id"] == active_id else f"[{route['name']}]{model_id}"
            models.append(
                {
                    "model_id": model_id,
                    "slug": slug,
                    "raw_id": model_id,
                    "route_id": row["route_id"],
                    "route_name": route["name"],
                    "is_default": row["route_id"] == active_id,
                    "fetched_at": row["fetched_at"],
                }
            )
        return models

    def model_summary(self) -> list[dict[str, Any]]:
        """Per-route counts for the UI, keyed by route id order."""
        counts = {
            row["route_id"]: row["total"]
            for row in self._rows(
                "SELECT route_id, COUNT(*) AS total, MAX(fetched_at) AS last FROM models GROUP BY route_id"
            )
        }
        last = {
            row["route_id"]: row["last"]
            for row in self._rows("SELECT route_id, MAX(fetched_at) AS last FROM models GROUP BY route_id")
        }
        active = self.active_route()
        active_id = active["id"] if active else None
        summary = []
        for route in self.list_routes():
            summary.append(
                {
                    "route_id": route["id"],
                    "route_name": route["name"],
                    "count": int(counts.get(route["id"], 0)),
                    "fetched_at": last.get(route["id"]),
                    "is_default": route["id"] == active_id,
                }
            )
        return summary

    def count_models(self) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM models"))

    # -- premodels ----------------------------------------------------------

    _PREMODEL_FIELDS = (
        "name",
        "slug",
        "description",
        "route_id",
        "model",
        "merge_mode",
        "accept_mwvars",
        "prompt_mode",
        "system_prompt",
        "preset_json",
        "params_enabled",
        "params_json",
    )

    def create_premodel(
        self,
        *,
        name: str,
        slug: str,
        description: str = "",
        route_id: int | None = None,
        model: str = "",
        merge_mode: str = "append",
        accept_mwvars: bool = False,
        prompt_mode: str = "simple",
        system_prompt: str = "",
        preset_json: str | None = None,
        params_enabled: bool = False,
        params_json: str | None = None,
    ) -> int:
        stamp = to_iso(utcnow())
        with self.write() as conn:
            cur = conn.execute(
                """
                INSERT INTO premodels (
                    name, slug, description, route_id, model, merge_mode,
                    accept_mwvars, prompt_mode, system_prompt, preset_json,
                    params_enabled, params_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    slug,
                    description,
                    route_id,
                    model,
                    merge_mode,
                    int(bool(accept_mwvars)),
                    prompt_mode,
                    system_prompt,
                    preset_json,
                    int(bool(params_enabled)),
                    params_json,
                    stamp,
                    stamp,
                ),
            )
            return int(cur.lastrowid)

    def update_premodel(self, premodel_id: int, **fields: Any) -> None:
        updates = {key: value for key, value in fields.items() if key in self._PREMODEL_FIELDS}
        if not updates:
            return
        if "accept_mwvars" in updates:
            updates["accept_mwvars"] = int(bool(updates["accept_mwvars"]))
        if "params_enabled" in updates:
            updates["params_enabled"] = int(bool(updates["params_enabled"]))
        updates["updated_at"] = to_iso(utcnow())
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.write() as conn:
            conn.execute(
                f"UPDATE premodels SET {assignments} WHERE id = ?",
                (*updates.values(), premodel_id),
            )

    def delete_premodel(self, premodel_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM premodels WHERE id = ?", (premodel_id,))

    def get_premodel(self, premodel_id: int) -> sqlite3.Row | None:
        return self._row("SELECT * FROM premodels WHERE id = ?", (premodel_id,))

    def get_premodel_by_slug(self, slug: str) -> sqlite3.Row | None:
        if not slug:
            return None
        return self._row("SELECT * FROM premodels WHERE slug = ? COLLATE NOCASE", (slug,))

    def list_premodels(self) -> list[sqlite3.Row]:
        return self._rows("SELECT * FROM premodels ORDER BY name COLLATE NOCASE, id")

    # -- api keys -----------------------------------------------------------

    _API_KEY_FIELDS = (
        "name",
        "description",
        "cors_allow_origin",
        "allow_premodels",
        "restrict_premodels",
        "allowed_premodel_ids",
        "restrict_providers",
        "allowed_route_ids",
        "restrict_models",
        "allowed_model_ids",
    )

    @staticmethod
    def generate_token(prefix: str = "mw-") -> str:
        return prefix + secrets.token_urlsafe(24)

    def create_api_key(
        self,
        name: str,
        *,
        description: str = "",
        cors_allow_origin: str = "*",
        allow_premodels: bool = True,
        restrict_premodels: bool = False,
        allowed_premodel_ids: list[int] | None = None,
        restrict_providers: bool = False,
        allowed_route_ids: list[int] | None = None,
        restrict_models: bool = False,
        allowed_model_ids: list[str] | None = None,
    ) -> str:
        token = self.generate_token()
        with self.write() as conn:
            conn.execute(
                """
                INSERT INTO api_keys (
                    name, description, token, cors_allow_origin, allow_premodels,
                    restrict_premodels, allowed_premodel_ids, restrict_providers,
                    allowed_route_ids, restrict_models, allowed_model_ids, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    description,
                    token,
                    cors_allow_origin,
                    int(bool(allow_premodels)),
                    int(bool(restrict_premodels)),
                    dump_id_list(allowed_premodel_ids),
                    int(bool(restrict_providers)),
                    dump_id_list(allowed_route_ids),
                    int(bool(restrict_models)),
                    dump_model_list(allowed_model_ids),
                    to_iso(utcnow()),
                ),
            )
        return token

    def update_api_key(self, key_id: int, **fields: Any) -> None:
        updates = {key: value for key, value in fields.items() if key in self._API_KEY_FIELDS}
        if not updates:
            return
        for flag in ("allow_premodels", "restrict_premodels", "restrict_providers", "restrict_models"):
            if flag in updates:
                updates[flag] = int(bool(updates[flag]))
        for ids in ("allowed_premodel_ids", "allowed_route_ids"):
            if ids in updates:
                updates[ids] = dump_id_list(updates[ids])
        if "allowed_model_ids" in updates:
            updates["allowed_model_ids"] = dump_model_list(updates["allowed_model_ids"])
        if "cors_allow_origin" in updates:
            updates["cors_allow_origin"] = str(updates["cors_allow_origin"]).strip()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.write() as conn:
            conn.execute(f"UPDATE api_keys SET {assignments} WHERE id = ?", (*updates.values(), key_id))

    def get_api_key_by_token(self, token: str) -> sqlite3.Row | None:
        if not token:
            return None
        return self._row("SELECT * FROM api_keys WHERE token = ?", (token,))

    def get_api_key(self, key_id: int) -> sqlite3.Row | None:
        return self._row("SELECT * FROM api_keys WHERE id = ?", (key_id,))

    def list_api_keys(self) -> list[sqlite3.Row]:
        return self._rows(
            """
            SELECT k.*,
                   (SELECT COUNT(*) FROM requests r WHERE r.api_key_id = k.id) AS request_count,
                   (SELECT COALESCE(SUM(r.total_tokens), 0) FROM requests r WHERE r.api_key_id = k.id) AS token_count
            FROM api_keys k
            ORDER BY k.id
            """
        )

    def delete_api_key(self, key_id: int) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))

    def touch_api_key(self, key_id: int) -> None:
        with self.write() as conn:
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (to_iso(utcnow()), key_id))

    def resolve_api_key(self, token: str | None) -> sqlite3.Row | None:
        return self.get_api_key_by_token(token or "")

    # -- requests -----------------------------------------------------------

    def record_request(
        self,
        *,
        api_key_id: int | None,
        route_id: int | None,
        method: str,
        request_path: str,
        model: str | None,
        status_code: int,
        latency_ms: int,
        streamed: bool,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        token_source: str = "upstream",
        upstream_request_id: str | None = None,
        error: str | None = None,
        request_body: str | None = None,
        response_body: str | None = None,
        model_raw: str | None = None,
        host_prefix: str | None = None,
    ) -> int:
        with self.write() as conn:
            cur = conn.execute(
                """
                INSERT INTO requests (
                    created_at, request_path, method, model, model_raw, host_prefix,
                    status_code, latency_ms, streamed, prompt_tokens, completion_tokens,
                    total_tokens, token_source, upstream_request_id, error, request_body,
                    response_body, route_id, api_key_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    to_iso(utcnow()),
                    request_path,
                    method,
                    model,
                    model_raw,
                    host_prefix,
                    int(status_code),
                    int(latency_ms),
                    int(bool(streamed)),
                    int(prompt_tokens),
                    int(completion_tokens),
                    int(total_tokens),
                    token_source or "upstream",
                    upstream_request_id,
                    error,
                    request_body,
                    response_body,
                    route_id,
                    api_key_id,
                ),
            )
            return int(cur.lastrowid)

    def list_requests(
        self,
        limit: int = 50,
        offset: int = 0,
        api_key_id: int | None = None,
        route_id: int | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if api_key_id is not None:
            clauses.append("r.api_key_id = ?")
            params.append(api_key_id)
        if route_id is not None:
            clauses.append("r.route_id = ?")
            params.append(route_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])
        return self._rows(
            f"""
            SELECT r.*, k.name AS api_key_name, rt.name AS route_name
            FROM requests r
            LEFT JOIN api_keys k ON k.id = r.api_key_id
            LEFT JOIN routes rt ON rt.id = r.route_id
            {where}
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )

    def count_requests(self, api_key_id: int | None = None, route_id: int | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if api_key_id is not None:
            clauses.append("api_key_id = ?")
            params.append(api_key_id)
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(route_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return int(self._scalar(f"SELECT COUNT(*) FROM requests {where}", tuple(params)))

    def get_request(self, request_id: int) -> sqlite3.Row | None:
        return self._row(
            """
            SELECT r.*, k.name AS api_key_name, rt.name AS route_name
            FROM requests r
            LEFT JOIN api_keys k ON k.id = r.api_key_id
            LEFT JOIN routes rt ON rt.id = r.route_id
            WHERE r.id = ?
            """,
            (request_id,),
        )

    def purge_requests(self) -> int:
        with self.write() as conn:
            cur = conn.execute("DELETE FROM requests")
            return int(cur.rowcount)

    def prune_requests(self, api_key_id: int | None = None) -> int:
        """Trim the request log so each API key keeps at most ``request_log_limit`` rows."""
        limit = self.request_log_limit()
        removed = 0
        with self.write() as conn:
            if api_key_id is None:
                key_ids = [row["id"] for row in conn.execute("SELECT id FROM api_keys").fetchall()]
                key_ids.append(None)
            else:
                key_ids = [api_key_id]
            for key_id in key_ids:
                if key_id is None:
                    clause, params = "api_key_id IS NULL", ()
                else:
                    clause, params = "api_key_id = ?", (key_id,)
                if limit <= 0:
                    cur = conn.execute(f"DELETE FROM requests WHERE {clause}", params)
                    removed += int(cur.rowcount)
                    continue
                cur = conn.execute(
                    f"""
                    DELETE FROM requests
                    WHERE {clause} AND id NOT IN (
                        SELECT id FROM requests WHERE {clause}
                        ORDER BY created_at DESC, id DESC LIMIT ?
                    )
                    """,
                    params + params + (limit,),
                )
                removed += int(cur.rowcount)
        return removed

    # -- statistics ---------------------------------------------------------

    def usage_totals(
        self,
        period: str = "day",
        api_key_id: int | None = None,
        route_id: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        start = to_iso(window_start(period, now))
        clauses = ["created_at >= ?"]
        params: list[Any] = [start]
        if api_key_id is not None:
            clauses.append("api_key_id = ?")
            params.append(api_key_id)
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(route_id)
        row = self._row(
            f"""
            SELECT COUNT(*)                            AS requests,
                   COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                   COALESCE(AVG(latency_ms), 0)        AS avg_latency_ms
            FROM requests
            WHERE {' AND '.join(clauses)}
            """,
            tuple(params),
        )
        return {
            "period": period,
            "start": start,
            "requests": int(row["requests"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "total_tokens": int(row["total_tokens"]),
            "avg_latency_ms": int(row["avg_latency_ms"]),
        }

    def daily_usage(
        self,
        days: int = 365,
        api_key_id: int | None = None,
        route_id: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, dict[str, int]]:
        """Token totals per UTC calendar day, keyed by ISO date string."""
        now = now or utcnow()
        start = window_start("day", now) - timedelta(days=max(days - 1, 0))
        clauses = ["created_at >= ?"]
        params: list[Any] = [to_iso(start)]
        if api_key_id is not None:
            clauses.append("api_key_id = ?")
            params.append(api_key_id)
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(route_id)
        rows = self._rows(
            f"""
            SELECT substr(created_at, 1, 10) AS day,
                   COUNT(*)                  AS requests,
                   COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0)      AS total_tokens
            FROM requests
            WHERE {' AND '.join(clauses)}
            GROUP BY day
            """,
            tuple(params),
        )
        return {
            row["day"]: {
                "requests": int(row["requests"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "total_tokens": int(row["total_tokens"]),
            }
            for row in rows
        }

    def model_usage(
        self,
        api_key_id: int | None = None,
        route_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if api_key_id is not None:
            clauses.append("api_key_id = ?")
            params.append(api_key_id)
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(route_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(
            f"""
            SELECT COALESCE(NULLIF(model, ''), 'unknown') AS model,
                   COUNT(*)                  AS requests,
                   COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0)      AS total_tokens
            FROM requests
            {where}
            GROUP BY model
            ORDER BY total_tokens DESC, requests DESC
            """,
            tuple(params),
        )
        return [
            {
                "model": row["model"],
                "requests": int(row["requests"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "total_tokens": int(row["total_tokens"]),
            }
            for row in rows
        ]

    def host_usage(
        self,
        api_key_id: int | None = None,
        route_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if api_key_id is not None:
            clauses.append("api_key_id = ?")
            params.append(api_key_id)
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(route_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._rows(
            f"""
            SELECT COALESCE(NULLIF(host_prefix, ''), 'default') AS host,
                   COUNT(*)                            AS requests,
                   COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0)      AS total_tokens
            FROM requests
            {where}
            GROUP BY host
            ORDER BY total_tokens DESC, requests DESC
            """,
            tuple(params),
        )
        return [
            {
                "host": row["host"],
                "requests": int(row["requests"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "total_tokens": int(row["total_tokens"]),
            }
            for row in rows
        ]

    def overview(self, api_key_id: int | None = None, route_id: int | None = None) -> dict[str, Any]:
        return {
            "day": self.usage_totals("day", api_key_id, route_id),
            "month": self.usage_totals("month", api_key_id, route_id),
            "year": self.usage_totals("year", api_key_id, route_id),
            "all_time": self._all_time(api_key_id, route_id),
            "keys": self.count_api_keys(),
            "routes": len(self.list_routes()),
        }

    def _all_time(self, api_key_id: int | None, route_id: int | None) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if api_key_id is not None:
            clauses.append("api_key_id = ?")
            params.append(api_key_id)
        if route_id is not None:
            clauses.append("route_id = ?")
            params.append(route_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._row(
            f"""
            SELECT COUNT(*)                            AS requests,
                   COALESCE(SUM(prompt_tokens), 0)     AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(total_tokens), 0)      AS total_tokens,
                   COALESCE(AVG(latency_ms), 0)        AS avg_latency_ms
            FROM requests {where}
            """,
            tuple(params),
        )
        return {
            "period": "all",
            "start": None,
            "requests": int(row["requests"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
            "total_tokens": int(row["total_tokens"]),
            "avg_latency_ms": int(row["avg_latency_ms"]),
        }

    def count_api_keys(self) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM api_keys"))

    def heatmap(
        self,
        weeks: int = 52,
        api_key_id: int | None = None,
        route_id: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """GitHub-style contribution grid: columns of weeks (Sun-Sat), oldest first."""
        now = now or utcnow()
        today = date(now.year, now.month, now.day)
        usage = self.daily_usage(days=weeks * 7, api_key_id=api_key_id, route_id=route_id, now=now)
        peak = max((entry["total_tokens"] for entry in usage.values()), default=0)

        begin_of_week = today - timedelta(days=(today.weekday() + 1) % 7)  # Sunday
        start = begin_of_week - timedelta(weeks=weeks - 1)

        grid: list[list[dict[str, Any]]] = []
        for week_index in range(weeks):
            column: list[dict[str, Any]] = []
            for weekday in range(7):
                day = start + timedelta(days=week_index * 7 + weekday)
                if day > today:
                    column.append({"date": day.isoformat(), "future": True, "tokens": 0, "requests": 0, "level": 0})
                    continue
                entry = usage.get(day.isoformat())
                tokens = entry["total_tokens"] if entry else 0
                column.append(
                    {
                        "date": day.isoformat(),
                        "future": False,
                        "tokens": tokens,
                        "requests": entry["requests"] if entry else 0,
                        "level": self._heat_level(tokens, peak),
                    }
                )
            grid.append(column)

        return {
            "weeks": grid,
            "peak": peak,
            "total_tokens": sum(entry["total_tokens"] for entry in usage.values()),
            "active_days": sum(1 for entry in usage.values() if entry["total_tokens"] > 0),
            "start": start.isoformat(),
            "end": today.isoformat(),
        }

    @staticmethod
    def _heat_level(tokens: int, peak: int) -> int:
        if tokens <= 0 or peak <= 0:
            return 0
        ratio = tokens / peak
        if ratio <= 0.25:
            return 1
        if ratio <= 0.5:
            return 2
        if ratio <= 0.75:
            return 3
        return 4


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def dump_id_list(ids: Any) -> str | None:
    """Serialize a list of integer ids for a ``TEXT`` json column."""
    if not ids:
        return None
    cleaned: set[int] = set()
    for item in ids:
        try:
            cleaned.add(int(item))
        except (TypeError, ValueError):
            continue
    if not cleaned:
        return None
    return json.dumps(sorted(cleaned))


def parse_id_list(raw: Any) -> set[int]:
    """Read an id list back; unknown or corrupt values are ignored, never raised."""
    if not raw:
        return set()
    if isinstance(raw, (list, tuple, set)):
        data: Any = list(raw)
    else:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return set()
    if not isinstance(data, list):
        return set()
    ids: set[int] = set()
    for item in data:
        try:
            ids.add(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def model_key(route_id: int, model_id: str) -> str:
    """Stable identity for a catalogue entry: ``"<route_id>:<model_id>"``.

    A model id alone is not unique across providers, so the route is part of the
    key. ``model_id`` may itself contain colons, so only the first one splits.
    """
    return f"{int(route_id)}:{model_id}"


def dump_model_list(keys: Any) -> str | None:
    """Serialize ``"<route_id>:<model_id>"`` keys for a ``TEXT`` json column."""
    if not keys:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in keys:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return json.dumps(cleaned) if cleaned else None


def parse_model_list(raw: Any) -> set[str]:
    """Read model keys back; unknown or corrupt values are ignored, never raised."""
    if not raw:
        return set()
    if isinstance(raw, (list, tuple, set)):
        data: Any = list(raw)
    else:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return set()
    if not isinstance(data, list):
        return set()
    return {str(item).strip() for item in data if str(item).strip()}
