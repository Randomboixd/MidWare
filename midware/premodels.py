"""Premodels: named overlays that bundle a route, a model, and a prompt preset.

A premodel is addressed in a request's ``model`` field as ``<p>-slug``. When
MidWare sees that prefix it resolves the bundled route and model, merges the
premodel's prompt inventory into the caller's messages (honoring the chosen
merge mode), and expands MWVAR macros.

Two prompt modes are supported:

* ``simple`` - one system prompt.
* ``sillytavern`` - a parsed SillyTavern preset. Enabled prompts keep their
  role, so system prompts land in the system block and user/assistant prompts
  are injected ``injection_depth`` messages from the end of the chat.

MWVAR (macro variable) messages carry ``@name=value`` definitions. The defining
message is removed from the conversation (along with any later MWVAR messages)
before substitution, and every definition also exposes a capitalized and an
uppercased spelling of the value.
"""

from __future__ import annotations

import json
import re
from typing import Any

PREMODEL_PREFIX_RE = re.compile(r"^\s*<p>-(?P<slug>[A-Za-z0-9._-]+)\s*")
MWVAR_MARKER_RE = re.compile(r"^\s*!!!MWVARS?!!!", re.IGNORECASE)
MWVAR_LINE_RE = re.compile(r"^\s*@(?P<name>[^=]+?)\s*=\s*(?P<value>.*)$")
MACRO_RE = re.compile(r"\{\{\s*(?P<name>[^{}]+?)\s*\}\}")
FALLBACK_MACRO_RE = re.compile(r"@@\s*(?P<name>[^@]+?)\s*@@")

MERGE_MODES = ("caller", "premodel", "append")
PROMPT_MODES = ("simple", "sillytavern")
DESCRIPTION_MAX = 250

# Sampling parameters a premodel can inject when the caller did not set them.
SAMPLING_KEYS = ("temperature", "top_p", "max_tokens")


def slugify(value: str) -> str:
    """Lowercase, dash-separated slug safe to type after ``<p>-``."""
    text = (value or "").strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"[^a-z0-9.-]+", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-.")


def normalize_slug(raw: str, name: str = "") -> str:
    return slugify(raw) or slugify(name)


def extract_premodel_prefix(model: Any) -> tuple[str | None, str]:
    """Split ``<p>-slug`` off a model string, returning ``(slug, remainder)``."""
    if not isinstance(model, str):
        return None, ""
    match = PREMODEL_PREFIX_RE.match(model)
    if not match:
        return None, model
    return match.group("slug"), model[match.end():]


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_prompt(item: dict, enabled: bool) -> dict[str, Any]:
    role = str(item.get("role") or "").strip().lower()
    system_prompt = bool(item.get("system_prompt"))
    if role not in {"system", "user", "assistant"}:
        role = "system" if system_prompt else "user"
    return {
        "identifier": str(item.get("identifier") or item.get("id") or ""),
        "name": str(item.get("name") or ""),
        "role": role,
        "content": str(item.get("content") or ""),
        "system_prompt": system_prompt,
        "marker": bool(item.get("marker")),
        "injection_position": _as_int(item.get("injection_position"), 0),
        "injection_depth": _as_int(item.get("injection_depth"), 0),
        "injection_order": _as_int(item.get("injection_order"), 100),
        "enabled": bool(enabled),
    }


def normalize_preset(raw: Any) -> dict[str, Any]:
    """Reduce a SillyTavern preset (or an already-normalized one) to prompts.

    ``prompt_order`` wins over per-prompt ``enabled`` flags because that is where
    SillyTavern stores the user's toggles. When neither is present, markers
    default to disabled and everything else to enabled.
    """
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {"prompts": []}
    if not isinstance(raw, dict):
        return {"prompts": []}

    enabled_map: dict[str, bool] = {}
    groups = raw.get("prompt_order")
    if isinstance(groups, list):
        for group in groups:
            entries = group.get("order") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                identifier = item.get("identifier")
                if identifier is not None:
                    enabled_map[str(identifier)] = bool(item.get("enabled", True))

    prompts: list[dict[str, Any]] = []
    raw_prompts = raw.get("prompts")
    if isinstance(raw_prompts, list):
        for item in raw_prompts:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("identifier") or item.get("id") or item.get("name") or "")
            if identifier in enabled_map:
                enabled = enabled_map[identifier]
            elif "enabled" in item:
                enabled = bool(item.get("enabled"))
            else:
                enabled = not bool(item.get("marker"))
            prompts.append(_normalize_prompt(item, enabled))
    return {"prompts": prompts}


