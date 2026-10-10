"""Workflow pipelines for premodels: chained AI sub-calls before the final forward.

A workflow is a linear chain stored on a premodel (``workflow_json``): an
undeletable Origin publishes the inbound request as template variables, zero to
eight ``api_request`` nodes each make a nested, non-streaming chat call whose
reply joins the variable scope, and an undeletable Final Request builds the
message list that is finally forwarded upstream.

The graph is validated once at save time (``normalize_workflow``) and again
defensively at load (``load_workflow``), because a hand-edited database should
fail loudly rather than forward a half-built request.

Sub-calls intentionally bypass the calling key's provider/model allowlists:
they are server-side machinery the admin configured, not something the client
addressed. Only the final forwarded model is subject to the key's limits.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import httpx

from .conversation import _collect_reasoning
from .premodels import FALLBACK_MACRO_RE, MACRO_RE
from .upstream import HOP_BY_HOP, upstream_url

WORKFLOW_MAX_INTERMEDIATE = 8
WORKFLOW_MAX_FINAL_BLOCKS = 20
WORKFLOW_MAX_CONTENT = 20_000
WORKFLOW_MAX_MODEL = 200
WORKFLOW_NODE_TIMEOUT_DEFAULT = 60
WORKFLOW_NODE_TIMEOUT_MAX = 120

NODE_TYPES = ("api_request", "image_captioner")
IMAGE_MODES = ("batch", "individual")
FINAL_ROLES = ("system", "user", "assistant")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
STRIPPED_REQUEST_HEADERS = HOP_BY_HOP | {"authorization", "x-api-key"}


class WorkflowError(ValueError):
    """Save/load-time validation failure; the message is shown to the admin."""


class WorkflowNodeError(Exception):
    """A sub-call failed; ``node_name`` identifies the offending node."""

    def __init__(self, node_name: str, detail: str, status_code: int | None = None):
        super().__init__(detail)
        self.node_name = node_name
        self.detail = detail
        self.status_code = status_code


class WorkflowResult:
    def __init__(
        self,
        messages: list[dict[str, Any]],
        model_override: str,
        usage: dict[str, int],
        reasoning: str = "",
    ):
        self.messages = messages
        self.model_override = model_override
        self.usage = usage
        self.reasoning = reasoning


# -- validation / normalization ---------------------------------------------


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_block(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkflowError(f"Final Request block {index + 1} is not an object.")
    kind = str(raw.get("kind") or "text").strip().lower()
    if kind == "core_context":
        return {"kind": "core_context"}
    if kind != "text":
        raise WorkflowError(f"Final Request block {index + 1} has unknown kind '{kind}'.")
    role = str(raw.get("role") or "user").strip().lower()
    if role not in FINAL_ROLES:
        raise WorkflowError(
            f"Final Request block {index + 1} has unknown role '{role}'."
        )
    content = raw.get("content")
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    if len(content) > WORKFLOW_MAX_CONTENT:
        raise WorkflowError(
            f"Final Request block {index + 1} exceeds {WORKFLOW_MAX_CONTENT} characters."
        )
    return {"kind": "text", "role": role, "content": content}


def _normalize_node(raw: Any, index: int, seen: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise WorkflowError(f"Node {index + 1} is not an object.")
    node_type = str(raw.get("type") or "api_request").strip().lower()
    if node_type not in NODE_TYPES:
        raise WorkflowError(f"Node {index + 1} has unknown type '{node_type}'.")

    name = str(raw.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise WorkflowError(
            f"Node {index + 1} name '{name}' must be 1-32 lowercase letters, digits or dashes."
        )
    if name in seen:
        raise WorkflowError(f"Duplicate node name '{name}'.")
    seen.add(name)

    route_id = _as_int(raw.get("route_id"))
    if raw.get("route_id") not in (None, "") and route_id is None:
        raise WorkflowError(f"Node '{name}' has an invalid connection.")
    if route_id is not None and route_id < 1:
        route_id = None

    prompt = raw.get("prompt")
    if not isinstance(prompt, str):
        prompt = "" if prompt is None else str(prompt)
    if not prompt.strip():
        raise WorkflowError(f"Node '{name}' needs a prompt.")
    if len(prompt) > WORKFLOW_MAX_CONTENT:
        raise WorkflowError(f"Node '{name}' prompt exceeds {WORKFLOW_MAX_CONTENT} characters.")

    system_prompt = raw.get("system_prompt")
    if not isinstance(system_prompt, str):
        system_prompt = "" if system_prompt is None else str(system_prompt)
    if len(system_prompt) > WORKFLOW_MAX_CONTENT:
        raise WorkflowError(
            f"Node '{name}' system prompt exceeds {WORKFLOW_MAX_CONTENT} characters."
        )

    model = str(raw.get("model") or "").strip()
    if len(model) > WORKFLOW_MAX_MODEL:
        raise WorkflowError(f"Node '{name}' model name is too long.")

    max_tokens = _as_int(raw.get("max_tokens"))
    if max_tokens is not None and max_tokens < 1:
        max_tokens = None

    timeout = _as_int(raw.get("timeout_seconds"))
    if not timeout or timeout < 1:
        timeout = WORKFLOW_NODE_TIMEOUT_DEFAULT
    timeout = min(timeout, WORKFLOW_NODE_TIMEOUT_MAX)

    image_mode = str(raw.get("image_mode") or "batch").strip().lower()
    if image_mode not in IMAGE_MODES:
        raise WorkflowError(
            f"Node '{name}' has unknown image mode '{image_mode}' "
            f"(expected {' or '.join(IMAGE_MODES)})."
        )

    return {
        "type": node_type,
        "name": name,
        "route_id": route_id,
        "model": model,
        "system_prompt": system_prompt,
        "prompt": prompt,
        "expect_json": bool(raw.get("expect_json")) if node_type == "api_request" else False,
        "image_mode": image_mode,
        "strip_images": bool(raw.get("strip_images")) if node_type == "image_captioner" else False,
        "max_tokens": max_tokens,
        "timeout_seconds": timeout,
    }


def normalize_workflow(raw: Any) -> dict[str, Any]:
    """Validate and canonicalize a workflow blob; raises :class:`WorkflowError`."""
    if isinstance(raw, (str, bytes, bytearray)):
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        if not text.strip():
            raise WorkflowError("Workflow is empty.")
        try:
            raw = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise WorkflowError(f"Workflow is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkflowError("Workflow must be an object.")

    final_raw = raw.get("final")
    if not isinstance(final_raw, dict):
        final_raw = {}
    blocks_raw = final_raw.get("messages")
    if not isinstance(blocks_raw, list) or not blocks_raw:
        raise WorkflowError("A workflow needs at least one Final Request message block.")
    if len(blocks_raw) > WORKFLOW_MAX_FINAL_BLOCKS:
        raise WorkflowError(
            f"Final Request supports at most {WORKFLOW_MAX_FINAL_BLOCKS} message blocks."
        )
    blocks = [_normalize_block(block, index) for index, block in enumerate(blocks_raw)]

    override = final_raw.get("model_override")
    override = "" if override is None else str(override).strip()
    if len(override) > WORKFLOW_MAX_MODEL:
        raise WorkflowError("Final Request model override is too long.")

    nodes_raw = raw.get("nodes")
    if nodes_raw is None:
        nodes_raw = []
    if not isinstance(nodes_raw, list):
        raise WorkflowError("Workflow nodes must be a list.")
    if len(nodes_raw) > WORKFLOW_MAX_INTERMEDIATE:
        raise WorkflowError(
            f"A workflow supports at most {WORKFLOW_MAX_INTERMEDIATE} API Request nodes."
        )
    seen: set[str] = set()
    nodes = [_normalize_node(node, index, seen) for index, node in enumerate(nodes_raw)]

    return {
        "version": 1,
        "reasoning": bool(raw.get("reasoning")),
        "final": {"messages": blocks, "model_override": override},
        "nodes": nodes,
    }


def load_workflow(raw: Any) -> dict[str, Any]:
    """Runtime load of a stored blob (defensive re-validation)."""
    return normalize_workflow(raw)


# -- variable scope ----------------------------------------------------------


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


class WorkflowScope:
    """Flat variable table with one level of dotted lookup (``node.key``)."""

    def __init__(self, variables: dict[str, Any] | None = None):
        self._vars: dict[str, Any] = dict(variables or {})

    def set(self, name: str, value: Any) -> None:
        self._vars[name] = value

    def lookup(self, name: str) -> str | None:
        name = name.strip()
        if not name:
            return None
        if name in self._vars:
            return _stringify(self._vars[name])
        head, sep, tail = name.partition(".")
        if sep and head in self._vars:
            container = self._vars[head]
            if isinstance(container, dict) and tail in container:
                return _stringify(container[tail])
        return None


def _render(text: Any, scope: WorkflowScope) -> Any:
    """Expand ``{{ name }}`` / ``@@name@@`` against the scope; unknown = literal."""
    if not isinstance(text, str) or not text:
        return text

    def replace(match: re.Match[str]) -> str:
        found = scope.lookup(match.group("name"))
        return match.group(0) if found is None else found

    if "{{" in text:
        text = MACRO_RE.sub(replace, text)
    if "@@" in text:
        text = FALLBACK_MACRO_RE.sub(replace, text)
    return text


def _extract_json(text: str) -> Any | None:
    cleaned = (text or "").strip()
    fence = CODE_FENCE_RE.match(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except (ValueError, TypeError):
        return None


def _assistant_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return ""


def _node_headers(route) -> dict[str, str]:
    key = (route["upstream_key"] or "").strip()
    headers = {"Content-Type": "application/json", "Accept": "identity"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _filtered_request_headers(raw: dict[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in raw.items()
        if name.lower() not in STRIPPED_REQUEST_HEADERS
    }


# -- execution ---------------------------------------------------------------


def extract_image_urls(messages: list[Any]) -> list[str]:
    """Image URLs from OpenAI-style ``image_url`` content parts, in order."""
    urls: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url")
            if isinstance(image, dict) and isinstance(image.get("url"), str) and image["url"]:
                urls.append(image["url"])
            elif isinstance(image, str) and image:
                urls.append(image)
    return urls


def strip_images(messages: list[Any]) -> list[Any]:
    """Remove ``image_url`` parts from OpenAI-style content arrays.

    Captioner nodes consume the images and the stripped list becomes the new
    core context, so the final forward and later nodes never re-send them.
    Messages left with no parts are dropped entirely.
    """
    cleaned: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            cleaned.append(message)
            continue
        parts = [
            part
            for part in content
            if not (isinstance(part, dict) and part.get("type") == "image_url")
        ]
        if not parts:
            continue
        if len(parts) == len(content):
            cleaned.append(message)
            continue
        stripped = dict(message)
        stripped["content"] = parts
        cleaned.append(stripped)
    return cleaned


def _extract_reasoning(data: Any) -> str:
    """Reasoning text from a chat-completion body (any provider spelling)."""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return ""
    return "\n\n".join(section for section in _collect_reasoning(message) if section)


def format_node_reasoning(entries: list[tuple[str, str]]) -> str:
    """Render ``(name, text)`` pairs in the client-facing reasoning format."""
    blocks = [
        f"Started constructing node: {name}\n{name} logs: ```\n{text}\n```"
        for name, text in entries
        if name and text.strip()
    ]
    return "\n\n".join(blocks)


def _post_chat(
    http: httpx.Client,
    node: dict[str, Any],
    route: Any,
    payload: dict[str, Any],
    usage: dict[str, int],
) -> Any:
    try:
        response = http.post(
            upstream_url(route, "chat/completions"),
            json=payload,
            headers=_node_headers(route),
            timeout=float(node["timeout_seconds"]),
        )
    except httpx.HTTPError as exc:
        raise WorkflowNodeError(node["name"], f"Sub-request failed: {exc}") from exc

    if response.status_code >= 400:
        raise WorkflowNodeError(
            node["name"],
            f"Sub-request returned HTTP {response.status_code}.",
            response.status_code,
        )
    try:
        data = response.json()
    except (ValueError, TypeError) as exc:
        raise WorkflowNodeError(node["name"], "Sub-request did not return JSON.") from exc

    node_usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(node_usage, dict):
        usage["prompt"] += max(0, _as_int(node_usage.get("prompt_tokens")) or 0)
        usage["completion"] += max(0, _as_int(node_usage.get("completion_tokens")) or 0)
    return data


def _node_model(node: dict[str, Any], premodel: Any, scope: WorkflowScope) -> str:
    model = _render(node["model"], scope) or (premodel["model"] or "").strip()
    if not model:
        raise WorkflowNodeError(node["name"], "No model configured for this node.")
    return model


def _run_api_request(
    http: httpx.Client,
    node: dict[str, Any],
    route: Any,
    premodel: Any,
    scope: WorkflowScope,
    usage: dict[str, int],
) -> str:
    messages: list[dict[str, str]] = []
    system_prompt = _render(node["system_prompt"], scope)
    if isinstance(system_prompt, str) and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": _render(node["prompt"], scope) or ""})

    payload: dict[str, Any] = {
        "model": _node_model(node, premodel, scope),
        "messages": messages,
        "stream": False,
    }
    if node["max_tokens"]:
        payload["max_tokens"] = node["max_tokens"]

    data = _post_chat(http, node, route, payload, usage)
    text = _assistant_text(data)
    variables: dict[str, Any] = {}
    if node["expect_json"]:
        parsed = _extract_json(text)
        if parsed is None:
            raise WorkflowNodeError(node["name"], "Reply was not valid JSON.")
        if isinstance(parsed, dict):
            variables.update(parsed)
        else:
            variables["json"] = parsed
    variables["content"] = text
    reasoning = _extract_reasoning(data)
    if reasoning:
        variables["reasoning"] = reasoning
    scope.set(node["name"], variables)
    return reasoning


def _run_image_captioner(
    http: httpx.Client,
    node: dict[str, Any],
    route: Any,
    premodel: Any,
    scope: WorkflowScope,
    usage: dict[str, int],
    images: list[str],
) -> str:
    name = node["name"]
    if not images:
        # Placed early by design; a request without images simply yields nothing.
        scope.set(name, {"content": "", "count": 0})
        return ""

    model = _node_model(node, premodel, scope)
    prompt = _render(node["prompt"], scope) or ""
    system_prompt = _render(node["system_prompt"], scope)

    batches = [[url] for url in images] if node["image_mode"] == "individual" else [list(images)]
    captions: list[str] = []
    reasoning_parts: list[str] = []
    for batch in batches:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for url in batch:
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages: list[dict[str, Any]] = []
        if isinstance(system_prompt, str) and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if node["max_tokens"]:
            payload["max_tokens"] = node["max_tokens"]
        data = _post_chat(http, node, route, payload, usage)
        captions.append(_assistant_text(data))
        reasoning = _extract_reasoning(data)
        if reasoning:
            reasoning_parts.append(reasoning)

    variables: dict[str, Any] = {"content": "\n\n".join(captions), "count": len(images)}
    combined_reasoning = "\n\n".join(reasoning_parts)
    if combined_reasoning:
        variables["reasoning"] = combined_reasoning
    scope.set(name, variables)
    return combined_reasoning


def _run_node(
    http: httpx.Client,
    node: dict[str, Any],
    route: Any,
    premodel: Any,
    scope: WorkflowScope,
    usage: dict[str, int],
    images: list[str],
) -> str:
    if node["type"] == "image_captioner":
        return _run_image_captioner(http, node, route, premodel, scope, usage, images)
    return _run_api_request(http, node, route, premodel, scope, usage)


def _build_final_messages(
    workflow: dict[str, Any], caller_messages: list[Any], scope: WorkflowScope
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for block in workflow["final"]["messages"]:
        if block["kind"] == "core_context":
            messages.extend(
                message for message in caller_messages if isinstance(message, dict)
            )
            continue
        content = _render(block["content"], scope)
        if isinstance(content, str) and content:
            messages.append({"role": block["role"], "content": content})
    return messages


def run_workflow(
    http: httpx.Client,
    workflow: dict[str, Any],
    premodel: Any,
    caller_messages: list[Any],
    route_lookup: Callable[[int], Any],
    request_context: dict[str, Any],
) -> WorkflowResult:
    """Execute the chain and return the final message list plus folded usage."""
    scope = WorkflowScope(
        {
            "core_context": json.dumps(caller_messages, ensure_ascii=False),
            "system_request": json.dumps(
                {
                    "method": str(request_context.get("method") or ""),
                    "path": str(request_context.get("path") or ""),
                    "headers": _filtered_request_headers(
                        dict(request_context.get("headers") or {})
                    ),
                },
                ensure_ascii=False,
            ),
        }
    )
    usage = {"prompt": 0, "completion": 0}
    images = extract_image_urls(caller_messages)
    reasoning_entries: list[tuple[str, str]] = []

    for node in workflow["nodes"]:
        route = route_lookup(node["route_id"]) if node["route_id"] else None
        if route is None and premodel["route_id"]:
            route = route_lookup(premodel["route_id"])
        if route is None:
            raise WorkflowNodeError(node["name"], "The node's connection no longer exists.")
        reasoning = _run_node(http, node, route, premodel, scope, usage, images)
        if reasoning:
            reasoning_entries.append((node["name"], reasoning))
        if node["type"] == "image_captioner" and images and node.get("strip_images"):
            # Opt-in per node: the captioner consumed the images, so core context
            # (and the final splice) must not carry them upstream a second time.
            caller_messages = strip_images(caller_messages)
            scope.set("core_context", json.dumps(caller_messages, ensure_ascii=False))
            images = extract_image_urls(caller_messages)

    result_reasoning = ""
    if workflow.get("reasoning"):
        result_reasoning = format_node_reasoning(reasoning_entries)

    return WorkflowResult(
        _build_final_messages(workflow, caller_messages, scope),
        workflow["final"]["model_override"],
        usage,
        result_reasoning,
    )
