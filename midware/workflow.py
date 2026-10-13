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

import asyncio
import contextlib
import json
import re
import time
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
JS_NODE_TIMEOUT_DEFAULT = 5
JS_NODE_TIMEOUT_MAX = 30
JS_MEMORY_LIMIT_BYTES = 16 * 1024 * 1024
JS_FETCH_TIMEOUT_SECONDS = 10.0
JS_FETCH_MAX_BYTES = 1_000_000
JS_CONTEXT_TRIM_MAX = 1000

NODE_TYPES = (
    "api_request",
    "image_captioner",
    "environment",
    "context",
    "assure",
    "javascript",
)
REQUEST_NODE_TYPES = ("api_request", "image_captioner")
IMAGE_MODES = ("batch", "individual")
ENV_ACTIONS = ("time",)
ENV_ACTION_LABELS = {"time": "Declare Time"}
ASSURE_OPERATORS = (
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "greater",
    "less",
    "empty",
    "not_empty",
)
ASSURE_UNARY_OPERATORS = ("empty", "not_empty")
FINAL_ROLES = ("system", "user", "assistant")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
CALL_MACRO_RE = re.compile(
    r"\{\{\s*(?P<name>[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)+)\s*"
    r"\(\s*(?P<q>['\"])(?P<arg>[^'\"]*)(?P=q)\s*\)\s*\}\}"
)
CALL_FALLBACK_RE = re.compile(
    r"@@\s*(?P<name>[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)+)\s*"
    r"\(\s*(?P<q>['\"])(?P<arg>[^'\"]*)(?P=q)\s*\)\s*@@"
)
STRIPPED_REQUEST_HEADERS = HOP_BY_HOP | {"authorization", "x-api-key"}

# The JavaScript node's runtime. Everything crosses the Python/JS boundary as a
# JSON string because mini-racer only converts primitives and JS handles on
# calls; midware.fetch is a blocking Python callable surfaced as an async JS
# function (wrap_py_function is async-only), so scripts may `await` it. The
# head defines console/midware/APIRequest/APIImageRequest at top level (user
# code is concatenated between head and tail, so their `run` and any APIRequest
# or APIImageRequest subclasses close over the same scope), and Python-side
# failures are returned as {"error": ...} JSON — exceptions raised inside the
# bridge come back masked as "Error running Python function".
_JS_BOOTSTRAP_HEAD = """
var __mw_out = { vars: {}, log: [] };
var console = {
  log: (...a) => __mw_out.log.push(
    a.map(x => {
      if (typeof x === "string") { return x; }
      try { return JSON.stringify(x); } catch (e) { return String(x); }
    }).join(" ")
  ),
};
console.warn = console.log;
console.error = console.log;
let __mw_chat = null;
let __mw_image = null;
class APIRequest {
  constructor() {
    this.model = "";
    this.temperature = null;
    this.max_tokens = null;
  }
  async create(params) {
    if (!__mw_chat) { throw new Error("midware.chat is not available in this node"); }
    const body = Object.assign({}, params || {});
    const model = body.model || this.model;
    if (model) { body.model = model; }
    if (body.temperature == null && this.temperature != null) { body.temperature = this.temperature; }
    if (body.max_tokens == null && this.max_tokens != null) { body.max_tokens = this.max_tokens; }
    if (!Array.isArray(body.messages)) { body.messages = []; }
    return __mw_chat(body);
  }
  async ask(text, params) {
    const resp = await this.create(Object.assign({}, params || {}, {
      messages: [{ role: "user", content: String(text) }],
    }));
    const choice = resp && resp.choices && resp.choices[0];
    const msg = choice && choice.message;
    return (msg && msg.content) || "";
  }
}
class APIImageRequest {
  constructor() {
    this.model = "";
    this.size = null;
    this.n = null;
    this.quality = null;
    this.response_format = null;
  }
  async create(params) {
    if (!__mw_image) { throw new Error("image generation is not available in this node"); }
    const body = Object.assign({}, params || {});
    const model = body.model || this.model;
    if (model) { body.model = model; }
    for (const key of ["size", "n", "quality", "response_format"]) {
      if (body[key] == null && this[key] != null) { body[key] = this[key]; }
    }
    return __mw_image(body);
  }
  async ask(text, params) {
    const resp = await this.create(Object.assign({}, params || {}, {
      prompt: String(text),
    }));
    const item = resp && Array.isArray(resp.data) ? resp.data[0] : null;
    if (!item) { return ""; }
    return item.url || item.b64_json || "";
  }
}
async function __mw_main(mwDataJson, fetchFn, chatFn, imageFn) {
  const mwData = JSON.parse(mwDataJson);
  let msgs = mwData.messages;
  __mw_out.vars = {};
  __mw_out.log = [];
  __mw_chat = (params) => chatFn(JSON.stringify(params || {})).then(s => {
    const r = JSON.parse(s);
    if (r && r.error) { throw new Error(r.error); }
    return r;
  });
  __mw_image = imageFn ? (params) => imageFn(JSON.stringify(params || {})).then(s => {
    const r = JSON.parse(s);
    if (r && r.error) { throw new Error(r.error); }
    return r;
  }) : null;
  const midware = {
    get messages() { return msgs; },
    // Earlier nodes' vars merged with this node's own set()s so far, so a
    // script can read back what it just published.
    get vars() { return Object.assign({}, mwData.vars, __mw_out.vars); },
    get systemRequest() { return mwData.systemRequest; },
    setMessages(m) {
      if (!Array.isArray(m)) throw new Error("setMessages expects an array");
      msgs = m;
    },
    set(k, v) { __mw_out.vars[k] = v; },
    fetch: (u, o) => fetchFn(u, JSON.stringify(o || {})).then(s => {
      const r = JSON.parse(s);
      if (r && r.error) { throw new Error(r.error); }
      return r;
    }),
    chat: (params) => __mw_chat(params),
  };
  const res = await run(midware);
  return JSON.stringify({
    result: res === undefined ? null : res,
    vars: __mw_out.vars,
    messages: msgs,
    log: __mw_out.log,
  });
}
"""
_JS_BOOTSTRAP_TAIL = "__mw_main"


