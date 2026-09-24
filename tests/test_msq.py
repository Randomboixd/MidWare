from __future__ import annotations

import base64
import json
import re
from datetime import timedelta

from midware import adminauth
from midware.db import utcnow
from midware.msq import (
    DEGRADED,
    EXAMINING,
    FAILING,
    WORKING,
    chart_point,
    evaluate_recorder,
    run_due,
    run_recorder,
    score_check,
    start_scheduler,
)
from midware.msq_checks import (
    StreamAudit,
    build_needle_prompt,
    needle_findings,
    stream_integrity_findings,
    text_forensics_findings,
)


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def sse_chunks(*texts: str) -> list[bytes]:
    chunks = [
        ("data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n").encode()
        for text in texts
    ]
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def make_recorder(db, route_id, **overrides):
    fields = {
        "name": "Tester",
        "route_id": route_id,
        "model": "gpt-4o",
        "system_prompt": "Be brief.",
        "user_prompt": "Write a poem.",
    }
    fields.update(overrides)
    return db.create_msq_recorder(**fields)


def test_score_check_flags_slow_first_token():
    score, penalties = score_check(
        ok=True,
        ttft_ms=60_000,
        total_ms=1_000,
        text="fine",
        penalize_symbols=False,
        max_ttft_ms=30_000,
        max_total_ms=60_000,
    )
    assert score == 60.0
    assert len(penalties) == 1
    assert "First token" in penalties[0]["reason"]


def test_score_check_flags_symbols_and_slop():
    score, penalties = score_check(
        ok=True,
        ttft_ms=10,
        total_ms=10,
        text="Let us delve into 你好 the tapestry.",
        penalize_symbols=True,
        slop_list="delve, tapestry",
        max_ttft_ms=-1,
        max_total_ms=-1,
    )
    assert score < 100
    reasons = " ".join(p["reason"] for p in penalties)
    assert "Non-Latin" in reasons
    assert "delve" in reasons and "tapestry" in reasons


def test_score_check_failure_is_zero():
    score, penalties = score_check(ok=False, status_code=500, error="HTTP 500")
    assert score == 0.0
    assert penalties[0]["points"] == 100.0


