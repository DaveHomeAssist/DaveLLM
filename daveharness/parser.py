"""Normalize model tool calls without executing them."""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping

from .contracts import ParsedToolCall


def _decode_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ValueError("Tool arguments must be a JSON object or JSON string")
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("Tool arguments must decode to a JSON object")
    return decoded


def _strip_json_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped[7:-3].strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped[3:-3].strip()
    return stripped


def parse_tool_calls(message: Mapping[str, Any]) -> tuple[list[ParsedToolCall], str | None]:
    """Read native tool calls first, then one bounded JSON-content fallback."""
    native_calls = message.get("tool_calls")
    if native_calls:
        parsed: list[ParsedToolCall] = []
        try:
            if not isinstance(native_calls, list):
                raise ValueError("tool_calls must be an array")
            for index, call in enumerate(native_calls):
                if not isinstance(call, Mapping):
                    raise ValueError(f"tool_calls[{index}] must be an object")
                function = call.get("function", call)
                if not isinstance(function, Mapping):
                    raise ValueError(f"tool_calls[{index}].function must be an object")
                name = function.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise ValueError(f"tool_calls[{index}] is missing a function name")
                parsed.append(
                    ParsedToolCall(
                        call_id=str(call.get("id") or f"call_{uuid.uuid4().hex}"),
                        name=name,
                        arguments=_decode_arguments(
                            function.get("arguments", function.get("params", {}))
                        ),
                    )
                )
            return parsed, None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return [], str(exc)

    content = message.get("content")
    if not isinstance(content, str):
        return [], None
    candidate = _strip_json_fence(content)
    if not candidate.startswith("{"):
        return [], None
    try:
        decoded = json.loads(candidate)
    except json.JSONDecodeError as exc:
        if '"tool"' in candidate or '"name"' in candidate:
            return [], f"Malformed JSON tool call: {exc.msg}"
        return [], None
    if not isinstance(decoded, dict):
        return [], None
    name = decoded.get("tool") or decoded.get("name")
    if not name:
        return [], None
    try:
        arguments = _decode_arguments(decoded.get("params", decoded.get("arguments", {})))
        call = ParsedToolCall(
            call_id=f"call_{uuid.uuid4().hex}",
            name=str(name),
            arguments=arguments,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return [], str(exc)
    return [call], None