class WorkflowError(ValueError):
    """Save/load-time validation failure; the message is shown to the admin."""


class WorkflowNodeError(Exception):
    """A sub-call failed; ``node_name`` identifies the offending node.

    ``detail`` is the full server-side message (persisted on the request row);
    ``public_detail`` is what may be returned to the API client — transport
    exception class names and HTTP status codes are scrubbed so a key holder
    cannot fingerprint the upstream stack. ``usage`` carries any sub-call token
    usage already spent before the failure so rate limits still settle.
    """

    def __init__(
        self,
        node_name: str,
        detail: str,
        status_code: int | None = None,
        public_detail: str | None = None,
        usage: dict[str, int] | None = None,
    ):
        super().__init__(detail)
        self.node_name = node_name
        self.detail = detail
        self.status_code = status_code
        self.public_detail = public_detail if public_detail is not None else detail
        self.usage = usage


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

    node: dict[str, Any] = {
        "type": node_type,
        "name": name,
        "route_id": None,
        "model": "",
        "system_prompt": "",
        "prompt": "",
        "expect_json": False,
        "image_mode": "batch",
        "strip_images": False,
        "max_tokens": None,
        "timeout_seconds": WORKFLOW_NODE_TIMEOUT_DEFAULT,
        "actions": [],
        "left": "",
        "operator": "equals",
        "right": "",
        "use_system_timezone": True,
        "trim_count": 0,
        "trim_drop_system": False,
        "trim_drop_images": False,
        "allow_network": False,
    }

    if node_type in REQUEST_NODE_TYPES:
        route_id = _as_int(raw.get("route_id"))
        if raw.get("route_id") not in (None, "") and route_id is None:
            raise WorkflowError(f"Node '{name}' has an invalid connection.")
        if route_id is not None and route_id < 1:
            route_id = None
        node["route_id"] = route_id

        prompt = raw.get("prompt")
        if not isinstance(prompt, str):
            prompt = "" if prompt is None else str(prompt)
        if not prompt.strip():
            raise WorkflowError(f"Node '{name}' needs a prompt.")
        if len(prompt) > WORKFLOW_MAX_CONTENT:
            raise WorkflowError(f"Node '{name}' prompt exceeds {WORKFLOW_MAX_CONTENT} characters.")
        node["prompt"] = prompt

        system_prompt = raw.get("system_prompt")
        if not isinstance(system_prompt, str):
            system_prompt = "" if system_prompt is None else str(system_prompt)
        if len(system_prompt) > WORKFLOW_MAX_CONTENT:
            raise WorkflowError(
                f"Node '{name}' system prompt exceeds {WORKFLOW_MAX_CONTENT} characters."
            )
        node["system_prompt"] = system_prompt

        model = str(raw.get("model") or "").strip()
        if len(model) > WORKFLOW_MAX_MODEL:
            raise WorkflowError(f"Node '{name}' model name is too long.")
        node["model"] = model

        max_tokens = _as_int(raw.get("max_tokens"))
        if max_tokens is not None and max_tokens < 1:
            max_tokens = None
        node["max_tokens"] = max_tokens

        timeout = _as_int(raw.get("timeout_seconds"))
        if not timeout or timeout < 1:
            timeout = WORKFLOW_NODE_TIMEOUT_DEFAULT
        node["timeout_seconds"] = min(timeout, WORKFLOW_NODE_TIMEOUT_MAX)

        image_mode = str(raw.get("image_mode") or "batch").strip().lower()
        if image_mode not in IMAGE_MODES:
            raise WorkflowError(
                f"Node '{name}' has unknown image mode '{image_mode}' "
                f"(expected {' or '.join(IMAGE_MODES)})."
            )
        node["image_mode"] = image_mode

        if node_type == "api_request":
            node["expect_json"] = bool(raw.get("expect_json"))
        else:
            node["strip_images"] = bool(raw.get("strip_images"))

    elif node_type == "environment":
        actions_raw = raw.get("actions")
        if not isinstance(actions_raw, list) or not actions_raw:
            raise WorkflowError(f"Environment node '{name}' needs at least one action.")
        actions: list[str] = []
        for action in actions_raw:
            action = str(action).strip().lower()
            if action not in ENV_ACTIONS:
                raise WorkflowError(f"Environment node '{name}' has unknown action '{action}'.")
            if action not in actions:
                actions.append(action)
        node["actions"] = actions
        if "use_system_timezone" in raw:
            node["use_system_timezone"] = bool(raw.get("use_system_timezone"))

    elif node_type == "context":
        trim_count = _as_int(raw.get("trim_count"))
        if trim_count is None or trim_count < 0:
            trim_count = 0
        node["trim_count"] = min(trim_count, JS_CONTEXT_TRIM_MAX)
        node["trim_drop_system"] = bool(raw.get("trim_drop_system"))
        node["trim_drop_images"] = bool(raw.get("trim_drop_images"))

    elif node_type == "javascript":
        script = raw.get("prompt")
        if not isinstance(script, str):
            script = "" if script is None else str(script)
        if not script.strip():
            raise WorkflowError(
                f"JavaScript node '{name}' needs a script defining run(midware)."
            )
        if len(script) > WORKFLOW_MAX_CONTENT:
            raise WorkflowError(f"JavaScript node '{name}' script is too long.")
        node["prompt"] = script
        node["allow_network"] = bool(raw.get("allow_network"))
        timeout = _as_int(raw.get("timeout_seconds"))
        if not timeout or timeout < 1:
            timeout = JS_NODE_TIMEOUT_DEFAULT
        node["timeout_seconds"] = min(timeout, JS_NODE_TIMEOUT_MAX)
        # Optional connection + default model for midware.chat / APIRequest /
        # APIImageRequest.
        route_id = _as_int(raw.get("route_id"))
        if raw.get("route_id") not in (None, "") and route_id is None:
            raise WorkflowError(f"Node '{name}' has an invalid connection.")
        if route_id is not None and route_id < 1:
            route_id = None
        node["route_id"] = route_id
        model = str(raw.get("model") or "").strip()
        if len(model) > WORKFLOW_MAX_MODEL:
            raise WorkflowError(f"Node '{name}' model name is too long.")
        node["model"] = model

    else:  # assure
        left = raw.get("left")
        if not isinstance(left, str):
            left = "" if left is None else str(left)
        if not left.strip():
            raise WorkflowError(f"Assure node '{name}' needs a left-hand value.")
        if len(left) > WORKFLOW_MAX_CONTENT:
            raise WorkflowError(f"Assure node '{name}' left-hand value is too long.")
        node["left"] = left

        operator = str(raw.get("operator") or "equals").strip().lower()
        if operator not in ASSURE_OPERATORS:
            raise WorkflowError(f"Assure node '{name}' has unknown operator '{operator}'.")
        node["operator"] = operator

        right = raw.get("right")
        if not isinstance(right, str):
            right = "" if right is None else str(right)
        if len(right) > WORKFLOW_MAX_CONTENT:
            raise WorkflowError(f"Assure node '{name}' right-hand value is too long.")
        if operator not in ASSURE_UNARY_OPERATORS and not right.strip():
            raise WorkflowError(f"Assure node '{name}' needs a right-hand value.")
        node["right"] = right

    return node


