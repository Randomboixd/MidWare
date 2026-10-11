"""JavaScript node tests; skipped entirely when the optional mini-racer engine
is not installed."""

from __future__ import annotations

import json
import sys

import pytest

pytest.importorskip("py_mini_racer")

from midware.workflow import WorkflowError, WorkflowNodeError, normalize_workflow
from test_proxy import StubResponse, auth_header, install_stub
from test_workflows import (
    chat_reply,
    create_workflow_premodel,
    final_response,
    simple_workflow,
)


def js_workflow(script, follow_prompt="value={{ js.flag }}", **node_extra):
    node = {"type": "javascript", "name": "js", "prompt": script}
    node.update(node_extra)
    nodes = [node]
    if follow_prompt is not None:
        nodes.append(
            {"type": "api_request", "name": "note", "prompt": follow_prompt}
        )
    return simple_workflow(nodes=nodes)


# -- normalize ---------------------------------------------------------------


def test_js_normalize_requires_script():
    with pytest.raises(WorkflowError, match="run\\(midware\\)"):
        normalize_workflow(
            simple_workflow(nodes=[{"type": "javascript", "name": "js", "prompt": "  "}])
        )


def test_js_normalize_defaults_and_clamps():
    node = normalize_workflow(
        js_workflow("function run(m) { return 1; }")
    )["nodes"][0]
    assert node["allow_network"] is False
    assert node["timeout_seconds"] == 5

    clamped = normalize_workflow(
        js_workflow("function run(m) { return 1; }", timeout_seconds=99)
    )["nodes"][0]
    assert clamped["timeout_seconds"] == 30

    network = normalize_workflow(
        js_workflow("function run(m) { return 1; }", allow_network=True)
    )["nodes"][0]
    assert network["allow_network"] is True


def test_js_normalize_skips_template_scan():
    # {{ ... }} inside script strings must not trip the type checker.
    normalize_workflow(
        js_workflow(
            'function run(m) { m.set("p", "Hello {{ core_context }} and {{ nope.nope }}"); }'
        )
    )


def test_js_typecheck_lifts_published_paths():
    normalize_workflow(
        simple_workflow(
            nodes=[
                {"type": "javascript", "name": "js", "prompt": "function run(m){}"},
                {
                    "type": "api_request",
                    "name": "note",
                    "prompt": "{{ js.anything.deep }} {{ js.other }}",
                },
            ]
        )
    )


# -- runtime -----------------------------------------------------------------


def test_js_sets_variable_end_to_end(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow('function run(m) { m.set("flag", "set-by-js"); return "did it"; }'),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[0][1]["messages"][-1]["content"] == "value=set-by-js"


def test_js_integral_float_polish(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow('function run(m) { m.set("flag", 2); }'),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[0][1]["messages"][-1]["content"] == "value=2"


def test_js_set_messages_rewrites_final_splice(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            "function run(m) { m.setMessages(m.messages.slice(-1)); }",
            follow_prompt=None,
        ),
    )
    stub = install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "<p>-wf",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ],
        },
        headers=auth_header(token),
    )

    assert response.status_code == 200
    body = json.loads(stub.sent[-1][0]["content"])
    assert body["messages"] == [{"role": "assistant", "content": "second"}]


def test_js_sees_earlier_variables(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {"type": "context", "name": "ctx"},
                {
                    "type": "javascript",
                    "name": "js",
                    "prompt": (
                        "function run(m) {"
                        ' m.set("flag", "count=" + m.vars.ctx.count);'
                        "}"
                    ),
                },
                {"type": "api_request", "name": "note", "prompt": "{{ js.flag }}"},
            ]
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "<p>-wf",
            "messages": [{"role": "user", "content": "hey"}],
        },
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[0][1]["messages"][-1]["content"] == "count=1"


def test_js_throw_fails_workflow(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow('function run(m) { throw new Error("boom"); }'),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    error = response.get_json()["error"]
    assert error["code"] == "workflow_node_failed"
    assert "boom" in error["message"]
    assert not stub.posts
    assert not stub.sent


def test_js_missing_run_function(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow("const somethingElse = 1;"),
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "run is not defined" in response.get_json()["error"]["message"]


def test_js_infinite_loop_times_out(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow("function run(m) { while (true) {} }", timeout_seconds=1),
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "timed out" in response.get_json()["error"]["message"]


def test_js_fetch_denied_without_flag(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await m.fetch("http://example.test/"); }'
        ),
    )
    stub = install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "network is disabled" in response.get_json()["error"]["message"]
    assert not stub.requests


def test_js_fetch_allowed_uses_shared_client(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) {'
            ' const r = await m.fetch("http://example.test/api", { method: "POST", body: "x" });'
            ' m.set("flag", JSON.parse(r.body).answer);'
            "}",
            allow_network=True,
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    stub.http_response = StubResponse(
        content=b'{"answer": 42}', headers={"content-type": "application/json"}
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    method, url, content, headers, timeout = stub.requests[0]
    assert method == "POST"
    assert url == "http://example.test/api"
    assert content == "x"
    assert stub.posts[0][1]["messages"][-1]["content"] == "value=42"


def test_js_fetch_rejects_non_http_schemes(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await m.fetch("file:///etc/passwd"); }',
            allow_network=True,
        ),
    )
    stub = install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "http(s)" in response.get_json()["error"]["message"]
    assert not stub.requests


def test_js_reasoning_collects_log_and_return(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            reasoning=True,
            nodes=[
                {
                    "type": "javascript",
                    "name": "js",
                    "prompt": (
                        "function run(m) {"
                        ' console.log("hello log", m.messages.length);'
                        ' return "ret";'
                        "}"
                    ),
                }
            ],
        ),
    )
    install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    reasoning = response.get_json()["choices"][0]["message"]["reasoning_content"]
    assert "Started constructing node: js" in reasoning
    assert "hello log 1" in reasoning
    assert "Returned: ret" in reasoning


