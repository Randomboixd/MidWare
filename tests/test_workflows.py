from __future__ import annotations

import json
import re
import time

import httpx
import pytest

from midware.workflow import (
    WorkflowError,
    WorkflowScope,
    _assure_check,
    _extract_json,
    _format_timestamp,
    _render,
    _run_context,
    _run_environment,
    normalize_workflow,
)
from test_proxy import StubResponse, auth_header, install_stub

FINAL_BODY = {
    "id": "chatcmpl-final",
    "choices": [{"message": {"role": "assistant", "content": "Done."}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
}


def chat_reply(content, usage=None, reply_id="chatcmpl-sub"):
    body = {
        "id": reply_id,
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if usage:
        body["usage"] = usage
    return StubResponse(json_body=body)


def reasoning_reply(content, reasoning, usage=None):
    body = {
        "id": "chatcmpl-sub",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning,
                }
            }
        ],
    }
    if usage:
        body["usage"] = usage
    return StubResponse(json_body=body)


def final_response():
    return StubResponse(content=json.dumps(FINAL_BODY).encode())


def create_workflow_premodel(db, workflow, **overrides):
    fields = {
        "name": "Workflow",
        "slug": "wf",
        "route_id": db.active_route()["id"],
        "model": "final-model",
        "use_workflow": True,
        "workflow_json": json.dumps(workflow),
    }
    fields.update(overrides)
    return db.create_premodel(**fields)


def simple_workflow(nodes=None, messages=None, model_override="", reasoning=False):
    return {
        "version": 1,
        "reasoning": reasoning,
        "final": {
            "messages": messages if messages is not None else [{"kind": "core_context"}],
            "model_override": model_override,
        },
        "nodes": nodes or [],
    }


def _key_id(app):
    return app.extensions["midware_db"].list_api_keys()[0]["id"]


# -- normalize_workflow ------------------------------------------------------


def test_normalize_workflow_accepts_valid_chain():
    result = normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "api_request",
                    "name": "analyze",
                    "prompt": "Classify {{ core_context }}",
                    "expect_json": True,
                    "timeout_seconds": 30,
                    "max_tokens": 100,
                }
            ],
            messages=[
                {"kind": "text", "role": "system", "content": "Cat: {{ analyze.category }}"},
                {"kind": "core_context"},
            ],
        )
    )
    assert result["version"] == 1
    assert result["nodes"][0]["name"] == "analyze"
    assert result["nodes"][0]["timeout_seconds"] == 30
    assert result["final"]["messages"][0]["kind"] == "text"


@pytest.mark.parametrize(
    "blob, match",
    [
        ("", "empty"),
        ("not json", "valid JSON"),
        ("[]", "must be an object"),
        ({"final": {"messages": []}}, "at least one"),
        ({"final": {"messages": [{"kind": "mystery"}]}}, "unknown kind"),
        (
            {
                "final": {"messages": [{"kind": "text", "role": "narrator", "content": "x"}]}
            },
            "unknown role",
        ),
        (
            {
                "final": {"messages": [{"kind": "core_context"}]},
                "nodes": [{"type": "api_request", "name": "Bad Name", "prompt": "x"}],
            },
            "lowercase",
        ),
        (
            {
                "final": {"messages": [{"kind": "core_context"}]},
                "nodes": [
                    {"type": "api_request", "name": "dup", "prompt": "x"},
                    {"type": "api_request", "name": "dup", "prompt": "y"},
                ],
            },
            "Duplicate",
        ),
        (
            {
                "final": {"messages": [{"kind": "core_context"}]},
                "nodes": [{"type": "api_request", "name": "a", "prompt": "  "}],
            },
            "needs a prompt",
        ),
        (
            {
                "final": {"messages": [{"kind": "core_context"}]},
                "nodes": [{"type": "branch", "name": "a", "prompt": "x"}],
            },
            "unknown type",
        ),
        (
            {
                "final": {"messages": [{"kind": "core_context"}]},
                "nodes": [
                    {"type": "api_request", "name": f"n{i}", "prompt": "x"} for i in range(9)
                ],
            },
            "at most 8",
        ),
    ],
)
def test_normalize_workflow_rejects_invalid(blob, match):
    with pytest.raises(WorkflowError, match=match):
        normalize_workflow(blob)


# -- scope / rendering -------------------------------------------------------


def test_scope_dotted_lookup_and_unknown_literal():
    scope = WorkflowScope({"analyze": {"category": "tricks", "score": 3}})
    assert scope.lookup("analyze.category") == "tricks"
    assert scope.lookup("analyze.score") == "3"
    assert scope.lookup("analyze.missing") is None
    assert scope.lookup("nope") is None
    assert _render("Cat: {{ analyze.category }} / {{ missing }}", scope) == (
        "Cat: tricks / {{ missing }}"
    )


def test_extract_json_strips_code_fences():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json("nope") is None


# -- engine via the proxy ----------------------------------------------------