def _collect_reference_paths(text: Any) -> list[str]:
    """Every ``{{ name }}`` / ``@@name@@`` path mentioned in a template string."""
    if not isinstance(text, str) or not text:
        return []
    paths: list[str] = []
    residue = text
    for regex in (CALL_MACRO_RE, CALL_FALLBACK_RE):
        for match in regex.finditer(residue):
            paths.append(match.group("name").strip())
        residue = regex.sub("", residue)
    for regex in (MACRO_RE, FALLBACK_MACRO_RE):
        for match in regex.finditer(residue):
            name = match.group("name").strip()
            if name:
                paths.append(name)
    return paths


def _known_reference_paths(nodes: list[dict[str, Any]]) -> set[str]:
    """Paths a template may reference after ``nodes`` have run."""
    known: set[str] = {"core_context", "system_request"}
    for node in nodes:
        name = node["name"]
        known.add(name)
        if node["type"] in REQUEST_NODE_TYPES:
            known.add(f"{name}.content")
            known.add(f"{name}.reasoning")
            if node["type"] == "image_captioner":
                known.add(f"{name}.count")
        elif node["type"] == "environment" and "time" in node["actions"]:
            known.add(f"{name}.time")
            known.add(f"{name}.time.unix")
            known.add(f"{name}.time.formatted")
        elif node["type"] == "context":
            for key in (
                "count",
                "user_count",
                "assistant_count",
                "image_count",
                "chars",
                "text",
                "last_user",
                "last_assistant",
                "first_user",
                "trimmed",
            ):
                known.add(f"{name}.{key}")
    return known


def _lifted_reference_names(nodes: list[dict[str, Any]]) -> set[str]:
    """Nodes whose keys are not knowable in advance; dotted paths under them are free."""
    return {
        node["name"]
        for node in nodes
        if (node["type"] == "api_request" and node["expect_json"])
        or node["type"] == "javascript"
    }


def _check_reference_paths(
    known: set[str], lifted: set[str], path: str, where: str
) -> None:
    if path in known:
        return
    if path.split(".", 1)[0] in lifted:
        return
    raise WorkflowError(f"{where} references unknown variable '{{{{ {path} }}}}'.")


