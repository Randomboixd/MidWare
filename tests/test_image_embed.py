"""Image generation and embeddings as first-class proxied endpoints.

Covers the endpoint-family split (routing + prompt-only token accounting),
multipart image edits (pass-through, prefix rewrite, viewer), the premodel
chat-only rule at both request and save time, and the conversation viewer's
image/embedding blocks.
"""

from __future__ import annotations

import io
import json

from midware.conversation import build_conversation
from midware.usage import parse_multipart_fields
from test_proxy import StubResponse, auth_header, install_stub

PNG = b"\x89PNG\r\n\x1a\n" + b"midware-test-image-bytes"


def image_response(url="https://cdn.example.com/a.png", usage=None):
    body = {"created": 1, "data": [{"url": url}]}
    if usage:
        body["usage"] = usage
    return StubResponse(content=json.dumps(body).encode())


def embedding_response(dimensions=4, usage=None):
    body = {"object": "list", "data": [{"embedding": [0.1] * dimensions}]}
    if usage:
        body["usage"] = usage
    return StubResponse(content=json.dumps(body).encode())


def _create_story_premodel(db):
    return db.create_premodel(
        name="Story",
        slug="story",
        route_id=db.active_route()["id"],
        model="story-model",
    )


# -- endpoint families / token accounting ------------------------------------


def test_endpoint_family_split():
    from midware.blueprints.proxy import _endpoint_family

    assert _endpoint_family("images/generations") == "image"
    assert _endpoint_family("images/edits") == "image"
    assert _endpoint_family("images/variations") == "image"
    assert _endpoint_family("embeddings") == "embedding"
    assert _endpoint_family("chat/completions") == "chat"
    assert _endpoint_family("responses") == "chat"