def test_workflow_end_to_end(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    second_id = db.create_route(
        name="Second", target_host="https://second.example.com", upstream_key="sk-second"
    )
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {
                    "type": "api_request",
                    "name": "analyze",
                    "route_id": second_id,
                    "model": "cheap-model",
                    "system_prompt": "You classify.",
                    "prompt": "Classify: {{ core_context }}\nHdrs: {{ system_request }}",
                    "expect_json": True,
                    "timeout_seconds": 30,
                }
            ],
            messages=[
                {"kind": "text", "role": "system", "content": "Category: {{ analyze.category }}"},
                {"kind": "core_context"},
            ],
        ),
    )
    stub = install_stub(
        app,
        final_response(),
        posts=[
            chat_reply(
                '{"category": "tricks", "note": "ok"}',
                usage={"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
            )
        ],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "Hello!"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    url, sub_payload, sub_headers, timeout = stub.posts[0]
    assert url == "https://second.example.com/chat/completions"
    assert sub_payload["model"] == "cheap-model"
    assert sub_payload["stream"] is False
    assert sub_headers["Authorization"] == "Bearer sk-second"
    assert timeout == 30.0
    assert sub_payload["messages"][0] == {"role": "system", "content": "You classify."}
    assert "Classify:" in sub_payload["messages"][1]["content"]
    assert "Hello!" in sub_payload["messages"][1]["content"]
    assert token not in sub_payload["messages"][1]["content"]

    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["model"] == "final-model"
    assert final_payload["messages"] == [
        {"role": "system", "content": "Category: tricks"},
        {"role": "user", "content": "Hello!"},
    ]

    row = db.list_requests(limit=1)[0]
    assert row["prompt_tokens"] == 17
    assert row["completion_tokens"] == 25
    assert row["total_tokens"] == 42


def test_workflow_origin_to_final_only_is_plain_forward(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, simple_workflow())
    stub = install_stub(app, final_response())
    client = app.test_client()
    messages = [{"role": "user", "content": "Hi"}]

    client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": messages},
        headers=auth_header(token),
    )

    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["messages"] == messages
    assert final_payload["model"] == "final-model"
    assert stub.posts == []


def test_workflow_model_override_and_sampling_params(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(model_override="override-model"),
        params_enabled=True,
        params_json=json.dumps({"temperature": 0.2}),
    )
    stub = install_stub(app, final_response())
    client = app.test_client()

    client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "Hi"}]},
        headers=auth_header(token),
    )

    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["model"] == "override-model"
    assert final_payload["temperature"] == 0.2


def test_workflow_ignores_classic_prompt_fields(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(messages=[{"kind": "core_context"}]),
        system_prompt="PRESET THAT MUST NOT LEAK",
        merge_mode="append",
        accept_mwvars=True,
    )
    stub = install_stub(app, final_response())
    client = app.test_client()

    client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "Hi"}]},
        headers=auth_header(token),
    )

    final_payload = json.loads(stub.sent[0][0]["content"])
    assert "PRESET THAT MUST NOT LEAK" not in json.dumps(final_payload)