def _validate_references(result: dict[str, Any]) -> None:
    """Save-time type checking: every ``{{ path }}`` must exist at its position.

    A node's templates only see the origin variables plus variables published
    by *earlier* nodes; Expect JSON nodes lift the check for any dotted path
    under them. The Final Request sees every node's variables.
    """
    nodes = result["nodes"]
    for index, node in enumerate(nodes):
        if node["type"] == "javascript":
            # The script is code, not a template; its strings may legitimately
            # contain {{ ... }} literals meant for later expansion.
            continue
        prior = nodes[:index]
        known = _known_reference_paths(prior)
        lifted = _lifted_reference_names(prior)
        where = f"Node '{node['name']}'"
        for field in ("system_prompt", "prompt", "model", "left", "right"):
            for path in _collect_reference_paths(node.get(field)):
                _check_reference_paths(known, lifted, path, where)
    known = _known_reference_paths(nodes)
    lifted = _lifted_reference_names(nodes)
    for block in result["final"]["messages"]:
        if block["kind"] != "text":
            continue
        for path in _collect_reference_paths(block["content"]):
            _check_reference_paths(known, lifted, path, "Final Request")


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

    result = {
        "version": 1,
        "reasoning": bool(raw.get("reasoning")),
        "final": {"messages": blocks, "model_override": override},
        "nodes": nodes,
    }
    _validate_references(result)
    return result


def load_workflow(raw: Any) -> dict[str, Any]:
    """Runtime load of a stored blob (defensive re-validation)."""
    return normalize_workflow(raw)


# -- variable scope ----------------------------------------------------------


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


class WorkflowScope:
    """Variable table with dotted lookup; nesting may go arbitrarily deep."""

    def __init__(self, variables: dict[str, Any] | None = None):
        self._vars: dict[str, Any] = dict(variables or {})

    def set(self, name: str, value: Any) -> None:
        self._vars[name] = value

    def snapshot(self) -> dict[str, Any]:
        """Shallow copy of the variable table (for the JavaScript node's mwData)."""
        return dict(self._vars)

    def resolve(self, name: str) -> tuple[Any, bool]:
        """Raw value for ``node.key...``; ``(None, False)`` when absent."""
        parts = name.strip().split(".")
        if not parts or not parts[0] or parts[0] not in self._vars:
            return None, False
        value = self._vars[parts[0]]
        for part in parts[1:]:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return None, False
        return value, True

    def lookup(self, name: str) -> str | None:
        value, found = self.resolve(name)
        if not found or value is None or callable(value):
            return None
        return _stringify(value)


def _render(text: Any, scope: WorkflowScope) -> Any:
    """Expand ``{{ name }}`` / ``@@name@@`` against the scope; unknown = literal.

    Call syntax ``{{ path("argument") }}`` invokes a callable stored in the
    scope (currently only ``<node>.time.formatted``); an unknown path or a
    non-callable target stays literal, as does a bare reference to a callable.
    """
    if not isinstance(text, str) or not text:
        return text

    def call_replace(match: re.Match[str]) -> str:
        value, found = scope.resolve(match.group("name"))
        if not found or not callable(value):
            return match.group(0)
        try:
            result = value(match.group("arg"))
        except (ValueError, TypeError):
            return match.group(0)
        return _stringify(result) if not isinstance(result, str) else result

    def replace(match: re.Match[str]) -> str:
        value, found = scope.resolve(match.group("name"))
        if not found or value is None or callable(value):
            return match.group(0)
        return _stringify(value)

    if "{{" in text:
        text = CALL_MACRO_RE.sub(call_replace, text)
        text = MACRO_RE.sub(replace, text)
    if "@@" in text:
        text = CALL_FALLBACK_RE.sub(call_replace, text)
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
    if node["type"] == "environment":
        return _run_environment(node, scope)
    if node["type"] == "assure":
        return _run_assure(node, scope)
    if node["type"] == "image_captioner":
        return _run_image_captioner(http, node, route, premodel, scope, usage, images)
    return _run_api_request(http, node, route, premodel, scope, usage)


_TIMESTAMP_TOKENS = (
    ("YYYY", "%Y"),
    ("YY", "%y"),
    ("MM", "%m"),
    ("DD", "%d"),
    ("HH", "%H"),
    ("MN", "%M"),
    ("SE", "%S"),
)


def _format_timestamp(unix: int, fmt: str, use_system: bool = True) -> str:
    """Render ``unix`` with YY/YYYY/MM/DD/HH/MN/SE tokens.

    ``use_system`` picks the machine's local timezone (default) or UTC. A
    token is only substituted at non-letter boundaries, so surrounding text
    (``SECONDS``, ``DDOS``) stays literal; everything that is not a token
    passes through unchanged.
    """
    local = time.localtime(unix) if use_system else time.gmtime(unix)
    out: list[str] = []
    index = 0
    while index < len(fmt):
        for token, directive in _TIMESTAMP_TOKENS:
            if not fmt.startswith(token, index):
                continue
            before = fmt[index - 1] if index > 0 else ""
            next_index = index + len(token)
            after = fmt[next_index] if next_index < len(fmt) else ""
            if before.isalpha() or after.isalpha():
                continue
            out.append(time.strftime(directive, local))
            index = next_index
            break
        else:
            out.append(fmt[index])
            index += 1
    return "".join(out)


