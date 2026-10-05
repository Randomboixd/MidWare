from __future__ import annotations

from datetime import datetime, timezone

from midware.db import Database, window_start


def make_db() -> Database:
    db = Database(":memory:")
    db.init_schema()
    return db


def test_window_start_boundaries():
    now = datetime(2026, 9, 17, 13, 45, tzinfo=timezone.utc)
    assert window_start("day", now).isoformat() == "2026-09-17T00:00:00+00:00"
    assert window_start("month", now).isoformat() == "2026-09-01T00:00:00+00:00"
    assert window_start("year", now).isoformat() == "2026-01-01T00:00:00+00:00"


def test_route_and_key_lifecycle():
    db = make_db()
    token = db.create_api_key("primary")
    key = db.get_api_key_by_token(token)
    assert key is not None and key["name"] == "primary"

    route_id = db.create_route("OpenAI", "https://api.openai.com/", "sk-abc", key["id"])
    route = db.get_route(route_id)
    assert route["target_host"] == "https://api.openai.com"
    assert db.active_route()["id"] == route_id

    db.update_route(route_id, is_active=False)
    assert db.get_route(route_id)["is_active"] == 0
    assert db.active_route()["id"] == route_id


def test_settings_and_pruning_keeps_newest():
    db = make_db()
    db.set_setting("request_log_limit", "2")
    token = db.create_api_key("k")
    key = db.get_api_key_by_token(token)

    for index in range(5):
        db.record_request(
            api_key_id=key["id"],
            route_id=None,
            method="POST",
            request_path="chat/completions",
            model="gpt-4o",
            status_code=200,
            latency_ms=100 + index,
            streamed=False,
            prompt_tokens=index,
            completion_tokens=index,
            total_tokens=index * 2,
        )

    assert db.count_requests() == 5
    removed = db.prune_requests(key["id"])
    assert removed == 3
    rows = db.list_requests(limit=10)
    assert len(rows) == 2
    assert [row["total_tokens"] for row in rows] == [8, 6]


def test_usage_totals_and_heatmap():
    db = make_db()
    token = db.create_api_key("k")
    key = db.get_api_key_by_token(token)
    db.record_request(
        api_key_id=key["id"], route_id=None, method="POST", request_path="p",
        model="m", status_code=200, latency_ms=50, streamed=False,
        prompt_tokens=100, completion_tokens=200, total_tokens=300,
    )

    totals = db.usage_totals("day")
    assert totals["total_tokens"] == 300
    assert totals["requests"] == 1

    heatmap = db.heatmap(weeks=2)
    assert heatmap["total_tokens"] == 300
    assert heatmap["peak"] == 300
    assert heatmap["active_days"] == 1
    assert len(heatmap["weeks"]) == 2
    assert all(len(column) == 7 for column in heatmap["weeks"])
    levels = [cell["level"] for column in heatmap["weeks"] for cell in column if not cell["future"]]
    assert 4 in levels


def test_msq_migration_adds_hardcore_columns(tmp_path):
    from midware.db import Database

    db = Database(str(tmp_path / "old.db"))
    with db.write() as conn:
        conn.executescript(
            """
            CREATE TABLE routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                target_host TEXT NOT NULL, upstream_key TEXT NOT NULL DEFAULT '',
                api_key_id INTEGER, is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE msq_recorders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                route_id INTEGER, model TEXT NOT NULL DEFAULT '',
                system_prompt TEXT NOT NULL DEFAULT '', user_prompt TEXT NOT NULL DEFAULT '',
                interval_seconds INTEGER NOT NULL DEFAULT 3600,
                enabled INTEGER NOT NULL DEFAULT 1,
                penalize_symbols INTEGER NOT NULL DEFAULT 1,
                slop_list TEXT NOT NULL DEFAULT '',
                max_ttft_ms INTEGER NOT NULL DEFAULT 30000,
                max_total_ms INTEGER NOT NULL DEFAULT 60000,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                last_run_at TEXT, next_run_at TEXT
            );
            CREATE TABLE msq_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, recorder_id INTEGER NOT NULL,
                created_at TEXT NOT NULL, status_code INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0, score REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            );
            """
        )

    db.init_schema()

    recorder_cols = {row["name"] for row in db._rows("PRAGMA table_info(msq_recorders)")}
    check_cols = {row["name"] for row in db._rows("PRAGMA table_info(msq_checks)")}
    key_cols = {row["name"] for row in db._rows("PRAGMA table_info(api_keys)")}
    assert "hardcore_json" in recorder_cols
    assert "collect_requests" in recorder_cols
    assert "ignore_thinking" in recorder_cols
    assert "hard_fail" in check_cols
    assert "is_system" in key_cols