def test_workflow_node_http_error_is_502(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[{"type": "api_request", "name": "analyze", "prompt": "Go"}]
        ),
    )
    stub = install_stub(app, final_response(), posts=[StubResponse(status_code=500, content=b"{}")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "workflow_node_failed"
    assert "analyze" in body["error"]["message"]
    assert stub.sent == []
    row = db.list_requests(limit=1)[0]
    assert row["status_code"] == 502
    assert "workflow[analyze]" in row["error"]


def test_workflow_expect_json_rejects_non_json(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {
                    "type": "api_request",
                    "name": "extract",
                    "prompt": "JSON please",
                    "expect_json": True,
                }
            ]
        ),
    )
    install_stub(app, final_response(), posts=[chat_reply("sorry, no")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "workflow_node_failed"


def test_workflow_node_timeout_is_502(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(nodes=[{"type": "api_request", "name": "slow", "prompt": "Go"}]),
    )
    install_stub(app, final_response(), posts=[httpx.ReadTimeout("too slow")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert "slow" in response.get_json()["error"]["message"]


def test_workflow_sub_calls_bypass_key_allowlists(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    default_id = db.active_route()["id"]
    second_id = db.create_route(
        name="Second", target_host="https://second.example.com", upstream_key="sk-second"
    )
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {
                    "type": "api_request",
                    "name": "sniffer",
                    "route_id": second_id,
                    "model": "other-model",
                    "prompt": "Go",
                }
            ]
        ),
    )
    db.update_api_key(
        _key_id(app),
        restrict_providers=True,
        allowed_route_ids=[default_id],
        restrict_models=True,
        allowed_model_ids=[f"{default_id}:final-model"],
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[0][0] == "https://second.example.com/chat/completions"
    assert len(stub.sent) == 1


def test_workflow_final_model_still_gated_by_allowlist(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    default_id = db.active_route()["id"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[{"type": "api_request", "name": "sniffer", "prompt": "Go"}]
        ),
    )
    db.update_api_key(
        _key_id(app),
        restrict_models=True,
        allowed_model_ids=[f"{default_id}:something-else"],
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "model_not_allowed"
    assert stub.posts == []
    assert stub.sent == []


def test_workflow_usage_folds_into_streamed_row(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(nodes=[{"type": "api_request", "name": "prep", "prompt": "Go"}]),
    )
    chunks = [
        b'data: {"id":"1","choices":[{"delta":{"content":"hey"}}]}\n\n',
        b'data: {"id":"1","usage":{"prompt_tokens":4,"completion_tokens":6,"total_tokens":10}}\n\n',
        b"data: [DONE]\n\n",
    ]
    install_stub(
        app,
        StubResponse(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            stream_chunks=chunks,
        ),
        posts=[
            chat_reply(
                "ok",
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            )
        ],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "stream": True, "messages": []},
        headers=auth_header(token),
    )
    b"".join(response.response)

    row = db.list_requests(limit=1)[0]
    assert row["streamed"] == 1
    assert row["prompt_tokens"] == 104
    assert row["completion_tokens"] == 56
    assert row["total_tokens"] == 160


def test_workflow_token_limit_settles_on_folded_total(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    premodel_id = create_workflow_premodel(
        db,
        simple_workflow(nodes=[{"type": "api_request", "name": "prep", "prompt": "Go"}]),
        limit_tokens_enabled=True,
        limit_tokens_hour=True,
        max_tokens_hour=40,
    )
    install_stub(
        app,
        final_response(),
        posts=[
            chat_reply(
                "ok",
                usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            )
        ],
    )
    client = app.test_client()
    payload = {"model": "<p>-wf", "messages": []}

    first = client.post("/v1/chat/completions", json=payload, headers=auth_header(token))
    second = client.post("/v1/chat/completions", json=payload, headers=auth_header(token))

    assert first.status_code == 200
    assert second.status_code == 429
    body = second.get_json()
    assert body["error"]["code"] == "token_limit_exceeded"
    assert db.premodel_rate_usage_amounts(premodel_id, "tokens", ("hour",))["hour"] == 42


def test_workflow_invalid_saved_blob_is_502(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, simple_workflow(), workflow_json="not json at all")
    install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    assert response.get_json()["error"]["code"] == "workflow_invalid"


def test_workflow_host_prefix_still_wins(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    second_id = db.create_route(
        name="Second", target_host="https://second.example.com", upstream_key="sk-second"
    )
    create_workflow_premodel(
        db,
        simple_workflow(nodes=[{"type": "api_request", "name": "prep", "prompt": "Go"}]),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf[Second]m2", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["model"] == "m2"
    assert stub.sent[0][0]["url"] == "https://second.example.com/chat/completions"
    assert db.list_requests(limit=1)[0]["host_prefix"] == "Second"
    assert db.get_route(second_id)["name"] == "Second"


def test_workflow_premodel_listed_in_models(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, simple_workflow())
    client = app.test_client()

    response = client.get("/v1/models", headers=auth_header(token))
    ids = [entry["id"] for entry in response.get_json()["data"]]
    assert "<p>-wf" in ids


# -- admin form --------------------------------------------------------------


def test_workflow_form_roundtrip(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]
    blob = simple_workflow(
        nodes=[{"type": "api_request", "name": "analyze", "prompt": "Classify {{ core_context }}"}],
        messages=[
            {"kind": "text", "role": "system", "content": "Cat: {{ analyze.content }}"},
            {"kind": "core_context"},
        ],
    )

    response = client.post(
        "/admin/premodels/new",
        data={
            "name": "Chain",
            "slug": "chain",
            "route_id": str(route_id),
            "model": "m",
            "use_workflow": "1",
            "workflow_json": json.dumps(blob),
        },
    )

    assert response.status_code == 302
    premodel = db.get_premodel_by_slug("chain")
    assert premodel["use_workflow"] == 1
    stored = json.loads(premodel["workflow_json"])
    assert stored["nodes"][0]["name"] == "analyze"
    assert stored["final"]["messages"][0]["kind"] == "text"

    edit = client.get(f"/admin/premodels/{premodel['id']}/edit")
    assert b"workflow-data" in edit.data
    assert b"Use Workflows" in edit.data


def test_workflow_form_rejects_invalid_blob(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]

    response = client.post(
        "/admin/premodels/new",
        data={
            "name": "Chain",
            "slug": "chain",
            "route_id": str(route_id),
            "model": "m",
            "use_workflow": "1",
            "workflow_json": "not json at all",
        },
    )

    assert response.status_code == 200
    assert b"not valid JSON" in response.data
    assert db.get_premodel_by_slug("chain") is None


def test_workflow_form_roundtrip_preserves_call_syntax(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    route_id = db.active_route()["id"]
    prompt = 'Clock: {{ env.time.formatted("MN:SE") }} / {{ env.time.unix }}'
    blob = simple_workflow(
        nodes=[
            {"type": "environment", "name": "env", "actions": ["time"]},
            {"type": "api_request", "name": "note", "prompt": prompt},
        ]
    )

    response = client.post(
        "/admin/premodels/new",
        data={
            "name": "Chain",
            "slug": "chain",
            "route_id": str(route_id),
            "model": "m",
            "use_workflow": "1",
            "workflow_json": json.dumps(blob),
        },
    )

    assert response.status_code == 302
    stored = json.loads(db.get_premodel_by_slug("chain")["workflow_json"])
    assert stored["nodes"][1]["prompt"] == prompt


def test_workflow_badge_on_list(configured_client):
    client, _ = configured_client
    db = client.application.extensions["midware_db"]
    create_workflow_premodel(db, simple_workflow(), slug="wf", name="WF")

    page = client.get("/admin/premodels")
    assert page.status_code == 200
    assert b">workflow<" in page.data


# -- image captioner ---------------------------------------------------------


def test_normalize_image_captioner_defaults():
    result = normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "image_captioner",
                    "name": "captioner",
                    "prompt": "Describe.",
                }
            ]
        )
    )
    node = result["nodes"][0]
    assert node["type"] == "image_captioner"
    assert node["image_mode"] == "batch"
    assert node["expect_json"] is False
    assert node["strip_images"] is False
    assert result["reasoning"] is False


def test_normalize_strip_images_flag():
    result = normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "image_captioner",
                    "name": "captioner",
                    "prompt": "Describe.",
                    "strip_images": True,
                }
            ]
        )
    )
    assert result["nodes"][0]["strip_images"] is True
    api_result = normalize_workflow(
        simple_workflow(
            nodes=[{"type": "api_request", "name": "note", "prompt": "x", "strip_images": True}]
        )
    )
    assert api_result["nodes"][0]["strip_images"] is False