def _run_environment(node: dict[str, Any], scope: WorkflowScope) -> str:
    """Publish environment variables; no HTTP, no usage."""
    variables: dict[str, Any] = {}
    exposed: list[str] = []
    for action in node["actions"]:
        if action == "time":
            unix = int(time.time())
            use_sys = node.get("use_system_timezone", True)
            variables["time"] = {
                "unix": unix,
                "formatted": lambda fmt, ts=unix, sys_tz=use_sys: _format_timestamp(
                    ts, str(fmt), use_system=sys_tz
                ),
            }
            exposed.append(f"Declare time: {unix}")
    scope.set(node["name"], variables)
    return "\n".join(exposed)


def _assure_check(operator: str, left: str, right: str) -> bool:
    if operator == "equals":
        return left == right
    if operator == "not_equals":
        return left != right
    if operator == "contains":
        return right in left
    if operator == "not_contains":
        return right not in left
    if operator == "starts_with":
        return left.startswith(right)
    if operator == "ends_with":
        return left.endswith(right)
    if operator in ("greater", "less"):
        try:
            left_num: Any = float(left)
            right_num: Any = float(right)
        except ValueError:
            left_num, right_num = left, right
        if operator == "greater":
            return left_num > right_num
        return left_num < right_num
    if operator == "empty":
        return not left.strip()
    if operator == "not_empty":
        return bool(left.strip())
    return False


def _run_assure(node: dict[str, Any], scope: WorkflowScope) -> str:
    """Guard clause: a failed comparison aborts the whole workflow."""
    left = _render(node["left"], scope)
    right = _render(node["right"], scope)
    left = left if isinstance(left, str) else _stringify(left)
    right = right if isinstance(right, str) else _stringify(right)
    if not _assure_check(node["operator"], left, right):
        raise WorkflowNodeError(node["name"], "Assurance test failed.")
    return f"Passed {node['name']} Assurance Test"


# -- context node ------------------------------------------------------------


def _message_text(message: dict[str, Any]) -> str:
    """Plain text of one message; images become ``[image]`` placeholders."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                chunks.append(str(part.get("text") or ""))
            elif part.get("type") in ("image_url", "image"):
                chunks.append("[image]")
        return "".join(chunks)
    return ""


def _flatten_messages_text(messages: list[dict[str, Any]]) -> str:
    lines = [
        f"{message.get('role') or '?'}: {_message_text(message)}" for message in messages
    ]
    return "\n".join(lines)


def _reshape_messages(
    messages: list[dict[str, Any]],
    keep_last: int = 0,
    drop_system: bool = False,
    drop_images: bool = False,
) -> list[dict[str, Any]]:
    result = [message for message in messages if isinstance(message, dict)]
    if drop_system:
        result = [message for message in result if message.get("role") != "system"]
    if keep_last > 0:
        result = result[-keep_last:]
    if drop_images:
        result = strip_images(result)
    return result


def _run_context(node: dict[str, Any], scope: WorkflowScope, caller_messages: list[Any]) -> str:
    """Publish conversation stats/extracts; no HTTP, no usage."""
    messages = [message for message in caller_messages if isinstance(message, dict)]
    text = _flatten_messages_text(messages)
    user_count = sum(1 for message in messages if message.get("role") == "user")
    assistant_count = sum(1 for message in messages if message.get("role") == "assistant")
    image_count = len(extract_image_urls(messages))
    last_user = next(
        (
            _message_text(message)
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )
    first_user = next(
        (_message_text(message) for message in messages if message.get("role") == "user"),
        "",
    )
    last_assistant = next(
        (
            _message_text(message)
            for message in reversed(messages)
            if message.get("role") == "assistant"
        ),
        "",
    )
    trimmed = _reshape_messages(
        messages,
        keep_last=node.get("trim_count") or 0,
        drop_system=bool(node.get("trim_drop_system")),
        drop_images=bool(node.get("trim_drop_images")),
    )
    scope.set(
        node["name"],
        {
            "count": len(messages),
            "user_count": user_count,
            "assistant_count": assistant_count,
            "image_count": image_count,
            "chars": len(text),
            "text": text,
            "last_user": last_user,
            "last_assistant": last_assistant,
            "first_user": first_user,
            "trimmed": json.dumps(trimmed, ensure_ascii=False),
        },
    )
    return (
        f"Inspected context: {len(messages)} messages "
        f"({user_count} user / {assistant_count} assistant, {image_count} images)"
    )


# -- javascript node ---------------------------------------------------------


def _load_mini_racer():
    """Import the optional V8 engine; pinned because we touch a private attr."""
    try:
        import py_mini_racer
    except ImportError as exc:
        raise WorkflowError(
            "JavaScript nodes need the optional 'mini-racer' package "
            "(pip install 'mini-racer>=0.14,<0.15')."
        ) from exc
    return py_mini_racer


@contextlib.contextmanager
def _bind_py_function(mr, fn):
    """Sync driver for mini-racer's async-only ``wrap_py_function``.

    The private ``_ctx.event_loop`` access is why the dependency is pinned;
    exceptions are re-raised on the caller's thread after the async CM exits.
    """
    cm = mr.wrap_py_function(fn)
    loop = mr._ctx.event_loop
    js_fn = asyncio.run_coroutine_threadsafe(cm.__aenter__(), loop).result()
    try:
        yield js_fn
    except BaseException as exc:
        asyncio.run_coroutine_threadsafe(
            cm.__aexit__(type(exc), exc, None), loop
        ).result()
    else:
        asyncio.run_coroutine_threadsafe(cm.__aexit__(None, None, None), loop).result()


def _system_request_payload(request_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": str(request_context.get("method") or ""),
        "path": str(request_context.get("path") or ""),
        "headers": _filtered_request_headers(dict(request_context.get("headers") or {})),
    }


def _json_safe(value: Any) -> Any:
    """Strip callables/non-JSON values so the scope snapshot can be dumped."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _json_safe(inner)
            for key, inner in value.items()
            if not callable(inner)
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(inner) for inner in value]
    if callable(value):
        return None
    return str(value)