def test_run_recorder_success(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    route_id = db.active_route()["id"]
    recorder_id = make_recorder(db, route_id)
    stub = install_stub(app, StubResponse(stream_chunks=sse_chunks("Hello", " world")))

    result = run_recorder(app, db.get_msq_recorder(recorder_id))

    assert result["ok"] is True
    assert result["status_code"] == 200
    check = db.latest_msq_check(recorder_id)
    assert check["ok"] == 1
    assert check["score"] == 100.0
    assert "Hello" in check["response_excerpt"] and "world" in check["response_excerpt"]
    assert check["ttft_ms"] is not None

    upstream_request, stream = stub.sent[0]
    assert stream is True
    assert upstream_request["url"].endswith("/v1/chat/completions")
    body = json.loads(upstream_request["content"])
    assert body["model"] == "gpt-4o"
    assert body["stream"] is True
    assert body["messages"][0]["role"] == "system"


def test_run_recorder_records_upstream_error(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, db.active_route()["id"])
    install_stub(app, StubResponse(status_code=500, content=b'{"error":"boom"}'))

    result = run_recorder(app, db.get_msq_recorder(recorder_id))

    assert result["ok"] is False
    check = db.latest_msq_check(recorder_id)
    assert check["ok"] == 0
    assert check["score"] == 0.0
    assert "HTTP 500" in check["error"]


def test_run_recorder_empty_response_is_a_failure(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, db.active_route()["id"])
    chunks = [b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n', b"data: [DONE]\n\n"]
    install_stub(app, StubResponse(stream_chunks=chunks))

    result = run_recorder(app, db.get_msq_recorder(recorder_id))

    assert result["ok"] is False
    assert "empty" in (db.latest_msq_check(recorder_id)["error"] or "").lower()


def test_run_recorder_penalizes_bad_answer(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, db.active_route()["id"], slop_list="delve")
    install_stub(app, StubResponse(stream_chunks=sse_chunks("Let us delve into 你好.")))

    result = run_recorder(app, db.get_msq_recorder(recorder_id))

    assert result["ok"] is True
    assert result["score"] < 100
    point = chart_point(db.latest_msq_check(recorder_id), 0)
    reasons = " ".join(p["reason"] for p in point["penalties"])
    assert "Non-Latin" in reasons and "delve" in reasons


def test_run_recorder_without_route_is_a_failure(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, None)
    result = run_recorder(app, db.get_msq_recorder(recorder_id))
    assert result["ok"] is False
    assert "route" in (result["error"] or "").lower()


def test_evaluate_recorder_transitions():
    now = utcnow()
    old = (now - timedelta(hours=25)).isoformat()
    fresh = now.isoformat()

    def checks(*scores):
        return [{"created_at": fresh, "score": score} for score in scores]

    degraded = evaluate_recorder({"created_at": old}, checks(20, 30, 40), now=now)
    assert degraded["status"] == DEGRADED

    working = evaluate_recorder({"created_at": old}, checks(95, 90, 85), now=now)
    assert working["status"] == WORKING

    too_young = evaluate_recorder({"created_at": fresh}, checks(95, 90, 85), now=now)
    assert too_young["status"] == EXAMINING

    too_few = evaluate_recorder({"created_at": old}, checks(95, 90), now=now)
    assert too_few["status"] == EXAMINING

    empty = evaluate_recorder({"created_at": old}, [], now=now)
    assert empty["status"] == EXAMINING


def test_run_due_claims_recorder_once(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, db.active_route()["id"])
    install_stub(app, StubResponse(stream_chunks=sse_chunks("hi")))

    assert len(run_due(app)) == 1
    assert db.latest_msq_check(recorder_id) is not None

    assert run_due(app) == []


def test_run_due_skips_disabled(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    make_recorder(db, db.active_route()["id"], enabled=False)
    assert run_due(app) == []


def test_scheduler_is_disabled_under_tests(app):
    assert start_scheduler(app) is None


def test_msq_pages_render(configured_client):
    client, _ = configured_client
    assert client.get("/admin/msq/").status_code == 200
    assert client.get("/admin/msq/new").status_code == 200


def test_msq_create_and_delete(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]

    response = client.post(
        "/admin/msq/new",
        data={
            "name": "Nightly",
            "route_id": str(route_id),
            "model": "gpt-4o",
            "system_prompt": "Be brief.",
            "user_prompt": "Write a poem.",
            "interval_minutes": "30",
            "enabled": "1",
            "penalize_symbols": "1",
            "max_ttft_seconds": "20",
            "max_total_seconds": "45",
        },
    )
    assert response.status_code == 302
    assert db.count_msq_recorders() == 1
    recorder = db.list_msq_recorders()[0]
    assert recorder["name"] == "Nightly"
    assert recorder["interval_seconds"] == 1800
    assert recorder["max_ttft_ms"] == 20_000

    html = client.get("/admin/msq/").get_data(as_text=True)
    assert "Nightly" in html

    client.post(f"/admin/msq/{recorder['id']}/delete")
    assert db.count_msq_recorders() == 0


def test_msq_form_respects_unchecked_switches(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]

    client.post(
        "/admin/msq/new",
        data={
            "name": "Paused",
            "route_id": str(route_id),
            "model": "gpt-4o",
            "interval_minutes": "60",
            "enabled": "0",
            "penalize_symbols": "0",
            "max_ttft_seconds": "-1",
            "max_total_seconds": "-1",
        },
    )

    recorder = db.list_msq_recorders()[0]
    assert recorder["enabled"] == 0
    assert recorder["penalize_symbols"] == 0
    assert recorder["max_ttft_ms"] == -1
    assert recorder["max_total_ms"] == -1


def test_msq_form_requires_fields(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    response = client.post("/admin/msq/new", data={"name": "", "route_id": "", "model": ""})
    assert response.status_code == 200
    assert db.count_msq_recorders() == 0


def test_msq_run_now_records_a_check(configured_client):
    from test_proxy import StubResponse, install_stub

    client, _ = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, db.active_route()["id"])
    install_stub(app, StubResponse(stream_chunks=sse_chunks("hello")))

    response = client.post(f"/admin/msq/{recorder_id}/run", follow_redirects=True)
    assert response.status_code == 200
    assert db.latest_msq_check(recorder_id) is not None


def test_msq_requires_login(secured_app):
    db = secured_app.extensions["midware_db"]
    db.create_route(name="Default", target_host="https://api.example.com", upstream_key="sk")
    db.mark_configured()
    with secured_app.app_context():
        adminauth.set_credentials("admin", "pw")
    client = secured_app.test_client()

    locked = client.get("/admin/msq/")
    assert locked.status_code == 401
    assert locked.headers["WWW-Authenticate"].startswith("Basic")
    assert client.get("/admin/msq/", headers=basic("admin", "pw")).status_code == 200


# -- hardcore checks ---------------------------------------------------------


class _MsqClient:
    """Handles the streamed probe and echoes (or misses) the needle code."""

    def __init__(self, needle_answer=None):
        self.needle_answer = needle_answer
        self.sent = []

    def build_request(self, method, url, content=None, headers=None):
        return {"method": method, "url": url, "content": content, "headers": headers}

    def send(self, request, stream=False):
        from test_proxy import StubResponse

        self.sent.append((request, stream))
        if stream:
            return StubResponse(stream_chunks=sse_chunks("hello world"))
        text = json.loads(request["content"])["messages"][-1]["content"]
        match = re.search(r"MW-[0-9A-F]{6}", text)
        code = match.group(0) if match else "MW-000000"
        answer = self.needle_answer if self.needle_answer is not None else code
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": answer}}]}).encode()
        return StubResponse(status_code=200, content=body)


def test_stream_audit_flags_malformed_and_truncation():
    audit = StreamAudit(stall_ms=1000)
    audit.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    audit.feed(b"data: {not json}\n\n", at=5.0)

    findings, hard = stream_integrity_findings(audit)
    assert hard is True
    reasons = " ".join(item["reason"] for item in findings)
    assert "malformed" in reasons
    assert "truncated" in reasons


def test_stream_audit_detects_terminal_and_finish_reason():
    audit = StreamAudit()
    audit.feed(b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n')
    _, hard = stream_integrity_findings(audit)
    assert hard is False

    clean = StreamAudit()
    clean.feed(b"data: [DONE]\n\n")
    findings, hard = stream_integrity_findings(clean)
    assert hard is False
    assert findings[0]["passed"] is True


def test_stream_audit_ignores_non_sse_body():
    audit = StreamAudit()
    audit.feed(b'{"choices":[{"message":{"content":"hi"}}]}')
    findings, hard = stream_integrity_findings(audit)
    assert hard is False
    assert findings[0]["passed"] is True


def test_stream_audit_detects_stalls():
    audit = StreamAudit(stall_ms=1000)
    audit.feed(b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n', at=0.0)
    audit.feed(b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n', at=5.0)
    audit.feed(b"data: [DONE]\n\n", at=5.0)

    findings, hard = stream_integrity_findings(audit)
    assert hard is False
    assert any("stall" in item["reason"] for item in findings)


def test_text_forensics_repetition_glitch_template():
    repetition = text_forensics_findings("alpha beta gamma delta epsilon zeta " * 5)
    assert any("Repeats" in item["reason"] for item in repetition)

    glitch = text_forensics_findings("broken \ufffd output \x01")
    reasons = " ".join(item["reason"] for item in glitch)
    assert "replacement" in reasons and "control" in reasons

    leaked = text_forensics_findings("sure <|im_start|>system")
    assert any("chat-template" in item["reason"] for item in leaked)

    clean = text_forensics_findings("A perfectly ordinary sentence about rivers.")
    assert clean[0]["passed"] is True


def test_needle_prompt_and_grading():
    code, prompt = build_needle_prompt(400)
    assert code in prompt
    assert len(prompt) > 400

    findings, hard = needle_findings(code, f"the code is {code}.")
    assert hard is False and findings[0]["passed"] is True

    findings, hard = needle_findings(code, "I have no idea.")
    assert hard is True and findings[0]["passed"] is False


def test_run_recorder_marks_failing_on_truncated_stream(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(
        db, db.active_route()["id"], hardcore_json=json.dumps({"stream_integrity": True})
    )
    install_stub(app, StubResponse(stream_chunks=[b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n']))

    result = run_recorder(app, db.get_msq_recorder(recorder_id))

    assert result["hard_fail"] is True
    check = db.latest_msq_check(recorder_id)
    assert check["hard_fail"] == 1
    assert check["score"] == 0.0
    assert evaluate_recorder(db.get_msq_recorder(recorder_id), [check])["status"] == FAILING


def test_run_recorder_text_forensics_penalize(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(
        db, db.active_route()["id"], hardcore_json=json.dumps({"text_forensics": True})
    )
    install_stub(
        app,
        StubResponse(stream_chunks=sse_chunks("alpha beta gamma delta epsilon zeta " * 5)),
    )

    result = run_recorder(app, db.get_msq_recorder(recorder_id))

    assert result["hard_fail"] is False
    assert result["score"] < 100
    point = chart_point(db.latest_msq_check(recorder_id), 0)
    assert any(item["kind"] == "repetition" for item in point["penalties"])


def test_run_recorder_needle_pass_and_miss(configured):
    app, _ = configured
    db = app.extensions["midware_db"]

    good_id = make_recorder(
        db, db.active_route()["id"], hardcore_json=json.dumps({"needle": True, "needle_context_chars": 300})
    )
    app.extensions["midware_http"] = _MsqClient()
    good = run_recorder(app, db.get_msq_recorder(good_id))
    assert good["ok"] is True and good["hard_fail"] is False

    miss_id = make_recorder(
        db, db.active_route()["id"], hardcore_json=json.dumps({"needle": True, "needle_context_chars": 300})
    )
    app.extensions["midware_http"] = _MsqClient(needle_answer="no idea")
    miss = run_recorder(app, db.get_msq_recorder(miss_id))
    assert miss["ok"] is False and miss["hard_fail"] is True


def test_evaluate_recorder_failing_on_latest_hard_fail():
    now = utcnow()
    fresh = now.isoformat()
    checks = [
        {"created_at": fresh, "score": 90, "hard_fail": 0},
        {"created_at": fresh, "score": 0, "hard_fail": 1},
    ]
    evaluated = evaluate_recorder({"created_at": fresh}, checks, now=now)
    assert evaluated["status"] == FAILING


def test_msq_page_renders_failing_and_hardcore(configured_client):
    from test_proxy import StubResponse, install_stub

    client, _ = configured_client
    app = client.application
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(
        db, db.active_route()["id"], hardcore_json=json.dumps({"stream_integrity": True})
    )
    install_stub(app, StubResponse(stream_chunks=[b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n']))
    client.post(f"/admin/msq/{recorder_id}/run")

    html = client.get("/admin/msq/").get_data(as_text=True)
    assert "Failing" in html
    assert "integrity" in html


def test_collect_requests_logs_probe(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    route_id = db.active_route()["id"]
    recorder_id = make_recorder(db, route_id, collect_requests=True)
    install_stub(app, StubResponse(stream_chunks=sse_chunks("hello world")))

    run_recorder(app, db.get_msq_recorder(recorder_id))

    assert db.count_requests() == 1
    row = db.list_requests(limit=1)[0]
    assert row["api_key_name"] == "MSQ Measurements"
    assert row["route_id"] == route_id
    assert row["model"] == "gpt-4o"
    assert row["streamed"] == 1
    assert row["total_tokens"] > 0
    # Named in history, but the collector key never shows up as a manageable key.
    assert "MSQ Measurements" not in [key["name"] for key in db.list_api_keys()]
    assert db.count_api_keys() == 1


def test_collect_requests_off_by_default(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, db.active_route()["id"])
    install_stub(app, StubResponse(stream_chunks=sse_chunks("hi")))

    run_recorder(app, db.get_msq_recorder(recorder_id))

    assert db.count_requests() == 0
    assert all(key["name"] != "MSQ Measurements" for key in db.list_api_keys())


def test_collect_requests_logs_http_error(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    recorder_id = make_recorder(db, db.active_route()["id"], collect_requests=True)
    install_stub(app, StubResponse(status_code=500, content=b'{"error":"boom"}'))

    run_recorder(app, db.get_msq_recorder(recorder_id))

    row = db.list_requests(limit=1)[0]
    assert row["status_code"] == 500
    assert row["total_tokens"] == 0
    assert row["token_source"] == "none"


def sse_reasoning_chunks(reasoning: str, content: str) -> list[bytes]:
    chunks = [
        ("data: " + json.dumps({"choices": [{"delta": {"reasoning_content": reasoning[i : i + 40]}}]}) + "\n\n").encode()
        for i in range(0, len(reasoning), 40)
    ]
    chunks.append(("data: " + json.dumps({"choices": [{"delta": {"content": content}}]}) + "\n\n").encode())
    chunks.append(b"data: [DONE]\n\n")
    return chunks


def test_ignore_thinking_excludes_reasoning_from_scoring(configured):
    from test_proxy import StubResponse, install_stub

    app, _ = configured
    db = app.extensions["midware_db"]
    reasoning = "alpha beta gamma delta epsilon zeta " * 5
    chunks = sse_reasoning_chunks(reasoning, "A clean poem about the sea.")
    forensics = json.dumps({"text_forensics": True})

    ignored_id = make_recorder(db, db.active_route()["id"], hardcore_json=forensics, ignore_thinking=True)
    install_stub(app, StubResponse(stream_chunks=chunks))
    ignored = run_recorder(app, db.get_msq_recorder(ignored_id))
    assert ignored["score"] == 100.0

    included_id = make_recorder(db, db.active_route()["id"], hardcore_json=forensics, ignore_thinking=False)
    install_stub(app, StubResponse(stream_chunks=chunks))
    included = run_recorder(app, db.get_msq_recorder(included_id))
    assert included["score"] < 100


def test_collect_needle_records_second_request(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    hardcore = json.dumps({"needle": True, "needle_context_chars": 300, "collect_needle": True})
    recorder_id = make_recorder(db, db.active_route()["id"], collect_requests=True, hardcore_json=hardcore)
    app.extensions["midware_http"] = _MsqClient()

    run_recorder(app, db.get_msq_recorder(recorder_id))

    rows = db.list_requests(limit=10)
    assert len(rows) == 2
    assert all(row["api_key_name"] == "MSQ Measurements" for row in rows)
    needle_rows = [row for row in rows if "vault access code" in (row["request_body"] or "")]
    assert len(needle_rows) == 1
    assert "MW-" in (needle_rows[0]["response_body"] or "")
    assert needle_rows[0]["streamed"] == 0


def test_collect_needle_off_records_only_main(configured):
    app, _ = configured
    db = app.extensions["midware_db"]
    hardcore = json.dumps({"needle": True, "needle_context_chars": 300})
    recorder_id = make_recorder(db, db.active_route()["id"], collect_requests=True, hardcore_json=hardcore)
    app.extensions["midware_http"] = _MsqClient()

    run_recorder(app, db.get_msq_recorder(recorder_id))

    assert db.count_requests() == 1


def test_msq_form_persists_collect_needle(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]

    client.post(
        "/admin/msq/new",
        data={
            "name": "Needler",
            "route_id": str(route_id),
            "model": "gpt-4o",
            "interval_minutes": "60",
            "collect_requests": "1",
            "hc_needle": "1",
            "hc_collect_needle": "1",
        },
    )

    recorder = db.list_msq_recorders()[0]
    assert json.loads(recorder["hardcore_json"])["collect_needle"] is True

    edit = client.get(f"/admin/msq/{recorder['id']}/edit").get_data(as_text=True)
    assert 'id="msq-collect-needle"' in edit
    script = client.get("/static/msq.js").get_data(as_text=True)
    assert "msq-collect-needle" in script


def test_msq_form_persists_collect_requests(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]

    client.post(
        "/admin/msq/new",
        data={
            "name": "Collected",
            "route_id": str(route_id),
            "model": "gpt-4o",
            "interval_minutes": "60",
            "collect_requests": "1",
        },
    )

    recorder = db.list_msq_recorders()[0]
    assert recorder["collect_requests"] == 1
    edit = client.get(f"/admin/msq/{recorder['id']}/edit").get_data(as_text=True)
    assert "Collect MSQ Requests" in edit


def test_msq_warning_banner_is_client_dismissible(configured_client):
    client, _ = configured_client

    html = client.get("/admin/msq/").get_data(as_text=True)
    assert 'id="msq-warning"' in html
    assert 'id="msq-warning-dismiss"' in html

    form = client.get("/admin/msq/new").get_data(as_text=True)
    assert 'id="msq-warning"' in form

    script = client.get("/static/msq.js").get_data(as_text=True)
    assert "midware.msq.warning.dismissed" in script
    assert "localStorage" in script


def test_msq_form_persists_hardcore_toggles(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]

    client.post(
        "/admin/msq/new",
        data={
            "name": "Hardcore",
            "route_id": str(route_id),
            "model": "gpt-4o",
            "interval_minutes": "60",
            "hc_stream_integrity": "1",
            "hc_needle": "1",
            "needle_context_chars": "800",
        },
    )

    recorder = db.list_msq_recorders()[0]
    hardcore = json.loads(recorder["hardcore_json"])
    assert hardcore["stream_integrity"] is True
    assert hardcore["needle"] is True
    assert hardcore["needle_context_chars"] == 800
    assert "text_forensics" not in hardcore

    edit = client.get(f"/admin/msq/{recorder['id']}/edit").get_data(as_text=True)
    assert "Hardcore checks" in edit
    assert "needle_context_chars" in edit