def test_rate_limit_migration_adds_columns(tmp_path):
    from midware.db import Database

    db = Database(str(tmp_path / "old.db"))
    with db.write() as conn:
        conn.executescript(
            """
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
            );
            """
        )

    db.init_schema()

    key_cols = {row["name"] for row in db._rows("PRAGMA table_info(api_keys)")}
    tables = {
        row["name"] for row in db._rows("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "limit_tokens_enabled" in key_cols
    assert "max_tokens_day" in key_cols
    assert "limit_requests_minute" in key_cols
    assert "max_requests_week" in key_cols
    assert "rate_usage" in tables

    token = db.create_api_key("migrated")
    key = db.get_api_key_by_token(token)
    assert key["limit_tokens_enabled"] == 0
    assert key["max_requests_minute"] == 0


def test_filters_scope_totals():
    db = make_db()
    first = db.get_api_key_by_token(db.create_api_key("a"))
    second = db.get_api_key_by_token(db.create_api_key("b"))
    for key, tokens in ((first, 10), (second, 90)):
        db.record_request(
            api_key_id=key["id"], route_id=None, method="POST", request_path="p",
            model="m", status_code=200, latency_ms=1, streamed=False,
            prompt_tokens=tokens, completion_tokens=0, total_tokens=tokens,
        )

    assert db.usage_totals("day")["total_tokens"] == 100
    assert db.usage_totals("day", api_key_id=first["id"])["total_tokens"] == 10


def _record(db, key_id, *, total, latency=10):
    db.record_request(
        api_key_id=key_id, route_id=None, method="POST", request_path="p",
        model="m", status_code=200, latency_ms=latency, streamed=False,
        prompt_tokens=total, completion_tokens=0, total_tokens=total,
    )


def test_totals_survive_pruning():
    db = make_db()
    db.set_setting("request_log_limit", "2")
    key = db.get_api_key_by_token(db.create_api_key("k"))

    for index in range(1, 6):
        _record(db, key["id"], total=index * 10)

    db.prune_requests(key["id"])
    assert db.count_requests() == 2

    listed = [row for row in db.list_api_keys() if row["id"] == key["id"]][0]
    assert listed["request_count"] == 5
    assert listed["token_count"] == 150

    assert db.usage_totals("day")["requests"] == 5
    assert db.usage_totals("day")["total_tokens"] == 150
    assert db.usage_totals("day")["avg_latency_ms"] == 10

    overview = db.overview()
    assert overview["all_time"]["requests"] == 5
    assert overview["all_time"]["total_tokens"] == 150
    assert overview["all_time"]["avg_latency_ms"] == 10
    assert db.heatmap(weeks=2)["total_tokens"] == 150


def test_purge_keeps_ledger_totals():
    db = make_db()
    key = db.get_api_key_by_token(db.create_api_key("k"))
    _record(db, key["id"], total=42)

    assert db.purge_requests() == 1
    assert db.count_requests() == 0

    listed = [row for row in db.list_api_keys() if row["id"] == key["id"]][0]
    assert listed["request_count"] == 1
    assert listed["token_count"] == 42
    assert db.usage_totals("day")["total_tokens"] == 42


def test_backfill_seeds_ledger_once(tmp_path):
    from midware.db import Database

    path = tmp_path / "ledger.db"
    db = Database(str(path))
    db.init_schema()
    key = db.get_api_key_by_token(db.create_api_key("k"))
    _record(db, key["id"], total=30)
    db.prune_requests(key["id"])
    conn = db.conn
    conn.execute("DELETE FROM usage_daily")
    conn.commit()

    reopened = Database(str(path))
    reopened.init_schema()
    assert reopened.usage_totals("day")["total_tokens"] == 30

    reopened.init_schema()
    assert reopened.usage_totals("day")["total_tokens"] == 30
    assert reopened.count_requests() == 1
