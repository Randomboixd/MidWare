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