def test_normalize_rejects_bad_image_mode():
    with pytest.raises(WorkflowError, match="unknown image mode"):
        normalize_workflow(
            simple_workflow(
                nodes=[
                    {
                        "type": "image_captioner",
                        "name": "captioner",
                        "prompt": "Describe.",
                        "image_mode": "solo",
                    }
                ]
            )
        )


def test_normalize_coerces_reasoning_flag():
    assert normalize_workflow(simple_workflow(reasoning=True))["reasoning"] is True
    assert normalize_workflow(simple_workflow(reasoning="yes"))["reasoning"] is True
    assert normalize_workflow(simple_workflow())["reasoning"] is False


IMAGE_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "Look at these"},
            {"type": "image_url", "image_url": {"url": "https://img/1.png"}},
            {"type": "image_url", "image_url": {"url": "https://img/2.png"}},
        ],
    }
]


def caption_workflow(mode="batch", messages=None, strip=True):
    return simple_workflow(
        nodes=[
            {
                "type": "image_captioner",
                "name": "captioner",
                "prompt": "Describe.",
                "image_mode": mode,
                "strip_images": strip,
            }
        ],
        messages=messages
        if messages is not None
        else [
            {
                "kind": "text",
                "role": "system",
                "content": "Captions ({{ captioner.count }}): {{ captioner.content }}",
            },
            {"kind": "core_context"},
        ],
    )


