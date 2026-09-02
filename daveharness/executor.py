"""Schema-validated tool execution and bounded agent loop for DaveHarness."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence


LOGGER = logging.getLogger("dave_llm.tools")
DEFAULT_STEP_LIMIT = 8
DEFAULT_ERROR_BUDGET = 2
DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0

ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]
ModelInvoker = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


def utc_timestamp() -> str:
    """Return an ISO 8601 UTC timestamp suitable for transcripts and logs."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Describe one runtime-loadable tool and its trust boundary."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    permission: str = "read"
    approval_required: bool = False
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS

    def model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.parameters),
            },
        }

    def public_metadata(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "parameters": copy.deepcopy(self.parameters),
            "permission": self.permission,
            "approval_required": self.approval_required,
            "timeout_seconds": self.timeout_seconds,
        }


class ToolRegistry:
    """Own enabled tool definitions and support registration or revocation at runtime."""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not definition.name.strip():
            raise ValueError("Tool name is required")
        if definition.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be greater than zero")
        if definition.name in self._definitions:
            raise ValueError(f"Tool '{definition.name}' is already registered")
        self._definitions[definition.name] = self._snapshot(definition)

    @staticmethod
    def _snapshot(definition: ToolDefinition) -> ToolDefinition:
        """Copy security-relevant definition data at the registry boundary."""
        return ToolDefinition(
            name=definition.name,
            description=definition.description,
            parameters=copy.deepcopy(definition.parameters),
            handler=definition.handler,
            permission=definition.permission,
            approval_required=definition.approval_required,
            timeout_seconds=definition.timeout_seconds,
        )

    def revoke(self, name: str) -> bool:
        return self._definitions.pop(name, None) is not None

    def get(self, name: str) -> ToolDefinition | None:
        definition = self._definitions.get(name)
        return self._snapshot(definition) if definition is not None else None

    def model_schemas(self) -> list[dict[str, Any]]:
        return [
            self._definitions[name].model_schema()
            for name in sorted(self._definitions)
        ]

    def public_catalog(self) -> dict[str, dict[str, Any]]:
        return {
            name: self._definitions[name].public_metadata()
            for name in sorted(self._definitions)
        }


DEFAULT_TOOL_REGISTRY = ToolRegistry()


class SchemaValidationError(ValueError):
    """Report a JSON schema mismatch without exposing an implementation traceback."""


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise SchemaValidationError(f"Unsupported schema type '{expected}'")


def validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Validate the JSON schema subset used by the DaveLLM tool registry."""
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, item) for item in expected):
            raise SchemaValidationError(
                f"{path} must match one of: {', '.join(expected)}"
            )
    elif isinstance(expected, str) and not _matches_type(value, expected):
        raise SchemaValidationError(f"{path} must be {expected}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        raise SchemaValidationError(f"{path} must be one of: {allowed}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise SchemaValidationError(f"{path}.{key} is required")

        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise SchemaValidationError(
                    f"{path} contains unsupported field(s): {', '.join(unexpected)}"
                )
        for key, item in value.items():
            if key in properties:
                validate_json_schema(item, properties[key], f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_json_schema(item, schema["items"], f"{path}[{index}]")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise SchemaValidationError(f"{path} is shorter than {minimum} characters")
        if isinstance(maximum, int) and len(value) > maximum:
            raise SchemaValidationError(f"{path} is longer than {maximum} characters")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise SchemaValidationError(f"{path} must be at least {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise SchemaValidationError(f"{path} must be at most {maximum}")


@dataclass(frozen=True, slots=True)
class ToolExecution:
    call_id: str
    name: str
    status: str
    result: str
    error: str | None
    started_at: str
    completed_at: str
    duration_ms: float

    def tool_message(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
        }
        return {
            "role": "tool",
            "tool_call_id": self.call_id,
            "name": self.name,
            "content": json.dumps(payload, ensure_ascii=False),
        }


def _execution_error(
    *,
    call_id: str,
    name: str,
    status: str,
    error: str,
    started_at: str,
    started_monotonic: float,
) -> ToolExecution:
    return ToolExecution(
        call_id=call_id,
        name=name,
        status=status,
        result="",
        error=error,
        started_at=started_at,
        completed_at=utc_timestamp(),
        duration_ms=round((time.monotonic() - started_monotonic) * 1000, 3),
    )


async def _invoke_handler(handler: ToolHandler, args: dict[str, Any]) -> Any:
    if inspect.iscoroutinefunction(handler):
        return await handler(args)
    return await asyncio.to_thread(handler, args)


async def run_tool(
    name: str,
    args: dict[str, Any],
    *,
    call_id: str | None = None,
    registry: ToolRegistry = DEFAULT_TOOL_REGISTRY,
    timeout_seconds: float | None = None,
) -> ToolExecution:
    """Validate and run one registered tool, returning errors instead of raising."""
    resolved_call_id = call_id or f"call_{uuid.uuid4().hex}"
    started_at = utc_timestamp()
    started_monotonic = time.monotonic()
    definition = registry.get(name)
    LOGGER.info(
        json.dumps(
            {
                "event": "tool_call",
                "call_id": resolved_call_id,
                "tool": name,
                "timestamp": started_at,
                "argument_keys": sorted(args) if isinstance(args, dict) else [],
            }
        )
    )

    if definition is None:
        execution = _execution_error(
            call_id=resolved_call_id,
            name=name,
            status="revoked",
            error=f"Tool '{name}' is disabled, not registered, or has been revoked",
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
    elif not isinstance(args, dict):
        execution = _execution_error(
            call_id=resolved_call_id,
            name=name,
            status="validation_error",
            error="Tool arguments must be a JSON object",
            started_at=started_at,
            started_monotonic=started_monotonic,
        )
    else:
        try:
            validate_json_schema(args, definition.parameters)
            timeout = timeout_seconds or definition.timeout_seconds
            raw_result = await asyncio.wait_for(
                _invoke_handler(definition.handler, args),
                timeout=timeout,
            )
            if hasattr(raw_result, "model_dump"):
                raw_result = raw_result.model_dump()
            if isinstance(raw_result, Mapping) and raw_result.get("status") == "error":
                execution = _execution_error(
                    call_id=resolved_call_id,
                    name=name,
                    status="error",
                    error=str(raw_result.get("error") or "Tool failed"),
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                )
            else:
                if isinstance(raw_result, Mapping) and "result" in raw_result:
                    raw_result = raw_result["result"]
                result = (
                    raw_result
                    if isinstance(raw_result, str)
                    else json.dumps(raw_result, ensure_ascii=False, sort_keys=True)
                )
                execution = ToolExecution(
                    call_id=resolved_call_id,
                    name=name,
                    status="success",
                    result=result,
                    error=None,
                    started_at=started_at,
                    completed_at=utc_timestamp(),
                    duration_ms=round(
                        (time.monotonic() - started_monotonic) * 1000,
                        3,
                    ),
                )
        except SchemaValidationError as exc:
            execution = _execution_error(
                call_id=resolved_call_id,
                name=name,
                status="validation_error",
                error=str(exc),
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        except asyncio.TimeoutError:
            execution = _execution_error(
                call_id=resolved_call_id,
                name=name,
                status="timeout",
                error=f"Tool exceeded {timeout_seconds or definition.timeout_seconds:g} second timeout",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        except Exception as exc:
            execution = _execution_error(
                call_id=resolved_call_id,
                name=name,
                status="error",
                error=str(exc),
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

    LOGGER.info(
        json.dumps(
            {
                "event": "tool_result",
                "call_id": execution.call_id,
                "tool": execution.name,
                "status": execution.status,
                "timestamp": execution.completed_at,
                "duration_ms": execution.duration_ms,
            }
        )
    )
    return execution


@dataclass(frozen=True, slots=True)
class ParsedToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def as_openai_call(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


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
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return [], str(exc)
    return [
        ParsedToolCall(
            call_id=f"call_{uuid.uuid4().hex}",
            name=str(name),
            arguments=arguments,
        )
    ], None


@dataclass(frozen=True, slots=True)
class ExecutorOutcome:
    status: str
    transcript: list[dict[str, Any]]
    final_answer: str | None
    steps: int
    errors: int
    status_message: str
    pending_tool_call: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _invoke_model(
    invoke_model: ModelInvoker,
    transcript: list[dict[str, Any]],
    schemas: list[dict[str, Any]],
    timeout_seconds: float,
) -> Mapping[str, Any]:
    async def call_model() -> Any:
        copied_transcript = copy.deepcopy(transcript)
        copied_schemas = copy.deepcopy(schemas)
        if inspect.iscoroutinefunction(invoke_model):
            return await invoke_model(copied_transcript, copied_schemas)
        result = await asyncio.to_thread(
            invoke_model,
            copied_transcript,
            copied_schemas,
        )
        if inspect.isawaitable(result):
            return await result
        return result

    result = await asyncio.wait_for(call_model(), timeout=timeout_seconds)
    if not isinstance(result, Mapping):
        raise ValueError("Model invoker must return an object")
    choices = result.get("choices")
    if isinstance(choices, Sequence) and choices:
        choice = choices[0]
        if isinstance(choice, Mapping) and isinstance(choice.get("message"), Mapping):
            return choice["message"]
    message = result.get("message")
    if isinstance(message, Mapping):
        return message
    return result


async def run_executor_loop(
    messages: list[dict[str, Any]],
    invoke_model: ModelInvoker,
    *,
    registry: ToolRegistry = DEFAULT_TOOL_REGISTRY,
    step_limit: int = DEFAULT_STEP_LIMIT,
    error_budget: int = DEFAULT_ERROR_BUDGET,
    approved_tools: set[str] | None = None,
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS,
) -> ExecutorOutcome:
    """Run model and tools until a final answer, ceiling, approval gate, or error budget."""
    if step_limit < 1:
        raise ValueError("step_limit must be at least 1")
    if error_budget < 1:
        raise ValueError("error_budget must be at least 1")

    transcript = copy.deepcopy(messages)
    approvals = approved_tools or set()
    errors = 0
    last_content: str | None = None

    for step in range(1, step_limit + 1):
        try:
            model_message = await _invoke_model(
                invoke_model,
                transcript,
                registry.model_schemas(),
                model_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ExecutorOutcome(
                status="model_timeout",
                transcript=transcript,
                final_answer=last_content,
                steps=step,
                errors=errors + 1,
                status_message=f"Model exceeded {model_timeout_seconds:g} second timeout.",
            )
        except Exception as exc:
            return ExecutorOutcome(
                status="model_error",
                transcript=transcript,
                final_answer=last_content,
                steps=step,
                errors=errors + 1,
                status_message=f"Model invocation failed: {exc}",
            )

        content = model_message.get("content")
        if isinstance(content, str) and content.strip():
            last_content = content
        calls, parse_error = parse_tool_calls(model_message)

        if parse_error:
            transcript.append(dict(model_message))
            errors += 1
            transcript.append(
                {
                    "role": "system",
                    "content": (
                        "The prior tool call was malformed and was not executed. "
                        f"Error: {parse_error}. Return a valid structured tool call "
                        "matching one provided schema, or return a final answer."
                    ),
                }
            )
            if errors >= error_budget:
                return ExecutorOutcome(
                    status="error_budget",
                    transcript=transcript,
                    final_answer=last_content,
                    steps=step,
                    errors=errors,
                    status_message="Malformed tool calls exhausted the error budget.",
                )
            continue

        if not calls:
            transcript.append(dict(model_message))
            return ExecutorOutcome(
                status="completed",
                transcript=transcript,
                final_answer=content if isinstance(content, str) else "",
                steps=step,
                errors=errors,
                status_message="Model returned a final answer.",
            )

        assistant_message = dict(model_message)
        assistant_message["role"] = "assistant"
        assistant_message["content"] = content if isinstance(content, str) else ""
        assistant_message["tool_calls"] = [call.as_openai_call() for call in calls]
        transcript.append(assistant_message)

        for call in calls:
            definition = registry.get(call.name)
            if definition and definition.approval_required and call.name not in approvals:
                return ExecutorOutcome(
                    status="approval_required",
                    transcript=transcript,
                    final_answer=last_content,
                    steps=step,
                    errors=errors,
                    status_message=f"Approval is required before running {call.name}.",
                    pending_tool_call={
                        "id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                        "permission": definition.permission,
                    },
                )

            execution = await run_tool(
                call.name,
                call.arguments,
                call_id=call.call_id,
                registry=registry,
            )
            transcript.append(execution.tool_message())
            if execution.status != "success":
                errors += 1
                if errors >= error_budget:
                    return ExecutorOutcome(
                        status="error_budget",
                        transcript=transcript,
                        final_answer=last_content,
                        steps=step,
                        errors=errors,
                        status_message="Tool failures exhausted the error budget.",
                    )

    return ExecutorOutcome(
        status="step_limit",
        transcript=transcript,
        final_answer=last_content,
        steps=step_limit,
        errors=errors,
        status_message=(
            f"Stopped after the configured {step_limit} model steps. "
            "The full partial transcript is available."
        ),
    )