# -- MWVAR macros ------------------------------------------------------------


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict)
        )
    return ""


def _is_mwvar_message(message: Any) -> bool:
    """A message carrying only MWVAR definitions.

    Any role is accepted: clients commonly attach the block as a system message
    even though the spec frames it as a user/assistant turn.
    """
    if not isinstance(message, dict):
        return False
    return bool(MWVAR_MARKER_RE.match(_content_text(message.get("content"))))


def parse_mwvar(content: str) -> dict[str, dict[str, str]]:
    """Collect ``@name=value`` definitions from an MWVAR message body."""
    definitions: dict[str, dict[str, str]] = {}
    for line in content.splitlines():
        if MWVAR_MARKER_RE.match(line):
            continue
        match = MWVAR_LINE_RE.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        if not name:
            continue
        definitions[name.casefold()] = {"name": name, "value": match.group("value").rstrip()}
    return definitions


def extract_mwvars(messages: list[Any]) -> tuple[dict[str, dict[str, str]], list[Any]]:
    """Read the first MWVAR message, drop it and every later one.

    Scanning stops after the first definition block: later MWVAR messages are
    removed from the context but their variables are ignored, matching the
    "deleted, and any following mwvars will be deleted too" contract.
    """
    definitions: dict[str, dict[str, str]] = {}
    found = False
    cleaned: list[Any] = []
    for message in messages:
        if _is_mwvar_message(message):
            if not found:
                definitions.update(parse_mwvar(_content_text(message.get("content"))))
                found = True
            continue
        cleaned.append(message)
    if not found:
        return {}, list(messages)
    return definitions, cleaned


def _capitalize_first(value: str) -> str:
    return value[:1].upper() + value[1:]


def _apply_macro(token: str, definitions: dict[str, dict[str, str]], original: str) -> str:
    entry = definitions.get(token.strip().casefold())
    if entry is None:
        return original
    name = entry["name"]
    value = entry["value"]
    if token == name:
        return value
    if token == _capitalize_first(name):
        return _capitalize_first(value)
    if token == name.upper():
        return value.upper()
    return value


def substitute(text: Any, definitions: dict[str, dict[str, str]]) -> Any:
    """Expand ``{{ name }}`` macros and ``@@name@@`` fallbacks.

    The ``@@...@@`` form exists because SillyTavern's own macro engine consumes
    ``{{...}}`` before a request ever leaves the client; the at-sign form passes
    through untouched. Unknown names are left as written.
    """
    if not definitions or not isinstance(text, str):
        return text
    if "{{" in text:
        text = MACRO_RE.sub(
            lambda match: _apply_macro(match.group("name"), definitions, match.group(0)),
            text,
        )
    if "@@" in text:
        text = FALLBACK_MACRO_RE.sub(
            lambda match: _apply_macro(match.group("name"), definitions, match.group(0)),
            text,
        )
    return text


def substitute_messages(messages: list[Any], definitions: dict[str, dict[str, str]]) -> list[Any]:
    if not definitions:
        return messages
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = substitute(content, definitions)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = substitute(part["text"], definitions)
    return messages


# -- sampling parameters -----------------------------------------------------


def parse_params(raw: Any) -> dict[str, float | int]:
    """Coerce a stored params blob into the known numeric sampling keys."""
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    params: dict[str, float | int] = {}
    for key in SAMPLING_KEYS:
        value = raw.get(key)
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            params[key] = value
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        params[key] = int(number) if key == "max_tokens" and number.is_integer() else number
    return params