def test_image_captioner_batch_sends_one_request(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, caption_workflow())
    stub = install_stub(app, final_response(), posts=[chat_reply("Two cats.")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": IMAGE_MESSAGES},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert len(stub.posts) == 1
    sub_payload = stub.posts[0][1]
    assert sub_payload["model"] == "final-model"
    content = sub_payload["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "Describe."}
    images = [part for part in content if part["type"] == "image_url"]
    assert [part["image_url"]["url"] for part in images] == [
        "https://img/1.png",
        "https://img/2.png",
    ]
    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["messages"][0] == {
        "role": "system",
        "content": "Captions (2): Two cats.",
    }
    assert final_payload["messages"][1:] == [
        {"role": "user", "content": [{"type": "text", "text": "Look at these"}]}
    ]


def test_image_captioner_individual_one_request_per_image(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, caption_workflow(mode="individual"))
    stub = install_stub(
        app,
        final_response(),
        posts=[chat_reply("First."), chat_reply("Second.")],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": IMAGE_MESSAGES},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert len(stub.posts) == 2
    for index, (_, sub_payload, _, _) in enumerate(stub.posts):
        images = [
            part
            for part in sub_payload["messages"][-1]["content"]
            if part["type"] == "image_url"
        ]
        assert len(images) == 1
        assert images[0]["image_url"]["url"] == f"https://img/{index + 1}.png"
    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["messages"][0] == {
        "role": "system",
        "content": "Captions (2): First.\n\nSecond.",
    }
    assert final_payload["messages"][1:] == [
        {"role": "user", "content": [{"type": "text", "text": "Look at these"}]}
    ]


def test_image_captioner_strip_updates_core_context_for_later_nodes(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {
                    "type": "image_captioner",
                    "name": "captioner",
                    "prompt": "Describe.",
                    "strip_images": True,
                },
                {
                    "type": "api_request",
                    "name": "note",
                    "prompt": "Context: {{ core_context }}",
                },
            ]
        ),
    )
    stub = install_stub(
        app, final_response(), posts=[chat_reply("Two cats."), chat_reply("ok")]
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": IMAGE_MESSAGES},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    later_prompt = stub.posts[1][1]["messages"][-1]["content"]
    assert "image_url" not in later_prompt
    assert "Look at these" in later_prompt


def test_image_captioner_drops_message_left_with_no_parts(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, caption_workflow())
    stub = install_stub(app, final_response(), posts=[chat_reply("A cat.")])
    client = app.test_client()
    messages = IMAGE_MESSAGES + [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://img/3.png"}}]}
    ]

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": messages},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["messages"][1:] == [
        {"role": "user", "content": [{"type": "text", "text": "Look at these"}]}
    ]
    body = json.dumps(final_payload)
    assert "https://img/" not in body


def test_image_captioner_no_strip_by_default(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, caption_workflow(strip=False))
    stub = install_stub(app, final_response(), posts=[chat_reply("Two cats.")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": IMAGE_MESSAGES},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["messages"][1] == IMAGE_MESSAGES[0]
    assert "image_url" in json.dumps(final_payload)


def test_image_captioner_without_images_is_skipped(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(db, caption_workflow())
    stub = install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "Text only"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts == []
    final_payload = json.loads(stub.sent[0][0]["content"])
    assert final_payload["messages"][0] == {
        "role": "system",
        "content": "Captions (0): ",
    }
    assert final_payload["messages"][1:] == [{"role": "user", "content": "Text only"}]


# -- node reasoning ----------------------------------------------------------


def test_workflow_reasoning_off_by_default(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(nodes=[{"type": "api_request", "name": "prep", "prompt": "Go"}]),
    )
    stub = install_stub(
        app,
        final_response(),
        posts=[reasoning_reply("ok", "I should say ok.")],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    body = response.get_json()
    message = body["choices"][0]["message"]
    assert "reasoning_content" not in message
    assert "I should say ok." not in json.dumps(body)


def test_workflow_reasoning_injected_non_streamed(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[{"type": "api_request", "name": "prep", "prompt": "Go"}],
            reasoning=True,
        ),
    )
    stub = install_stub(
        app,
        final_response(),
        posts=[reasoning_reply("ok", "I should say ok.")],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    message = response.get_json()["choices"][0]["message"]
    assert message["reasoning_content"].startswith(
        "Started constructing node: prep\nprep logs: ```\nI should say ok.\n```"
    )
    assert message["content"] == "Done."


def test_workflow_reasoning_injected_streamed(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[{"type": "api_request", "name": "prep", "prompt": "Go"}],
            reasoning=True,
        ),
    )
    chunks = [
        b'data: {"id":"1","choices":[{"delta":{"content":"hey"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    install_stub(
        app,
        StubResponse(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            stream_chunks=chunks,
        ),
        posts=[reasoning_reply("ok", "Streamed thinking.")],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "stream": True, "messages": []},
        headers=auth_header(token),
    )
    body = b"".join(response.response)

    assert b"Started constructing node: prep" in body
    assert b"Streamed thinking." in body
    assert body.index(b"midware-workflow") < body.index(b'"content":"hey"')


def test_workflow_node_reasoning_variable(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {"type": "api_request", "name": "prep", "prompt": "Think."},
                {"type": "api_request", "name": "act", "prompt": "Notes: {{ prep.reasoning }}"},
            ]
        ),
    )
    stub = install_stub(
        app,
        final_response(),
        posts=[
            reasoning_reply("ok", "first thoughts"),
            chat_reply("done"),
        ],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts[1][1]["messages"][-1]["content"] == "Notes: first thoughts"


def test_workflow_reasoning_merges_upstream_reasoning(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[{"type": "api_request", "name": "prep", "prompt": "Go"}],
            reasoning=True,
        ),
    )
    upstream_body = {
        "id": "final",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Done.",
                    "reasoning_content": "Upstream's own.",
                }
            }
        ],
    }
    install_stub(
        app,
        StubResponse(content=json.dumps(upstream_body).encode()),
        posts=[reasoning_reply("ok", "Node thinking.")],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    message = response.get_json()["choices"][0]["message"]
    assert message["reasoning_content"] == (
        "Started constructing node: prep\nprep logs: ```\nNode thinking.\n```\n\nUpstream's own."
    )


# -- environment node --------------------------------------------------------


def test_normalize_environment_node():
    result = normalize_workflow(
        simple_workflow(
            nodes=[{"type": "environment", "name": "env", "actions": ["time", "time"]}]
        )
    )
    node = result["nodes"][0]
    assert node["type"] == "environment"
    assert node["actions"] == ["time"]
    assert node["prompt"] == ""
    assert node["route_id"] is None


@pytest.mark.parametrize(
    "blob, match",
    [
        ({"type": "environment", "name": "env"}, "at least one action"),
        ({"type": "environment", "name": "env", "actions": []}, "at least one action"),
        ({"type": "environment", "name": "env", "actions": ["vibes"]}, "unknown action"),
    ],
)
def test_normalize_environment_rejects_bad_actions(blob, match):
    with pytest.raises(WorkflowError, match=match):
        normalize_workflow(simple_workflow(nodes=[blob]))


def test_format_timestamp_tokens():
    ts = 1_700_000_000
    local = time.localtime(ts)
    assert _format_timestamp(ts, "YYYY-MM-DD") == time.strftime("%Y-%m-%d", local)
    assert _format_timestamp(ts, "YY-MM-DD HH:MN:SE") == time.strftime(
        "%y-%m-%d %H:%M:%S", local
    )
    assert _format_timestamp(ts, "day DD") == "day " + time.strftime("%d", local)


def test_format_timestamp_only_substitutes_bounded_tokens():
    ts = 1_700_000_000
    local = time.localtime(ts)
    assert _format_timestamp(ts, "MN:SE") == time.strftime("%M:%S", local)
    assert _format_timestamp(ts, "SECONDS") == "SECONDS"
    assert _format_timestamp(ts, "DDOS at HH") == "DDOS at " + time.strftime("%H", local)
    assert _format_timestamp(ts, "plain text") == "plain text"


def test_format_timestamp_utc_mode():
    ts = 1_700_000_000
    utc = time.gmtime(ts)
    assert _format_timestamp(ts, "YYYY-MM-DD HH:MN:SE", use_system=False) == time.strftime(
        "%Y-%m-%d %H:%M:%S", utc
    )


def test_environment_timezone_flag_defaults_to_system():
    node = normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "environment",
                    "name": "env",
                    "actions": ["time"],
                }
            ]
        )
    )["nodes"][0]
    assert node["use_system_timezone"] is True

    explicit = normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "environment",
                    "name": "env",
                    "actions": ["time"],
                    "use_system_timezone": False,
                }
            ]
        )
    )["nodes"][0]
    assert explicit["use_system_timezone"] is False