def _js_polish(value: Any) -> Any:
    """Integral floats (``2.0``) become ints so templates render ``2``, not ``2.0``."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_js_polish(inner) for inner in value]
    if isinstance(value, dict):
        return {str(key): _js_polish(inner) for key, inner in value.items()}
    return value


# Transport exception class names (ConnectError, ReadTimeout, …) and upstream
# HTTP status codes are scrubbed from the client-facing node error so a key
# holder cannot fingerprint the upstream stack; the full detail stays on the
# request row for the admin.
_HTTP_STATUS_RE = re.compile(r"HTTP \d{3}")
_EXCEPTION_NAME_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Timeout)\b"
)


def _public_error_detail(detail: str) -> str:
    scrubbed = _HTTP_STATUS_RE.sub("an HTTP error", detail)
    scrubbed = _EXCEPTION_NAME_RE.sub("an error", scrubbed)
    return re.sub(r"\s+", " ", scrubbed).strip()


def _make_js_fetch(http: httpx.Client, allow_network: bool):
    """Blocking ``midware.fetch`` bridge (async only to satisfy wrap_py_function)."""

    async def js_fetch(url: str, opts_json: str) -> str:

        def refuse(message: str) -> str:
            return json.dumps({"error": message})

        if not allow_network:
            return refuse(
                "network is disabled for this node (Allow network is unticked)."
            )
        url = str(url)
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme not in ("http", "https"):
            return refuse("midware.fetch only supports http(s) URLs.")
        try:
            opts = json.loads(opts_json) if opts_json else {}
        except (ValueError, TypeError):
            opts = {}
        if not isinstance(opts, dict):
            opts = {}
        method = str(opts.get("method") or "GET").upper()
        body = opts.get("body")
        raw_headers = opts.get("headers")
        headers = (
            {str(key): str(value) for key, value in raw_headers.items()}
            if isinstance(raw_headers, dict)
            else None
        )
        try:
            response = http.request(
                method,
                url,
                content=body if isinstance(body, str) else None,
                headers=headers,
                timeout=JS_FETCH_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            return refuse(f"fetch failed: {type(exc).__name__}")
        content = response.content[:JS_FETCH_MAX_BYTES]
        return json.dumps(
            {
                "status": response.status_code,
                "ok": 200 <= response.status_code < 300,
                "body": content.decode("utf-8", errors="replace"),
                "headers": dict(response.headers),
            },
            ensure_ascii=False,
        )

    return js_fetch


# Carried from an APIRequest/midware.chat payload into the upstream body. Only
# well-known sampling knobs pass through; everything else (model, messages,
# stream) is handled explicitly so a script can't smuggle odd fields upstream.
_CHAT_PARAM_KEYS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "logit_bias",
    "user",
    "response_format",
    "tools",
    "tool_choice",
)


def _make_js_chat(
    http: httpx.Client,
    node: dict[str, Any],
    route: Any,
    premodel: Any,
    usage: dict[str, int],
):
    """Server-side chat bridge for ``midware.chat`` / ``APIRequest.create``.

    Goes through the node's connection (the route's host + upstream key never
    reach the sandbox), folds usage into the request's totals, and returns the
    full parsed response. ``allow_network`` does not gate it — like an API
    Request node it is MidWare routing, not arbitrary internet.
    """

    async def js_chat(params_json: str) -> str:

        def refuse(message: str) -> str:
            return json.dumps({"error": message})

        try:
            params = json.loads(params_json) if params_json else {}
        except (ValueError, TypeError):
            return refuse("chat params must be valid JSON.")
        if not isinstance(params, dict):
            return refuse("chat params must be an object.")
        if route is None:
            return refuse(
                "This JavaScript node has no connection; pick one to call a model."
            )
        raw_model = params.get("model")
        if raw_model is not None and not isinstance(raw_model, str):
            return refuse("model must be a string.")
        model = (
            (raw_model or "").strip()
            or node.get("model")
            or (premodel["model"] or "").strip()
        )
        if not model:
            return refuse("No model set for this chat call.")
        messages = params.get("messages")
        if not isinstance(messages, list) or not messages:
            return refuse("chat needs a non-empty messages array.")
        for message in messages:
            if not isinstance(message, dict):
                return refuse("every chat message must be an object.")
            if not isinstance(message.get("role"), str) or not message["role"]:
                return refuse("every chat message needs a string role.")
            content = message.get("content")
            if not isinstance(content, (str, list)):
                return refuse("chat message content must be a string or an array.")

        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        for key in _CHAT_PARAM_KEYS:
            if key in params and params[key] is not None:
                payload[key] = params[key]

        try:
            response = http.post(
                upstream_url(route, "chat/completions"),
                json=payload,
                headers=_node_headers(route),
                timeout=float(node.get("timeout_seconds") or JS_NODE_TIMEOUT_DEFAULT),
            )
        except httpx.HTTPError as exc:
            return refuse(f"chat request failed: {type(exc).__name__}")
        if response.status_code >= 400:
            return refuse(f"chat request returned HTTP {response.status_code}.")
        try:
            data = response.json()
        except (ValueError, TypeError):
            return refuse("chat request did not return JSON.")
        if not isinstance(data, dict):
            # A list/number response has no choices to read; surface it as an
            # error rather than letting ask() silently return "".
            return refuse("chat request returned an unexpected JSON shape.")
        node_usage = data.get("usage")
        if isinstance(node_usage, dict):
            usage["prompt"] += max(0, _as_int(node_usage.get("prompt_tokens")) or 0)
            usage["completion"] += max(
                0, _as_int(node_usage.get("completion_tokens")) or 0
            )
        return json.dumps(data, ensure_ascii=False)

    return js_chat


# Carried from an APIImageRequest/midware image payload into the upstream
# body; model and prompt are handled explicitly like chat's model/messages.
_IMAGE_PARAM_KEYS = (
    "n",
    "size",
    "quality",
    "response_format",
    "style",
    "background",
    "output_format",
    "output_compression",
    "moderation",
    "seed",
    "user",
)


def _make_js_image(
    http: httpx.Client,
    node: dict[str, Any],
    route: Any,
    usage: dict[str, int],
):
    """Server-side image bridge for ``APIImageRequest.create``.

    Same contract as the chat bridge. The premodel's model is deliberately not
    a fallback: premodels are chat-only, so using one would ship a chat model id
    to the image endpoint; the model comes from the call or the node instead.
    """

    async def js_image(params_json: str) -> str:

        def refuse(message: str) -> str:
            return json.dumps({"error": message})

        try:
            params = json.loads(params_json) if params_json else {}
        except (ValueError, TypeError):
            return refuse("image params must be valid JSON.")
        if not isinstance(params, dict):
            return refuse("image params must be an object.")
        if route is None:
            return refuse(
                "This JavaScript node has no connection; pick one to generate images."
            )
        raw_model = params.get("model")
        if raw_model is not None and not isinstance(raw_model, str):
            return refuse("model must be a string.")
        model = (raw_model or "").strip() or node.get("model")
        if not model:
            return refuse("No model set for this image request.")
        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return refuse("image generation needs a non-empty prompt.")

        payload: dict[str, Any] = {"model": model, "prompt": prompt}
        for key in _IMAGE_PARAM_KEYS:
            if key in params and params[key] is not None:
                payload[key] = params[key]

        try:
            response = http.post(
                upstream_url(route, "images/generations"),
                json=payload,
                headers=_node_headers(route),
                timeout=float(node.get("timeout_seconds") or JS_NODE_TIMEOUT_DEFAULT),
            )
        except httpx.HTTPError as exc:
            return refuse(f"image request failed: {type(exc).__name__}")
        if response.status_code >= 400:
            return refuse(f"image request returned HTTP {response.status_code}.")
        try:
            data = response.json()
        except (ValueError, TypeError):
            return refuse("image request did not return JSON.")
        if not isinstance(data, dict):
            return refuse("image request returned an unexpected JSON shape.")
        node_usage = data.get("usage")
        if isinstance(node_usage, dict):
            prompt_tokens = node_usage.get("input_tokens")
            if prompt_tokens is None:
                prompt_tokens = node_usage.get("prompt_tokens")
            completion_tokens = node_usage.get("output_tokens")
            if completion_tokens is None:
                completion_tokens = node_usage.get("completion_tokens")
            usage["prompt"] += max(0, _as_int(prompt_tokens) or 0)
            usage["completion"] += max(0, _as_int(completion_tokens) or 0)
        return json.dumps(data, ensure_ascii=False)

    return js_image


def _run_javascript(
    node: dict[str, Any],
    scope: WorkflowScope,
    caller_messages: list[Any],
    request_context: dict[str, Any],
    http: httpx.Client,
    route: Any,
    premodel: Any,
    usage: dict[str, int],
) -> tuple[str, list[dict[str, Any]] | None]:
    """Execute the node's script; returns (reasoning lines, rewritten messages|None)."""
    try:
        py_mini_racer = _load_mini_racer()
    except WorkflowError as exc:
        raise WorkflowNodeError(node["name"], str(exc)) from exc
    mw_data = json.dumps(
        {
            "messages": caller_messages,
            "systemRequest": _system_request_payload(request_context),
            "vars": _json_safe(scope.snapshot()),
        },
        ensure_ascii=False,
    )
    script = _JS_BOOTSTRAP_HEAD + "\n" + node["prompt"] + "\n" + _JS_BOOTSTRAP_TAIL
    timeout = node.get("timeout_seconds") or JS_NODE_TIMEOUT_DEFAULT
    outcome: dict[str, Any] = {}
    js_fetch = _make_js_fetch(http, bool(node.get("allow_network")))
    js_chat = _make_js_chat(http, node, route, premodel, usage)
    js_image = _make_js_image(http, node, route, usage)
    # mini-racer's context managers can consume exceptions raised in the body,
    # so nothing may escape the `with` blocks unrecorded.
    try:
        with py_mini_racer.MiniRacer() as mr:
            mr.set_hard_memory_limit(JS_MEMORY_LIMIT_BYTES)
            with _bind_py_function(mr, js_fetch) as fetch_fn, _bind_py_function(
                mr, js_chat
            ) as chat_fn, _bind_py_function(mr, js_image) as image_fn:
                phase = "compile"
                try:
                    main = mr.eval(script, timeout_sec=5)
                    phase = "run"
                    out = main(mw_data, fetch_fn, chat_fn, image_fn, timeout_sec=timeout)
                    if hasattr(out, "get"):
                        out = out.get(timeout=timeout + 1)
                    outcome["data"] = json.loads(out) if isinstance(out, str) else None
                except BaseException as exc:
                    outcome["error"] = exc
                    outcome["phase"] = phase
    except BaseException as exc:
        outcome.setdefault("error", exc)

    error = outcome.get("error")
    if error is not None:
        detail = (str(error).strip() or type(error).__name__).splitlines()[0][:300]
        kind = type(error).__name__
        if kind == "JSTimeoutException":
            # Compile has its own fixed 5 s budget; only the run phase uses
            # the node's configured timeout.
            budget = 5 if outcome.get("phase") == "compile" else timeout
            raise WorkflowNodeError(
                node["name"], f"JavaScript timed out ({budget}s)."
            )
        raise WorkflowNodeError(
            node["name"],
            f"JavaScript failed: {detail}",
            public_detail=f"JavaScript failed: {_public_error_detail(detail)}",
        )

    data = outcome.get("data")
    if not isinstance(data, dict):
        raise WorkflowNodeError(node["name"], "JavaScript returned no result.")

    variables: dict[str, Any] = {}
    raw_vars = data.get("vars")
    if isinstance(raw_vars, dict):
        for key, value in raw_vars.items():
            variables[str(key)] = _js_polish(value)
    scope.set(node["name"], variables)

    new_messages: list[dict[str, Any]] | None = None
    raw_messages = data.get("messages")
    if isinstance(raw_messages, list):
        new_messages = [
            message for message in raw_messages if isinstance(message, dict)
        ]

    lines: list[str] = []
    raw_log = data.get("log")
    if isinstance(raw_log, list):
        lines.extend(str(entry) for entry in raw_log)
    result = data.get("result")
    if result is not None:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if text.strip():
            lines.append(f"Returned: {text[:500]}")
    return "\n".join(lines), new_messages


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
                _system_request_payload(request_context), ensure_ascii=False
            ),
        }
    )
    usage = {"prompt": 0, "completion": 0}
    images = extract_image_urls(caller_messages)
    reasoning_entries: list[tuple[str, str]] = []

    try:
        for node in workflow["nodes"]:
            if node["type"] == "context":
                reasoning = _run_context(node, scope, caller_messages)
                reasoning_entries.append((node["name"], reasoning))
                continue
            if node["type"] == "javascript":
                # Optional connection for midware.chat / APIRequest /
                # APIImageRequest; a node that only computes needs none, so a
                # missing route is not an error here.
                route = None
                if node["route_id"]:
                    route = route_lookup(node["route_id"])
                if route is None and premodel["route_id"]:
                    route = route_lookup(premodel["route_id"])
                reasoning, new_messages = _run_javascript(
                    node, scope, caller_messages, request_context, http, route, premodel, usage
                )
                if reasoning:
                    reasoning_entries.append((node["name"], reasoning))
                if new_messages is not None:
                    # Same semantics as the captioner's strip: a rewritten context
                    # is what the Final Request splices and later nodes see.
                    caller_messages = new_messages
                    scope.set("core_context", json.dumps(caller_messages, ensure_ascii=False))
                    images = extract_image_urls(caller_messages)
                continue
            route = None
            if node["type"] in REQUEST_NODE_TYPES:
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
    except WorkflowNodeError as exc:
        # Sub-call tokens already spent are real; carry them so rate limits
        # settle even though the request errored (the row itself stays at zero).
        if exc.usage is None:
            exc.usage = dict(usage)
        raise

    result_reasoning = ""
    if workflow.get("reasoning"):
        result_reasoning = format_node_reasoning(reasoning_entries)

    return WorkflowResult(
        _build_final_messages(workflow, caller_messages, scope),
        workflow["final"]["model_override"],
        usage,
        result_reasoning,
    )
