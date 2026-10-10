from __future__ import annotations

import json

import httpx
import pytest

from midware.workflow import (
    WorkflowError,
    WorkflowScope,
    _extract_json,
    _render,
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