def test_environment_formatted_honours_timezone_flag():
    node = {
        "name": "env",
        "actions": ["time"],
        "use_system_timezone": False,
    }
    scope = WorkflowScope()
    _run_environment(node, scope)
    unix, found = scope.resolve("env.time.unix")
    assert found
    rendered = _render('{{ env.time.formatted("YYYY-MM-DD HH:MN:SE") }}', scope)
    assert rendered == time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(unix))


def test_typecheck_rejects_unknown_variable_in_prompt():
    with pytest.raises(WorkflowError, match="unknown variable"):
        normalize_workflow(
            simple_workflow(
                nodes=[
                    {"type": "api_request", "name": "prep", "prompt": "{{ mystery.path }}"}
                ]
            )
        )


def test_typecheck_rejects_unknown_subkey_on_plain_node():
    with pytest.raises(WorkflowError, match="prep\\.mystery"):
        normalize_workflow(
            simple_workflow(
                nodes=[
                    {"type": "api_request", "name": "prep", "prompt": "ok"},
                    {"type": "api_request", "name": "note", "prompt": "{{ prep.mystery }}"},
                ]
            )
        )


def test_typecheck_rejects_forward_reference():
    with pytest.raises(WorkflowError, match="unknown variable"):
        normalize_workflow(
            simple_workflow(
                nodes=[
                    {
                        "type": "api_request",
                        "name": "first",
                        "prompt": "see {{ later.content }}",
                    },
                    {"type": "api_request", "name": "later", "prompt": "hi"},
                ]
            )
        )


def test_typecheck_rejects_unknown_variable_in_final_block():
    with pytest.raises(WorkflowError, match="Final Request.*ghost"):
        normalize_workflow(
            simple_workflow(
                messages=[
                    {"kind": "text", "role": "user", "content": "{{ ghost }}"}
                ]
            )
        )


def test_typecheck_rejects_unknown_variable_in_assure_operand():
    with pytest.raises(WorkflowError, match="unknown variable"):
        normalize_workflow(
            simple_workflow(
                nodes=[
                    {
                        "type": "assure",
                        "name": "gate",
                        "left": "{{ env.time.unix }}",
                        "operator": "equals",
                        "right": "1",
                    }
                ]
            )
        )


def test_typecheck_allows_origin_and_prior_node_paths():
    normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "api_request",
                    "name": "prep",
                    "prompt": "Ctx: {{ core_context }} Hdrs: {{ system_request }}",
                },
                {
                    "type": "image_captioner",
                    "name": "cap",
                    "prompt": "Recap: {{ prep.content }} / {{ prep.reasoning }}",
                },
            ],
            messages=[
                {
                    "kind": "text",
                    "role": "user",
                    "content": "{{ cap.count }} images: {{ cap.content }}",
                }
            ],
        )
    )


def test_typecheck_expect_json_lifts_dotted_paths():
    normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "api_request",
                    "name": "analyze",
                    "prompt": "go",
                    "expect_json": True,
                },
                {
                    "type": "api_request",
                    "name": "note",
                    "prompt": "Category: {{ analyze.category }} Extra: {{ analyze.whatever.nested }}",
                },
            ]
        )
    )


def test_typecheck_expect_json_lifts_final_block_paths():
    normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "api_request",
                    "name": "analyze",
                    "prompt": "go",
                    "expect_json": True,
                }
            ],
            messages=[
                {"kind": "text", "role": "user", "content": "{{ analyze.category }}"}
            ],
        )
    )


def test_typecheck_environment_paths_after_environment_node():
    normalize_workflow(
        simple_workflow(
            nodes=[
                {"type": "environment", "name": "env", "actions": ["time"]},
                {
                    "type": "assure",
                    "name": "gate",
                    "left": '{{ env.time.formatted("HH") }}',
                    "operator": "not_empty",
                    "right": "",
                },
            ]
        )
    )


def test_typecheck_environment_subkey_rejected():
    with pytest.raises(WorkflowError, match="env\\.time\\.mystery"):
        normalize_workflow(
            simple_workflow(
                nodes=[
                    {"type": "environment", "name": "env", "actions": ["time"]},
                    {
                        "type": "api_request",
                        "name": "note",
                        "prompt": "{{ env.time.mystery }}",
                    },
                ]
            )
        )


# -- context node ------------------------------------------------------------


def test_normalize_context_node():
    result = normalize_workflow(
        simple_workflow(
            nodes=[{"type": "context", "name": "ctx"}]
        )
    )
    node = result["nodes"][0]
    assert node["type"] == "context"
    assert node["trim_count"] == 0
    assert node["trim_drop_system"] is False
    assert node["trim_drop_images"] is False

    explicit = normalize_workflow(
        simple_workflow(
            nodes=[
                {
                    "type": "context",
                    "name": "ctx",
                    "trim_count": 5,
                    "trim_drop_system": True,
                    "trim_drop_images": True,
                }
            ]
        )
    )["nodes"][0]
    assert explicit["trim_count"] == 5
    assert explicit["trim_drop_system"] is True
    assert explicit["trim_drop_images"] is True

    capped = normalize_workflow(
        simple_workflow(nodes=[{"type": "context", "name": "ctx", "trim_count": 99999}])
    )["nodes"][0]
    assert capped["trim_count"] == 1000


