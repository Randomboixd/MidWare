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


# -- APIImageRequest ----------------------------------------------------------


def image_reply(url="https://cdn.example.com/img.png", usage=None):
    body = {"created": 1, "data": [{"url": url}]}
    if usage:
        body["usage"] = usage
    return StubResponse(content=json.dumps(body).encode())


def test_js_apiimagerequest_ask_returns_url(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            "async function run(m) {"
            '  const url = await new APIImageRequest().ask("a cat in a hat");'
            '  m.set("flag", url);'
            "}",
            model="img-model",
        ),
    )
    stub = install_stub(
        app, final_response(), posts=[image_reply(url="https://cdn.example.com/x.png"), chat_reply("ok")]
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    url, payload, headers, timeout = stub.posts[0]
    assert url == "https://api.example.com/images/generations"
    assert payload["model"] == "img-model"
    assert payload["prompt"] == "a cat in a hat"
    assert headers["Authorization"] == "Bearer sk-upstream"
    assert stub.posts[1][1]["messages"][-1]["content"] == (
        "value=https://cdn.example.com/x.png"
    )


def test_js_apiimagerequest_create_folds_usage(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            "async function run(m) {"
            "  const r = await new APIImageRequest().create("
            '{prompt: "a dog", n: 2, size: "512x512"});'
            '  m.set("flag", r.data[0].url);'
            "}",
            model="img-model",
        ),
    )
    stub = install_stub(
        app,
        final_response(),
        posts=[image_reply(usage={"input_tokens": 3, "output_tokens": 4}), chat_reply("ok")],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    payload = stub.posts[0][1]
    assert payload["model"] == "img-model"
    assert payload["prompt"] == "a dog"
    assert payload["n"] == 2
    assert payload["size"] == "512x512"
    # Image usage folds into the single row (final 10/20 + image 3/4).
    row = db.list_requests(limit=1)[0]
    assert row["prompt_tokens"] == 13
    assert row["completion_tokens"] == 24
    assert row["total_tokens"] == 37


def test_js_apiimagerequest_requires_model(configured):
    # Premodels are chat-only, so the premodel's model is never an image
    # fallback: with no node model and no per-call model the call must fail.
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow('async function run(m) { await new APIImageRequest().create({prompt: "x"}); }'),
    )
    stub = install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "No model set" in response.get_json()["error"]["message"]
    assert not stub.posts


def test_js_apiimagerequest_requires_prompt(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await new APIImageRequest().create({model: "m1"}); }'
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
    assert "non-empty prompt" in response.get_json()["error"]["message"]
    assert not stub.posts


def test_js_apiimagerequest_without_connection_errors(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow('async function run(m) { await new APIImageRequest().ask("a cat"); }'),
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


def test_js_apiimagerequest_upstream_error_is_502(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await new APIImageRequest().ask("a cat"); }',
            model="img-model",
        ),
    )
    stub = install_stub(
        app,
        final_response(),
        posts=[StubResponse(status_code=400, content=b'{"error": {"message": "bad"}}')],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "workflow_node_failed"
    assert "an HTTP error" in response.get_json()["error"]["message"]


def test_js_midware_has_no_image_method(configured):
    # The image bridge is reachable only through the APIImageRequest class,
    # never as a bare midware.image() call.
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow('function run(m) { m.set("flag", typeof m.image); }'),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[0][1]["messages"][-1]["content"] == "value=undefined"


# -- regression: sandbox surface, validation, usage, sanitization -----------


def test_js_vars_includes_own_sets(configured):
    # midware.vars merges earlier nodes' vars with this node's set()s so far.
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow('function run(m) { m.set("a", "own"); m.set("flag", String(m.vars.a)); }'),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[0][1]["messages"][-1]["content"] == "value=own"


def test_js_console_log_circular_is_safe(configured):
    # console.log of a circular object must not fail the node.
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'function run(m) { const o = {a: 1}; o.self = o; console.log("circ", o); m.set("flag", "ok"); }'
        ),
    )
    install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200


def test_js_chat_passes_response_format_and_tools(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) {'
            " await m.chat({messages: [{role: 'user', content: 'x'}],"
            " response_format: {type: 'json_object'},"
            " tools: [{type: 'function', function: {name: 'f'}}],"
            " tool_choice: 'auto',"
            " max_completion_tokens: 42});"
            "}"
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    payload = stub.posts[0][1]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert payload["tool_choice"] == "auto"
    assert payload["max_completion_tokens"] == 42


def test_js_chat_param_whitelist_strips_unknown(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) {'
            " await m.chat({messages: [{role: 'user', content: 'x'}],"
            " stream: true, bogus_key: 1, temperature: 0.5});"
            "}"
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    payload = stub.posts[0][1]
    assert payload["stream"] is False
    assert "bogus_key" not in payload
    assert payload["temperature"] == 0.5


def test_js_chat_multiple_calls_fold_usage(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) {'
            " await m.chat({messages: [{role: 'user', content: 'a'}]});"
            " await m.chat({messages: [{role: 'user', content: 'b'}]});"
            "}"
        ),
    )
    stub = install_stub(
        app,
        final_response(),
        posts=[
            chat_reply("1", usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}),
            chat_reply("2", usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}),
            chat_reply("ok"),
        ],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    row = db.list_requests(limit=1)[0]
    # Final 10/20 + chat 5/7 + chat 3/4.
    assert row["prompt_tokens"] == 18
    assert row["completion_tokens"] == 31
    assert row["total_tokens"] == 49