def apply_sampling_params(payload: Any, premodel) -> Any:
    """Fill in the premodel's sampling values for keys the caller left unset."""
    if not isinstance(payload, dict) or not bool(premodel["params_enabled"]):
        return payload
    for key, value in parse_params(premodel["params_json"]).items():
        if payload.get(key) is None:
            payload[key] = value
    return payload


# -- prompt construction / merging ------------------------------------------


def build_preset_prompts(premodel) -> list[dict[str, Any]]:
    """The enabled prompts a premodel contributes, normalized to a common shape."""
    mode = premodel["prompt_mode"] or "simple"
    if mode == "sillytavern":
        raw = premodel["preset_json"]
        if not raw:
            return []
        try:
            preset = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return [prompt for prompt in normalize_preset(preset)["prompts"] if prompt["enabled"]]

    content = (premodel["system_prompt"] or "").strip()
    if not content:
        return []
    return [
        {
            "identifier": "system_prompt",
            "name": "System prompt",
            "role": "system",
            "content": content,
            "system_prompt": True,
            "marker": False,
            "injection_position": 0,
            "injection_depth": 0,
            "injection_order": 100,
            "enabled": True,
        }
    ]


def _insert_injections(rest: list[dict], injections: list[dict]) -> list[dict]:
    if not injections:
        return list(rest)
    total = len(rest)
    buckets: dict[int, list[dict]] = {}
    for prompt in injections:
        depth = max(_as_int(prompt.get("injection_depth"), 0), 0)
        position = max(0, min(total - depth, total))
        buckets.setdefault(position, []).append(prompt)

    merged: list[dict] = []
    for index in range(total + 1):
        for prompt in sorted(
            buckets.get(index, []), key=lambda item: _as_int(item.get("injection_order"), 100)
        ):
            merged.append(
                {
                    "role": str(prompt.get("role") or "user").lower(),
                    "content": prompt.get("content") or "",
                }
            )
        if index < total:
            merged.append(rest[index])
    return merged


def merge_messages(
    caller_messages: list[Any], preset_prompts: list[dict[str, Any]], mode: str
) -> list[dict[str, Any]]:
    """Merge caller messages with a premodel's prompts according to ``mode``."""
    caller = [message for message in caller_messages if isinstance(message, dict)]
    caller_system = [m for m in caller if str(m.get("role") or "").lower() == "system"]
    rest = [m for m in caller if str(m.get("role") or "").lower() != "system"]

    system_chunks: list[tuple[int, str]] = []
    injections: list[dict] = []
    for prompt in preset_prompts:
        content = (prompt.get("content") or "").strip()
        if not content:
            continue
        role = str(prompt.get("role") or "system").lower()
        absolute = _as_int(prompt.get("injection_position"), 0) == 1
        if prompt.get("system_prompt") or role == "system" or absolute:
            system_chunks.append((_as_int(prompt.get("injection_order"), 100), content))
        else:
            injections.append(prompt)

    if mode == "caller":
        if caller_system:
            return list(caller)
        mode = "premodel"

    if mode == "premodel":
        trailing_system: list[dict] = []
    else:  # append
        trailing_system = caller_system

    system_text = "\n\n".join(text for _, text in sorted(system_chunks))
    merged: list[dict[str, Any]] = []
    if system_text:
        merged.append({"role": "system", "content": system_text})
    merged.extend(trailing_system)
    merged.extend(_insert_injections(rest, injections))
    return merged


def apply_premodel_messages(premodel, messages: list[Any]) -> list[Any]:
    """Full overlay: MWVAR expansion, prompt merge, macro substitution."""
    definitions: dict[str, dict[str, str]] = {}
    working = messages
    if bool(premodel["accept_mwvars"]):
        definitions, working = extract_mwvars(messages)
    preset = build_preset_prompts(premodel)
    if not preset:
        return substitute_messages(working, definitions)
    merged = merge_messages(working, preset, premodel["merge_mode"] or "append")
    return substitute_messages(merged, definitions)