def test_run_context_publishes_stats_and_extracts():
    messages = [
        {"role": "system", "content": "be nice"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this "},
                {"type": "image_url", "image_url": {"url": "http://x/img.png"}},
            ],
        },
    ]
    scope = WorkflowScope()
    reasoning = _run_context({"name": "ctx", "trim_count": 0}, scope, messages)

    stats, found = scope.resolve("ctx")
    assert found
    assert stats["count"] == 4
    assert stats["user_count"] == 2
    assert stats["assistant_count"] == 1
    assert stats["image_count"] == 1
    assert stats["last_user"] == "look at this [image]"
    assert stats["first_user"] == "first question"
    assert stats["last_assistant"] == "first answer"
    assert stats["text"].startswith("system: be nice\nuser: first question")
    assert "4 messages" in reasoning
    assert "2 user / 1 assistant" in reasoning

    trimmed = json.loads(stats["trimmed"])
    assert [message["role"] for message in trimmed] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


def test_run_context_trim_options():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "three "},
                {"type": "image_url", "image_url": {"url": "http://x/i.png"}},
            ],
        },
    ]
    scope = WorkflowScope()
    _run_context(
        {
            "name": "ctx",
            "trim_count": 2,
            "trim_drop_system": True,
            "trim_drop_images": True,
        },
        scope,
        messages,
    )
    stats, _ = scope.resolve("ctx")
    trimmed = json.loads(stats["trimmed"])
    # Drop-images keeps messages that still have text parts (captioner semantics).
    assert trimmed == [
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": [{"type": "text", "text": "three "}]},
    ]


def test_context_node_end_to_end(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {"type": "context", "name": "ctx"},
                {
                    "type": "api_request",
                    "name": "note",
                    "prompt": (
                        "N={{ ctx.count }} U={{ ctx.user_count }} "
                        "L={{ ctx.last_user }} T={{ ctx.text }}"
                    ),
                },
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
                {"role": "system", "content": "be nice"},
                {"role": "user", "content": "hello world"},
            ],
        },
        headers=auth_header(token),
    )

    assert response.status_code == 200
    prompt = stub.posts[0][1]["messages"][-1]["content"]
    assert prompt == (
        "N=2 U=1 L=hello world T=system: be nice\nuser: hello world"
    )


def test_context_typecheck_known_and_unknown():
    normalize_workflow(
        simple_workflow(
            nodes=[
                {"type": "context", "name": "ctx"},
                {
                    "type": "api_request",
                    "name": "note",
                    "prompt": "{{ ctx.count }} {{ ctx.trimmed }} {{ ctx.text }}",
                },
            ]
        )
    )
    with pytest.raises(WorkflowError, match="ctx\\.nope"):
        normalize_workflow(
            simple_workflow(
                nodes=[
                    {"type": "context", "name": "ctx"},
                    {"type": "api_request", "name": "note", "prompt": "{{ ctx.nope }}"},
                ]
            )
        )


def test_context_reasoning_entry(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            reasoning=True,
            nodes=[
                {"type": "context", "name": "ctx"},
                {"type": "api_request", "name": "note", "prompt": "hi {{ ctx.count }}"},
            ],
        ),
    )
    install_stub(
        app,
        final_response(),
        posts=[chat_reply("ok", usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12})],
    )
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "hey"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    reasoning = response.get_json()["choices"][0]["message"]["reasoning_content"]
    assert "Started constructing node: ctx" in reasoning
    assert "Inspected context: 1 messages" in reasoning


def test_environment_node_publishes_time_variables(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {"type": "environment", "name": "env", "actions": ["time"]},
                {
                    "type": "api_request",
                    "name": "note",
                    "prompt": "At {{ env.time.unix }} it is {{ env.time.formatted(\"YYYY-MM-DD\") }}",
                },
            ]
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
    assert len(stub.posts) == 1
    prompt = stub.posts[0][1]["messages"][-1]["content"]
    match = re.search(r"At (\d{9,10}) it is (\d{4}-\d{2}-\d{2})", prompt)
    assert match, prompt
    assert int(match.group(1)) <= int(time.time())


def test_environment_node_reasoning_and_no_forward(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[{"type": "environment", "name": "env", "actions": ["time"]}],
            reasoning=True,
        ),
    )
    stub = install_stub(app, final_response())
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": [{"role": "user", "content": "Hi"}]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    assert stub.posts == []
    message = response.get_json()["choices"][0]["message"]
    assert re.search(r"Started constructing node: env\nenv logs: ```\nDeclare time: \d+", message["reasoning_content"])