def test_js_chat_rejects_non_string_model(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await m.chat({messages: [{role: "user", content: "x"}], model: {a: 1}}); }'
        ),
    )
    install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "model must be a string" in response.get_json()["error"]["message"]


def test_js_chat_rejects_message_without_role(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await m.chat({messages: [{content: "x"}]}); }'
        ),
    )
    install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "string role" in response.get_json()["error"]["message"]


def test_js_chat_rejects_non_dict_response(configured):
    # A list/number chat response is refused instead of silently becoming "".
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await m.chat({messages: [{role: "user", content: "x"}]}); }'
        ),
    )
    stub = install_stub(app, final_response(), posts=[StubResponse(json_body=[1, 2, 3])])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "unexpected JSON shape" in response.get_json()["error"]["message"]
    assert stub.sent == []


def test_js_chat_http_error_scrubs_status_for_client(configured):
    # The client-facing message hides the upstream HTTP status; the request
    # row keeps the full detail for the admin.
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) { await m.chat({messages: [{role: "user", content: "x"}]}); }'
        ),
    )
    install_stub(app, final_response(), posts=[StubResponse(status_code=401)])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    message = response.get_json()["error"]["message"]
    assert "an HTTP error" in message
    assert "401" not in message
    row = db.list_requests(limit=1)[0]
    assert "401" in row["error"]


def test_js_partial_usage_settles_rate_limit_on_failure(configured):
    # Sub-call tokens already spent settle the rate counter even though the
    # request errored (the row itself stays at zero tokens).
    app, token = configured
    db = app.extensions["midware_db"]
    premodel_id = create_workflow_premodel(
        db,
        js_workflow(
            'async function run(m) {'
            ' await m.chat({messages: [{role: "user", content: "x"}]});'
            ' throw new Error("boom");'
            "}"
        ),
        limit_tokens_enabled=True,
        limit_tokens_hour=True,
        max_tokens_hour=100,
    )
    install_stub(
        app,
        final_response(),
        posts=[chat_reply("ok", usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12})],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert db.premodel_rate_usage_amounts(premodel_id, "tokens", ("hour",))["hour"] == 12
    row = db.list_requests(limit=1)[0]
    assert row["total_tokens"] == 0


def test_js_set_messages_republishes_core_context(configured):
    # A later node's {{ core_context }} sees the rewritten conversation.
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {
                    "type": "javascript",
                    "name": "js",
                    "prompt": "function run(m) { m.setMessages(m.messages.slice(-1)); }",
                },
                {"type": "api_request", "name": "note", "prompt": "{{ core_context }}"},
            ]
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
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
    later_prompt = stub.posts[0][1]["messages"][-1]["content"]
    assert "second" in later_prompt
    assert "first" not in later_prompt


def test_js_memory_limit_trips(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        js_workflow(
            "function run(m) { const a = []; while (true) { a.push(new Array(100000).fill(1)); } }",
            timeout_seconds=30,
        ),
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "workflow_node_failed"


def test_js_sandbox_globals_match_probe(configured):
    # Locks the present/absent global lists the editor advertises against a
    # live mini-racer probe, so a bump cannot silently invalidate them.
    present = [
        "Math", "JSON", "Object", "Array", "String", "Number", "Boolean", "Date",
        "RegExp", "Promise", "Map", "Set", "WeakMap", "WeakSet", "Symbol", "BigInt",
        "Error", "TypeError", "RangeError", "SyntaxError", "ReferenceError",
        "EvalError", "URIError", "AggregateError",
        "Intl", "Proxy", "Reflect", "WeakRef", "FinalizationRegistry",
        "ArrayBuffer", "SharedArrayBuffer", "DataView",
        "Int8Array", "Uint8Array", "Uint8ClampedArray",
        "Int16Array", "Uint16Array", "Int32Array", "Uint32Array",
        "Float32Array", "Float64Array", "BigInt64Array", "BigUint64Array",
        "isNaN", "isFinite", "parseInt", "parseFloat",
        "encodeURIComponent", "decodeURIComponent", "escape", "unescape",
        "setTimeout", "clearTimeout",
    ]
    absent = [
        "URL", "URLSearchParams", "TextEncoder", "TextDecoder",
        "atob", "btoa", "structuredClone", "crypto",
        "setInterval", "clearInterval", "queueMicrotask",
        "AbortController", "AbortSignal", "fetch",
    ]
    checks = " ".join("r.%s = typeof %s;" % (name, name) for name in present + absent)
    script = (
        "function run(m) {"
        " const r = {};"
        + checks
        + ' m.set("flag", JSON.stringify(r));'
        "}"
    )
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, js_workflow(script))
    stub = install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    # The follow node ("note") carries the flag in its prompt; the final forward
    # only splices the (empty) core context.
    note_body = stub.posts[0][1]
    flag = json.loads(note_body["messages"][-1]["content"].split("value=", 1)[1])
    for name in present:
        assert flag[name] != "undefined", f"{name} should exist in the sandbox"
    for name in absent:
        assert flag[name] == "undefined", f"{name} should NOT exist in the sandbox"