def test_image_generation_records_upstream_usage(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    stub = install_stub(
        app, image_response(usage={"input_tokens": 9, "output_tokens": 12})
    )
    client = app.test_client()

    response = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-1", "prompt": "a red panda", "size": "1024x1024"},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    request = stub.sent[0][0]
    assert request["url"] == "https://api.example.com/images/generations"
    assert request["headers"]["Authorization"] == "Bearer sk-upstream"
    assert request["content"] == json.dumps(
        {"model": "gpt-image-1", "prompt": "a red panda", "size": "1024x1024"}
    ).encode()
    row = db.list_requests(limit=1)[0]
    assert row["prompt_tokens"] == 9
    assert row["completion_tokens"] == 12
    assert row["total_tokens"] == 21
    assert row["token_source"] == "upstream"
    assert row["model"] == "gpt-image-1"


def test_image_generation_without_usage_never_counts_the_url(configured):
    # A URL/b64 payload is not output text: only the prompt may be estimated.
    app, token = configured
    db = app.extensions["midware_db"]
    long_url = "https://cdn.example.com/" + "a" * 40000
    stub = install_stub(app, image_response(url=long_url))
    client = app.test_client()

    response = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-1", "prompt": "a red panda wearing a tiny hat"},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    row = db.list_requests(limit=1)[0]
    assert row["completion_tokens"] == 0
    assert 0 < row["prompt_tokens"] < 50
    assert row["total_tokens"] == row["prompt_tokens"]
    assert row["token_source"] in ("tiktoken", "chars")


def test_embeddings_records_upstream_usage(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    stub = install_stub(
        app, embedding_response(usage={"prompt_tokens": 6, "total_tokens": 6})
    )
    client = app.test_client()

    response = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "banana phone"},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    request = stub.sent[0][0]
    assert request["url"] == "https://api.example.com/embeddings"
    row = db.list_requests(limit=1)[0]
    assert row["prompt_tokens"] == 6
    assert row["completion_tokens"] == 0
    assert row["total_tokens"] == 6
    assert row["token_source"] == "upstream"


def test_embeddings_without_usage_count_input_not_vectors(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    stub = install_stub(app, embedding_response(dimensions=300))
    client = app.test_client()

    response = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": "banana phone"},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    row = db.list_requests(limit=1)[0]
    assert row["completion_tokens"] == 0
    assert 0 < row["prompt_tokens"] < 50
    assert row["token_source"] in ("tiktoken", "chars")


def test_embeddings_array_input_is_counted(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    stub = install_stub(app, embedding_response())
    client = app.test_client()

    response = client.post(
        "/v1/embeddings",
        json={"model": "text-embedding-3-small", "input": ["alpha", "beta"]},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    row = db.list_requests(limit=1)[0]
    assert row["prompt_tokens"] > 0
    assert row["completion_tokens"] == 0


# -- [Host] prefix routing ---------------------------------------------------


def test_image_host_prefix_routes_and_strips(multi_host):
    app, token = multi_host
    db = app.extensions["midware_db"]
    stub = install_stub(app, image_response())
    client = app.test_client()

    response = client.post(
        "/v1/images/generations",
        json={"model": "[NanoGPT]gpt-image-1", "prompt": "a red panda"},
        headers=auth_header(token),
    )

    assert response.status_code == 200
    request = stub.sent[0][0]
    assert (
        request["url"]
        == "https://nano-gpt.com/subscription/v1/images/generations"
    )
    payload = json.loads(request["content"])
    assert payload["model"] == "gpt-image-1"
    assert "prefix" not in json.dumps(payload).lower()
    row = db.list_requests(limit=1)[0]
    assert row["host_prefix"] == "NanoGPT"
    assert row["model_raw"] == "[NanoGPT]gpt-image-1"


# -- multipart image edits ---------------------------------------------------


def _post_edit(client, token, data):
    return client.post(
        "/v1/images/edits", data=data, headers=auth_header(token)
    )


def test_multipart_edit_forwards_form_and_counts_prompt(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    stub = install_stub(app, image_response())
    client = app.test_client()

    response = _post_edit(
        client,
        token,
        {
            "model": "gpt-image-1",
            "prompt": "make it blue",
            "image": (io.BytesIO(PNG), "cat.png"),
        },
    )

    assert response.status_code == 200
    request = stub.sent[0][0]
    assert request["url"] == "https://api.example.com/images/edits"
    assert request["headers"]["Content-Type"].startswith("multipart/form-data")
    fields = parse_multipart_fields(request["content"])
    assert fields["model"] == "gpt-image-1"
    assert fields["prompt"] == "make it blue"
    assert b'filename="cat.png"' in request["content"]
    assert PNG in request["content"]
    row = db.list_requests(limit=1)[0]
    assert row["model"] == "gpt-image-1"
    assert row["prompt_tokens"] > 0
    assert row["completion_tokens"] == 0
    assert row["token_source"] in ("tiktoken", "chars")


def test_multipart_edit_passes_through_without_rewrite(configured):
    app, token = configured
    stub = install_stub(app, image_response())
    client = app.test_client()
    body = (
        b'--bound123\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
        b"gpt-image-1\r\n"
        b'--bound123\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n'
        b"make it blue\r\n"
        b"--bound123--\r\n"
    )

    response = client.post(
        "/v1/images/edits",
        data=body,
        content_type="multipart/form-data; boundary=bound123",
        headers=auth_header(token),
    )

    assert response.status_code == 200
    request = stub.sent[0][0]
    # No routing rewrite: the client's bytes and boundary reach upstream intact.
    assert request["content"] == body
    assert request["headers"]["Content-Type"] == (
        "multipart/form-data; boundary=bound123"
    )


def test_multipart_edit_host_prefix_rewrite_rebuilds_body(configured):
    app, token = configured
    stub = install_stub(app, image_response())
    client = app.test_client()

    response = _post_edit(
        client,
        token,
        {
            "model": "[Default]gpt-image-1",
            "prompt": "make it blue",
            "image": (io.BytesIO(PNG), "cat.png"),
        },
    )

    assert response.status_code == 200
    request = stub.sent[0][0]
    fields = parse_multipart_fields(request["content"])
    assert fields["model"] == "gpt-image-1"
    assert fields["prompt"] == "make it blue"
    assert PNG in request["content"]
    # A rebuilt body needs a boundary header that names the new boundary.
    content_type = request["headers"]["Content-Type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1]
    assert f"--{boundary}".encode() in request["content"]
    assert boundary != "bound123"


# -- premodels are chat-only -------------------------------------------------


def test_premodel_prefix_rejected_on_image_endpoint(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    _create_story_premodel(db)
    stub = install_stub(app, image_response())
    client = app.test_client()

    response = client.post(
        "/v1/images/generations",
        json={"model": "<p>-story", "prompt": "x"},
        headers=auth_header(token),
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "premodel_chat_only"
    assert not stub.sent


def test_premodel_prefix_rejected_on_embeddings(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    _create_story_premodel(db)
    stub = install_stub(app, embedding_response())
    client = app.test_client()

    response = client.post(
        "/v1/embeddings",
        json={"model": "<p>-story", "input": "x"},
        headers=auth_header(token),
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "premodel_chat_only"
    assert not stub.sent


def test_premodel_multipart_body_rejected_on_chat(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    _create_story_premodel(db)
    stub = install_stub(app, image_response())
    client = app.test_client()
    body = (
        b'--bound123\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
        b"<p>-story\r\n"
        b'--bound123\r\nContent-Disposition: form-data; name="messages"\r\n\r\n'
        b"[]\r\n"
        b"--bound123--\r\n"
    )

    response = client.post(
        "/v1/chat/completions",
        data=body,
        content_type="multipart/form-data; boundary=bound123",
        headers=auth_header(token),
    )

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "premodel_body_not_json"
    assert not stub.sent


def test_premodel_admin_refuses_non_chat_models(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    route = db.active_route()
    client = app.test_client()

    for model in ("text-embedding-3-small", "dall-e-3", "gpt-image-1", "flux.1-dev"):
        response = client.post(
            "/admin/premodels/new",
            data={
                "name": "Bad Model",
                "slug": f"bad-{model.replace('.', '-')}",
                "route_id": route["id"],
                "model": model,
                "prompt_mode": "simple",
                "system_prompt": "hi",
            },
        )
        assert response.status_code == 200
        assert b"chat-only" in response.data
        assert db.get_premodel_by_slug(f"bad-{model.replace('.', '-')}") is None


def test_premodel_admin_still_accepts_chat_models(configured):
    app, token = configured
    db = app.extensions["midware_db"]
    route = db.active_route()
    client = app.test_client()

    response = client.post(
        "/admin/premodels/new",
        data={
            "name": "Good Model",
            "slug": "good-model",
            "route_id": route["id"],
            "model": "gpt-4o",
            "prompt_mode": "simple",
            "system_prompt": "hi",
        },
    )

    assert response.status_code == 302
    assert db.get_premodel_by_slug("good-model") is not None


# -- conversation viewer -----------------------------------------------------


def test_conversation_shows_image_prompt_and_data():
    request = json.dumps(
        {"model": "gpt-image-1", "prompt": "a red panda", "n": 2, "size": "1024x1024"}
    )
    response = json.dumps(
        {"created": 1, "data": [{"url": "https://cdn.example.com/a.png"}]}
    )

    convo = build_conversation(request, response)

    assert [m["role"] for m in convo["messages"]] == ["user", "assistant"]
    assert convo["messages"][0]["text"] == "a red panda"
    assert convo["messages"][1]["text"] == "https://cdn.example.com/a.png"
    assert convo["request_meta"]["n"] == 2
    assert convo["request_meta"]["size"] == "1024x1024"


def test_conversation_renders_embedding_dimensions():
    request = json.dumps({"model": "text-embedding-3-small", "input": "banana"})
    response = json.dumps({"object": "list", "data": [{"embedding": [0.1] * 4}]})

    convo = build_conversation(request, response)

    assert [m["role"] for m in convo["messages"]] == ["user", "assistant"]
    assert convo["messages"][0]["text"] == "banana"
    assert convo["messages"][1]["text"] == "[embedding #0: 4 dimensions]"


def test_conversation_shows_multipart_prompt_not_image_bytes():
    request = (
        b'--bound123\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
        b"gpt-image-1\r\n"
        b'--bound123\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n'
        b"make it blue\r\n"
        b'--bound123\r\nContent-Disposition: form-data; name="image"; '
        b'filename="cat.png"\r\nContent-Type: image/png\r\n\r\n' + PNG + b"\r\n"
        b"--bound123--\r\n"
    )
    response = json.dumps(
        {"created": 1, "data": [{"b64_json": "aGVsbG8="}]}
    )

    convo = build_conversation(request, response)

    assert [m["role"] for m in convo["messages"]] == ["user", "assistant"]
    assert convo["messages"][0]["text"] == "make it blue"
    assert "PNG" not in convo["messages"][0]["text"]
    assert "base64, 8 characters" in convo["messages"][1]["text"]