def test_render_time_formatted_call_syntax():
    scope = WorkflowScope()
    scope.set("env", {"time": {"unix": 0, "formatted": lambda fmt: f"FORMATTED:{fmt}"}})
    assert _render('{{ env.time.formatted("YY-MM-DD") }}', scope) == "FORMATTED:YY-MM-DD"
    assert _render("{{ env.time.formatted('HH') }}", scope) == "FORMATTED:HH"
    assert _render("@@env.time.formatted('HH')@@", scope) == "FORMATTED:HH"
    assert _render("{{ env.time.unix }}", scope) == "0"
    assert _render("{{ env.time.formatted }}", scope) == "{{ env.time.formatted }}"
    assert _render('{{ env.time.formatted("x") }}', WorkflowScope()) == (
        '{{ env.time.formatted("x") }}'
    )


def test_environment_formatted_colon_format_end_to_end(configured):
    """Regression: {{ env.time.formatted("MN:SE") }} must not stay literal."""
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {"type": "environment", "name": "env", "actions": ["time"]},
                {
                    "type": "api_request",
                    "name": "note",
                    "prompt": 'Clock: {{ env.time.formatted("MN:SE") }}',
                },
            ]
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
    prompt = stub.posts[0][1]["messages"][-1]["content"]
    assert "{{" not in prompt
    assert re.fullmatch(r"Clock: \d{2}:\d{2}", prompt), prompt


# -- assure node -------------------------------------------------------------


def test_normalize_assure_node():
    result = normalize_workflow(
        simple_workflow(
            nodes=[
                {"type": "api_request", "name": "note", "prompt": "ok"},
                {
                    "type": "assure",
                    "name": "gate",
                    "left": "{{ note.content }}",
                    "operator": "contains",
                    "right": "ok",
                },
            ]
        )
    )
    node = result["nodes"][1]
    assert node["type"] == "assure"
    assert node["left"] == "{{ note.content }}"
    assert node["operator"] == "contains"
    assert node["right"] == "ok"


@pytest.mark.parametrize(
    "blob, match",
    [
        ({"type": "assure", "name": "gate", "left": "", "operator": "equals", "right": "x"},
         "left-hand value"),
        ({"type": "assure", "name": "gate", "left": "a", "operator": "telepathy", "right": "x"},
         "unknown operator"),
        ({"type": "assure", "name": "gate", "left": "a", "operator": "equals", "right": "  "},
         "right-hand value"),
    ],
)
def test_normalize_assure_rejects_invalid(blob, match):
    with pytest.raises(WorkflowError, match=match):
        normalize_workflow(simple_workflow(nodes=[blob]))


def test_normalize_assure_unary_operator_needs_no_right():
    result = normalize_workflow(
        simple_workflow(
            nodes=[{"type": "assure", "name": "gate", "left": "x", "operator": "not_empty"}]
        )
    )
    assert result["nodes"][0]["right"] == ""


@pytest.mark.parametrize(
    "operator, left, right, expected",
    [
        ("equals", "abc", "abc", True),
        ("equals", "abc", "xyz", False),
        ("not_equals", "abc", "xyz", True),
        ("contains", "hello world", "world", True),
        ("contains", "hello", "xyz", False),
        ("not_contains", "hello", "xyz", True),
        ("starts_with", "hello", "he", True),
        ("starts_with", "hello", "lo", False),
        ("ends_with", "hello", "lo", True),
        ("ends_with", "hello", "he", False),
        ("greater", "10", "9", True),
        ("greater", "9", "10", False),
        ("less", "9", "10", True),
        ("greater", "b", "a", True),
        ("empty", "   ", "ignored", True),
        ("empty", "x", "ignored", False),
        ("not_empty", "x", "ignored", True),
    ],
)
def test_assure_check(operator, left, right, expected):
    assert _assure_check(operator, left, right) is expected


def test_assure_pass_end_to_end(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {
                    "type": "assure",
                    "name": "gate",
                    "left": "allowed",
                    "operator": "equals",
                    "right": "allowed",
                },
                {"type": "api_request", "name": "note", "prompt": "Go"},
            ],
            reasoning=True,
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
    assert len(stub.posts) == 1
    message = response.get_json()["choices"][0]["message"]
    assert "Passed gate Assurance Test" in message["reasoning_content"]


def test_assure_fail_aborts_workflow(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {
                    "type": "assure",
                    "name": "gate",
                    "left": "allowed",
                    "operator": "equals",
                    "right": "denied",
                },
                {"type": "api_request", "name": "note", "prompt": "Go"},
            ]
        ),
    )
    stub = install_stub(app, final_response(), posts=[chat_reply("ok")])
    client = app.test_client()

    response = client.post(
        "/v1/chat/completions",
        json={"model": "<p>-wf", "messages": []},
        headers=auth_header(token),
    )

    assert response.status_code == 502
    body = response.get_json()
    assert body["error"]["code"] == "workflow_node_failed"
    assert "gate" in body["error"]["message"]
    assert stub.posts == []
    assert stub.sent == []
    assert "workflow[gate]" in db.list_requests(limit=1)[0]["error"]


def test_assure_sees_environment_variables(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    create_workflow_premodel(
        db,
        simple_workflow(
            nodes=[
                {"type": "environment", "name": "env", "actions": ["time"]},
                {
                    "type": "assure",
                    "name": "gate",
                    "left": "{{ env.time.unix }}",
                    "operator": "greater",
                    "right": "1",
                },
                {"type": "api_request", "name": "note", "prompt": "Go"},
            ]
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
    assert len(stub.posts) == 1