def test_js_missing_engine_reports_node_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "py_mini_racer", None)
    from midware import workflow as workflow_module

    with pytest.raises(WorkflowNodeError, match="mini-racer"):
        workflow_module._run_javascript(
            {
                "name": "js",
                "prompt": "function run(m) {}",
                "allow_network": False,
                "timeout_seconds": 5,
            },
            workflow_module.WorkflowScope(),
            [],
            {"method": "POST", "path": "/v1/x", "headers": {}},
            None,
            None,
            {"model": ""},
            {"prompt": 0, "completion": 0},
        )


# -- midware.chat / APIRequest ----------------------------------------------


def test_js_normalize_allows_route_and_model():
    node = normalize_workflow(
        js_workflow("function run(m) {}", route_id=3, model="glm-x")
    )["nodes"][0]
    assert node["route_id"] == 3
    assert node["model"] == "glm-x"


def test_js_midware_chat_calls_model_and_folds_usage(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) {'
            ' const r = await m.chat({messages: [{role: "user", content: "ping"}], model: "sub-model"});'
            ' m.set("flag", r.choices[0].message.content);'
            "}"
        ),
    )
    stub = install_stub(
        app,
        final_response(),
        posts=[
            chat_reply(
                "pong", usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
            ),
            chat_reply("ok"),
        ],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    url, sub_payload, sub_headers, timeout = stub.posts[0]
    assert url == "https://api.example.com/chat/completions"
    assert sub_payload["model"] == "sub-model"
    assert sub_payload["stream"] is False
    assert sub_payload["messages"] == [{"role": "user", "content": "ping"}]
    assert sub_headers["Authorization"] == "Bearer sk-upstream"
    # The follow-up note node publishes the chat answer.
    assert stub.posts[1][1]["messages"][-1]["content"] == "value=pong"
    # Chat usage folds into the single request row (final 10/20 + chat 5/7).
    row = db.list_requests(limit=1)[0]
    assert row["prompt_tokens"] == 15
    assert row["completion_tokens"] == 27
    assert row["total_tokens"] == 42


def test_js_apirequest_subclass(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            "class GLMRequest extends APIRequest {"
            "  constructor() {"
            "    super();"
            '    this.model = "xiaomi/glm-5.3:thinking";'
            "  }"
            "}"
            "async function run(m) {"
            "  const glm = new GLMRequest();"
            '  const answer = await glm.ask("Say hi");'
            '  m.set("flag", answer);'
            "}"
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("hi there"), chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[0][1]["model"] == "xiaomi/glm-5.3:thinking"
    assert stub.posts[0][1]["messages"] == [{"role": "user", "content": "Say hi"}]
    assert stub.posts[1][1]["messages"][-1]["content"] == "value=hi there"


def test_js_apirequest_create_passes_sampling_params(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            "class R extends APIRequest {"
            "  constructor() { super(); this.model = 'm1'; this.temperature = 0.2; }"
            "}"
            "async function run(m) {"
            "  const r = await new R().create({messages: [{role: 'user', content: 'x'}], top_p: 0.9});"
            '  m.set("flag", r.choices[0].message.content);'
            "}"
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok"), chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    payload = stub.posts[0][1]
    assert payload["model"] == "m1"
    assert payload["temperature"] == 0.2
    assert payload["top_p"] == 0.9


def test_js_chat_model_defaults_to_node_model(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) {'
            ' const r = await m.chat({messages: [{role: "user", content: "ping"}]});'
            ' m.set("flag", r.choices[0].message.content);'
            "}",
            model="node-default-model",
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("pong"), chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[0][1]["model"] == "node-default-model"


def test_js_chat_without_connection_errors(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await m.chat({messages: [{role: "user", content: "hi"}]}); }'
        ),
        route_id=None,
    )
    stub = install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "no connection" in response.get_json()["error"]["message"]
    assert not stub.posts
