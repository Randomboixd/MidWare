"""Regressions for the 0.1.10 models/MSQ fixes.

Each test locks one verified bug: a catalogue wiped by an unrecognized 200, a
refresh thread per request while an upstream is down, unbounded ``msq_checks``
growth, one recorder's crash aborting the whole tick, and a healthy stream
whose final SSE frame has no trailing separator being scored as truncated.
"""

from __future__ import annotations

import json
import sqlite3

from midware import models as models_module
from midware.models import refresh_all, refresh_if_stale
from midware.msq import run_due, run_recorder
from midware.msq_checks import StreamAudit, stream_integrity_findings

from test_models import models_body
from test_msq import make_recorder, sse_chunks
from test_proxy import StubResponse, install_stub


# -- fix 1: a 200 that is not a model list must not wipe the catalogue -------


def test_refresh_with_unrecognized_200_keeps_existing_catalogue(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    install_stub(app, StubResponse(status_code=200, content=models_body(["keep-me"])))
    refresh_all(app)
    assert db.count_models() == 1

    # A WAF error page / an {"object": "list"} without "data": a 200 whose body
    # carries no recognizable model list. Replacing the catalogue with it would
    # leave the route empty (and 403 every key restricted to it) for a day.
    install_stub(app, StubResponse(status_code=200, content=b'{"error":"blocked by policy"}'))
    result = refresh_all(app)

    assert result["errors"]
    assert {m["model_id"] for m in db.list_models()} == {"keep-me"}
    assert "empty model list" in (db.get_meta("models_refresh_error") or "")


def test_refresh_accepts_empty_list_for_route_without_models(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    install_stub(app, StubResponse(status_code=200, content=b"{}"))

    result = refresh_all(app)

    assert not result["errors"]
    assert db.count_models() == 0
    assert not db.get_meta("models_refresh_error")
    assert db.get_meta("models_refreshed_at")


# -- fix 2: one refresh at a time, and interval-based retries on failure -----


class FakeRefreshThread:
    """Captures the refresh worker without starting it, so the test can hold a
    claim 'in flight' and then let the worker finish at a chosen moment."""

    instances: list["FakeRefreshThread"] = []

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        type(self).instances.append(self)

    def start(self):
        pass

    def run_worker(self):
        assert self.target is not None
        self.target(*self.args)


def test_failing_refresh_runs_once_and_retries_by_interval(app, monkeypatch):
    db = app.extensions["midware_db"]
    db.create_route(name="R", target_host="https://up.example.com", upstream_key="k")
    install_stub(app, StubResponse(status_code=503, content=b"{}"))

    FakeRefreshThread.instances = []
    monkeypatch.setattr(models_module.threading, "Thread", FakeRefreshThread)
    spawns = []
    real_refresh_all = models_module.refresh_all

    def counting_refresh(app_):
        spawns.append(1)
        return real_refresh_all(app_)

    monkeypatch.setattr(models_module, "refresh_all", counting_refresh)

    # First request: nothing refreshed yet, so exactly one worker is claimed.
    assert refresh_if_stale(app) is True
    # While that worker is in flight (and until the marker is stamped), no
    # other request may spawn another refresh.
    for _ in range(5):
        assert refresh_if_stale(app) is False
    assert len(FakeRefreshThread.instances) == 1
    assert spawns == []

    FakeRefreshThread.instances[0].run_worker()

    # The failed run latched both the marker and the error: retries now wait
    # for the interval instead of one thread per request.
    assert len(spawns) == 1
    assert db.get_meta("models_refresh_error")
    assert db.get_meta("models_refreshed_at")
    for _ in range(5):
        assert refresh_if_stale(app) is False
    assert len(FakeRefreshThread.instances) == 1

    # The worker released its claim, so a stale catalogue can refresh again.
    db.set_meta("models_refreshed_at", "2020-01-01T00:00:00+00:00")
    assert refresh_if_stale(app) is True
    assert len(FakeRefreshThread.instances) == 2


# -- fix 3: per-recorder msq_checks history is bounded -----------------------


def test_msq_check_history_pruned_per_recorder(db):
    one = db.create_msq_recorder(name="One", route_id=None, model="m")
    two = db.create_msq_recorder(name="Two", route_id=None, model="m")

    for index in range(8):
        db.record_msq_check(recorder_id=one, ok=True, score=float(index), history_limit=5)
    for index in range(3):
        db.record_msq_check(recorder_id=two, ok=True, score=float(index), history_limit=5)

    kept = db.list_msq_checks(one)
    assert [row["id"] for row in kept] == [4, 5, 6, 7, 8]  # ids 1-3 trimmed
    assert [row["score"] for row in kept] == [3.0, 4.0, 5.0, 6.0, 7.0]
    assert len(db.list_msq_checks(two)) == 3  # other recorders untouched


def test_run_recorder_prunes_history_to_configured_limit(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    app.config["MSQ_HISTORY_LIMIT"] = 3
    recorder_id = make_recorder(db, db.active_route()["id"])
    install_stub(app, StubResponse(stream_chunks=sse_chunks("hi")))

    for _ in range(5):
        run_recorder(app, db.get_msq_recorder(recorder_id))

    assert len(db.list_msq_checks(recorder_id)) == 3
    assert db.get_msq_recorder(recorder_id)["next_run_at"]  # each run rescheduled


# -- fix 4: one broken recorder must not abort the whole tick ----------------


def test_run_due_isolates_a_recorder_that_blows_up(configured, monkeypatch):
    app, _ = configured
    db = app.extensions["midware_db"]
    route_id = db.active_route()["id"]
    first = make_recorder(db, route_id, name="First")
    second = make_recorder(db, route_id, name="Second")
    install_stub(app, StubResponse(stream_chunks=sse_chunks("hi")))

    real_record = db.record_msq_check

    def exploding(**kwargs):
        if kwargs["recorder_id"] == first:
            raise sqlite3.OperationalError("database is locked")
        return real_record(**kwargs)

    monkeypatch.setattr(db, "record_msq_check", exploding)

    results = run_due(app)

    assert [result["recorder_id"] for result in results] == [second]
    assert db.latest_msq_check(second)["ok"] == 1
    assert db.latest_msq_check(first) is None


# -- fix 5: a stream's last frame is scored even without a separator --------


def test_stream_audit_finish_flushes_trailing_unseparated_event():
    separated = StreamAudit()
    separated.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    separated.feed(b"data: [DONE]\n\n")
    expected, expected_hard = stream_integrity_findings(separated)
    assert expected_hard is False

    bare = StreamAudit()
    bare.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    bare.feed(b"data: [DONE]")
    bare.finish()

    findings, hard = stream_integrity_findings(bare)
    assert hard is False
    assert [f["reason"] for f in findings] == [f["reason"] for f in expected]


def test_recorder_stream_without_trailing_separator_scores_clean(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(
        db, db.active_route()["id"], hardcore_json=json.dumps({"stream_integrity": True})
    )
    install_stub(
        app,
        StubResponse(
            stream_chunks=[
                b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
                b"data: [DONE]",
            ]
        ),
    )

    result = run_recorder(app, db.get_msq_recorder(recorder_id))

    assert result["ok"] is True
    assert result["hard_fail"] is False
    assert result["score"] == 100.0
