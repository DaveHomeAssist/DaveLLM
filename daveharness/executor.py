"""Schema-validated tool execution and bounded agent loop for DaveHarness."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import inspect
import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Collection, Mapping, Protocol, Sequence


LOGGER = logging.getLogger("dave_llm.tools")
DEFAULT_STEP_LIMIT = 8
DEFAULT_ERROR_BUDGET = 2
DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0
PENDING_CALL_TTL_SECONDS = 300
VALID_CANCELLATION_MODES = frozenset({"bounded", "abandon"})

ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]
ModelInvoker = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp() -> str:
    """Return an ISO 8601 UTC timestamp suitable for transcripts and logs."""
    return _utc_now().isoformat()


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
    cancellation: str = "abandon"
    async_handler: bool = False

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
            "cancellation": self.cancellation,
            "async_handler": self.async_handler,
        }


class ToolRegistry:
    """Own enabled tool definitions and support registration or revocation at runtime."""

    def __init__(
        self,
        *,
        async_handler_allowlist: Collection[str] = (),
    ) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._async_handler_allowlist = frozenset(async_handler_allowlist)

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not definition.name.strip():
            raise ValueError("Tool name is required")
        if definition.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be greater than zero")
        if (
            not isinstance(definition.cancellation, str)
            or definition.cancellation not in VALID_CANCELLATION_MODES
        ):
            raise ValueError(
                "Tool cancellation must be either 'bounded' or 'abandon'"
            )
        write_permission = (
            definition.permission == "write"
            or definition.permission.startswith("write_")
        )
        if (
            definition.approval_required or write_permission
        ) and definition.cancellation == "abandon":
            raise ValueError(
                "Approval-required and write tools must declare bounded cancellation"
            )
        handler_is_async = inspect.iscoroutinefunction(definition.handler)
        if handler_is_async and not definition.async_handler:
            raise ValueError("Coroutine tool handlers require async_handler=True")
        if definition.async_handler and not handler_is_async:
            raise ValueError("async_handler=True requires a coroutine function")
        if handler_is_async and definition.name not in self._async_handler_allowlist:
            raise ValueError(
                f"Coroutine tool '{definition.name}' is not in the async handler allowlist"
            )
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
            cancellation=definition.cancellation,
            async_handler=definition.async_handler,
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
    termination: str = "completed"

    def tool_message(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "termination": self.termination,
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
    termination: str = "error",
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
        termination=termination,
    )


async def _invoke_handler(
    definition: ToolDefinition,
    args: dict[str, Any],
) -> Any:
    if definition.async_handler:
        return await definition.handler(args)
    return await asyncio.to_thread(definition.handler, args)


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
            termination="denied",
        )
    elif not isinstance(args, dict):
        execution = _execution_error(
            call_id=resolved_call_id,
            name=name,
            status="validation_error",
            error="Tool arguments must be a JSON object",
            started_at=started_at,
            started_monotonic=started_monotonic,
            termination="denied",
        )
    else:
        try:
            validate_json_schema(args, definition.parameters)
            timeout = timeout_seconds or definition.timeout_seconds
            raw_result = await asyncio.wait_for(
                _invoke_handler(definition, args),
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
                    termination="completed",
                )
        except SchemaValidationError as exc:
            execution = _execution_error(
                call_id=resolved_call_id,
                name=name,
                status="validation_error",
                error=str(exc),
                started_at=started_at,
                started_monotonic=started_monotonic,
                termination="denied",
            )
        except asyncio.TimeoutError:
            execution = _execution_error(
                call_id=resolved_call_id,
                name=name,
                status="timeout",
                error=f"Tool exceeded {timeout_seconds or definition.timeout_seconds:g} second timeout",
                started_at=started_at,
                started_monotonic=started_monotonic,
                termination="deadline_abandoned",
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
                "termination": execution.termination,
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


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_arguments(
    definition: ToolDefinition,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise SchemaValidationError("Tool arguments must be a JSON object")
    validate_json_schema(arguments, definition.parameters)
    return json.loads(_canonical_json(arguments))


def _arguments_digest(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(arguments).encode("utf-8")).hexdigest()


def _transcript_revision(transcript: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical_json(transcript).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PendingCall:
    """One harness-validated call waiting for an exact operator decision."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    digest: str
    transcript_revision: str
    nonce: str
    created_at: str
    expires_at: str

    def public_metadata(self, *, permission: str) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "name": self.tool_name,
            "arguments": copy.deepcopy(self.arguments),
            "permission": permission,
            "digest": self.digest,
            "transcript_revision": self.transcript_revision,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class PendingCallClaim:
    """Result of looking up or atomically consuming one pending call."""

    status: str
    pending_call: PendingCall | None = None
    continuation: object | None = None


class PendingCallStore(Protocol):
    """Store pending in-process continuations without choosing host persistence."""

    def now(self) -> datetime: ...

    def save(
        self,
        run_id: str,
        pending_call: PendingCall,
        continuation: object,
    ) -> None: ...

    def lookup(self, run_id: str, call_id: str) -> PendingCallClaim: ...

    def claim(
        self,
        run_id: str,
        call_id: str,
        digest: str,
        transcript_revision: str,
    ) -> PendingCallClaim: ...


@dataclass(slots=True)
class _PendingEntry:
    pending_call: PendingCall
    continuation: object
    consumed: bool = False


class InMemoryPendingCallStore:
    """Thread-safe single-process pending-call store with replay tombstones."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or _utc_now
        self._entries: dict[tuple[str, str], _PendingEntry] = {}
        self._consumed_nonces: set[str] = set()
        self._lock = threading.Lock()

    def now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Pending-call store clock must return a timezone-aware value")
        return current.astimezone(timezone.utc)

    @staticmethod
    def _snapshot(pending_call: PendingCall) -> PendingCall:
        return PendingCall(
            call_id=pending_call.call_id,
            tool_name=pending_call.tool_name,
            arguments=copy.deepcopy(pending_call.arguments),
            digest=pending_call.digest,
            transcript_revision=pending_call.transcript_revision,
            nonce=pending_call.nonce,
            created_at=pending_call.created_at,
            expires_at=pending_call.expires_at,
        )

    def save(
        self,
        run_id: str,
        pending_call: PendingCall,
        continuation: object,
    ) -> None:
        key = (run_id, pending_call.call_id)
        with self._lock:
            if key in self._entries:
                raise ValueError("Pending call already exists")
            if pending_call.nonce in self._consumed_nonces or any(
                entry.pending_call.nonce == pending_call.nonce
                for entry in self._entries.values()
            ):
                raise ValueError("Pending call nonce already exists")
            self._entries[key] = _PendingEntry(
                pending_call=self._snapshot(pending_call),
                continuation=continuation,
            )

    def _missing_status(self, run_id: str) -> str:
        if any(key_run_id == run_id for key_run_id, _call_id in self._entries):
            return "approval_call_mismatch"
        return "approval_not_found"

    def lookup(self, run_id: str, call_id: str) -> PendingCallClaim:
        with self._lock:
            entry = self._entries.get((run_id, call_id))
            if entry is None:
                return PendingCallClaim(status=self._missing_status(run_id))
            if (
                entry.consumed
                or entry.pending_call.nonce in self._consumed_nonces
            ):
                return PendingCallClaim(
                    status="approval_replayed",
                    pending_call=self._snapshot(entry.pending_call),
                    continuation=entry.continuation,
                )
            return PendingCallClaim(
                status="pending",
                pending_call=self._snapshot(entry.pending_call),
                continuation=entry.continuation,
            )

    def claim(
        self,
        run_id: str,
        call_id: str,
        digest: str,
        transcript_revision: str,
    ) -> PendingCallClaim:
        with self._lock:
            entry = self._entries.get((run_id, call_id))
            if entry is None:
                return PendingCallClaim(status=self._missing_status(run_id))
            pending_call = entry.pending_call
            snapshot = self._snapshot(pending_call)
            if entry.consumed or pending_call.nonce in self._consumed_nonces:
                return PendingCallClaim(
                    status="approval_replayed",
                    pending_call=snapshot,
                    continuation=entry.continuation,
                )
            expires_at = datetime.fromisoformat(pending_call.expires_at)
            if self.now() >= expires_at:
                entry.consumed = True
                self._consumed_nonces.add(pending_call.nonce)
                return PendingCallClaim(
                    status="approval_expired",
                    pending_call=snapshot,
                    continuation=entry.continuation,
                )
            if not hmac.compare_digest(digest, pending_call.digest):
                return PendingCallClaim(
                    status="approval_digest_mismatch",
                    pending_call=snapshot,
                    continuation=entry.continuation,
                )
            if not hmac.compare_digest(
                transcript_revision,
                pending_call.transcript_revision,
            ):
                entry.consumed = True
                self._consumed_nonces.add(pending_call.nonce)
                return PendingCallClaim(
                    status="approval_stale",
                    pending_call=snapshot,
                    continuation=entry.continuation,
                )
            entry.consumed = True
            self._consumed_nonces.add(pending_call.nonce)
            return PendingCallClaim(
                status="claimed",
                pending_call=snapshot,
                continuation=entry.continuation,
            )


DEFAULT_PENDING_CALL_STORE = InMemoryPendingCallStore()


@dataclass(frozen=True, slots=True)
class ExecutorOutcome:
    status: str
    transcript: list[dict[str, Any]]
    final_answer: str | None
    steps: int
    errors: int
    status_message: str
    pending_tool_call: dict[str, Any] | None = None
    run_id: str | None = None

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


@dataclass(slots=True)
class _ExecutorState:
    run_id: str
    transcript: list[dict[str, Any]]
    invoke_model: ModelInvoker
    registry: ToolRegistry
    step_limit: int
    error_budget: int
    approved_tools: set[str]
    model_timeout_seconds: float
    step: int = 0
    errors: int = 0
    last_content: str | None = None


@dataclass(slots=True)
class _PendingContinuation:
    state: _ExecutorState
    remaining_calls: list[ParsedToolCall]


def _state_outcome(
    state: _ExecutorState,
    *,
    status: str,
    status_message: str,
    pending_tool_call: dict[str, Any] | None = None,
    final_answer: str | None = None,
) -> ExecutorOutcome:
    return ExecutorOutcome(
        status=status,
        transcript=copy.deepcopy(state.transcript),
        final_answer=state.last_content if final_answer is None else final_answer,
        steps=state.step,
        errors=state.errors,
        status_message=status_message,
        pending_tool_call=pending_tool_call,
        run_id=state.run_id,
    )


def _approval_failure_outcome(
    *,
    status: str,
    run_id: str,
    continuation: object | None,
) -> ExecutorOutcome:
    messages = {
        "approval_not_found": "The pending run was not found.",
        "approval_call_mismatch": "The call ID does not match this pending run.",
        "approval_digest_mismatch": "The approval digest does not match the pending call.",
        "approval_stale": "The pending transcript revision has changed.",
        "approval_replayed": "The pending call decision has already been consumed.",
        "approval_expired": "The pending call approval window has expired.",
        "approval_invalid_decision": "Decision must be either approve or deny.",
    }
    if isinstance(continuation, _PendingContinuation):
        return _state_outcome(
            continuation.state,
            status=status,
            status_message=messages[status],
        )
    return ExecutorOutcome(
        status=status,
        transcript=[],
        final_answer=None,
        steps=0,
        errors=0,
        status_message=messages[status],
        run_id=run_id,
    )


def _operator_denied_execution(call: PendingCall) -> ToolExecution:
    timestamp = utc_timestamp()
    return ToolExecution(
        call_id=call.call_id,
        name=call.tool_name,
        status="denied",
        result="",
        error="Operator denied this tool call",
        started_at=timestamp,
        completed_at=timestamp,
        duration_ms=0.0,
        termination="denied",
    )


async def _process_calls(
    state: _ExecutorState,
    calls: list[ParsedToolCall],
    pending_store: PendingCallStore,
) -> ExecutorOutcome | None:
    for index, call in enumerate(calls):
        definition = state.registry.get(call.name)
        if (
            definition
            and definition.approval_required
            and call.name not in state.approved_tools
        ):
            try:
                canonical_arguments = _canonical_arguments(
                    definition,
                    call.arguments,
                )
            except (SchemaValidationError, TypeError, ValueError):
                execution = await run_tool(
                    call.name,
                    call.arguments,
                    call_id=call.call_id,
                    registry=state.registry,
                )
            else:
                created_at = pending_store.now()
                pending_call = PendingCall(
                    call_id=call.call_id,
                    tool_name=call.name,
                    arguments=canonical_arguments,
                    digest=_arguments_digest(canonical_arguments),
                    transcript_revision=_transcript_revision(state.transcript),
                    nonce=secrets.token_urlsafe(32),
                    created_at=created_at.isoformat(),
                    expires_at=(
                        created_at + timedelta(seconds=PENDING_CALL_TTL_SECONDS)
                    ).isoformat(),
                )
                pending_store.save(
                    state.run_id,
                    pending_call,
                    _PendingContinuation(
                        state=state,
                        remaining_calls=copy.deepcopy(calls[index + 1 :]),
                    ),
                )
                return _state_outcome(
                    state,
                    status="approval_required",
                    status_message=(
                        f"Approval is required before running {call.name}."
                    ),
                    pending_tool_call=pending_call.public_metadata(
                        permission=definition.permission
                    ),
                )
        else:
            execution = await run_tool(
                call.name,
                call.arguments,
                call_id=call.call_id,
                registry=state.registry,
            )

        state.transcript.append(execution.tool_message())
        if execution.status != "success":
            state.errors += 1
            if state.errors >= state.error_budget:
                return _state_outcome(
                    state,
                    status="error_budget",
                    status_message="Tool failures exhausted the error budget.",
                )
    return None


async def _continue_executor_loop(
    state: _ExecutorState,
    pending_store: PendingCallStore,
) -> ExecutorOutcome:
    while state.step < state.step_limit:
        state.step += 1
        try:
            model_message = await _invoke_model(
                state.invoke_model,
                state.transcript,
                state.registry.model_schemas(),
                state.model_timeout_seconds,
            )
        except asyncio.TimeoutError:
            state.errors += 1
            return _state_outcome(
                state,
                status="model_timeout",
                status_message=(
                    f"Model exceeded {state.model_timeout_seconds:g} second timeout."
                ),
            )
        except Exception as exc:
            state.errors += 1
            return _state_outcome(
                state,
                status="model_error",
                status_message=f"Model invocation failed: {exc}",
            )

        content = model_message.get("content")
        if isinstance(content, str) and content.strip():
            state.last_content = content
        calls, parse_error = parse_tool_calls(model_message)

        if parse_error:
            state.transcript.append(dict(model_message))
            state.errors += 1
            state.transcript.append(
                {
                    "role": "system",
                    "content": (
                        "The prior tool call was malformed and was not executed. "
                        f"Error: {parse_error}. Return a valid structured tool call "
                        "matching one provided schema, or return a final answer."
                    ),
                }
            )
            if state.errors >= state.error_budget:
                return _state_outcome(
                    state,
                    status="error_budget",
                    status_message=(
                        "Malformed tool calls exhausted the error budget."
                    ),
                )
            continue

        if not calls:
            state.transcript.append(dict(model_message))
            return _state_outcome(
                state,
                status="completed",
                status_message="Model returned a final answer.",
                final_answer=content if isinstance(content, str) else "",
            )

        assistant_message = dict(model_message)
        assistant_message["role"] = "assistant"
        assistant_message["content"] = content if isinstance(content, str) else ""
        assistant_message["tool_calls"] = [call.as_openai_call() for call in calls]
        state.transcript.append(assistant_message)
        outcome = await _process_calls(state, calls, pending_store)
        if outcome is not None:
            return outcome

    return _state_outcome(
        state,
        status="step_limit",
        status_message=(
            f"Stopped after the configured {state.step_limit} model steps. "
            "The full partial transcript is available."
        ),
    )


async def run_executor_loop(
    messages: list[dict[str, Any]],
    invoke_model: ModelInvoker,
    *,
    registry: ToolRegistry = DEFAULT_TOOL_REGISTRY,
    step_limit: int = DEFAULT_STEP_LIMIT,
    error_budget: int = DEFAULT_ERROR_BUDGET,
    approved_tools: set[str] | None = None,
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS,
    pending_store: PendingCallStore = DEFAULT_PENDING_CALL_STORE,
    run_id: str | None = None,
) -> ExecutorOutcome:
    """Run model and tools until a final answer, ceiling, approval gate, or error budget."""
    if step_limit < 1:
        raise ValueError("step_limit must be at least 1")
    if error_budget < 1:
        raise ValueError("error_budget must be at least 1")

    state = _ExecutorState(
        run_id=run_id or f"run_{uuid.uuid4().hex}",
        transcript=copy.deepcopy(messages),
        invoke_model=invoke_model,
        registry=registry,
        step_limit=step_limit,
        error_budget=error_budget,
        approved_tools=set(approved_tools or set()),
        model_timeout_seconds=model_timeout_seconds,
    )
    return await _continue_executor_loop(state, pending_store)


async def resume_executor_loop(
    *,
    run_id: str,
    call_id: str,
    digest: str,
    decision: str,
    pending_store: PendingCallStore = DEFAULT_PENDING_CALL_STORE,
) -> ExecutorOutcome:
    """Consume one exact pending decision and resume without replaying its model step."""
    lookup = pending_store.lookup(run_id, call_id)
    if lookup.status != "pending":
        return _approval_failure_outcome(
            status=lookup.status,
            run_id=run_id,
            continuation=lookup.continuation,
        )
    if decision not in {"approve", "deny"}:
        return _approval_failure_outcome(
            status="approval_invalid_decision",
            run_id=run_id,
            continuation=lookup.continuation,
        )
    if not isinstance(lookup.continuation, _PendingContinuation):
        return _approval_failure_outcome(
            status="approval_stale",
            run_id=run_id,
            continuation=lookup.continuation,
        )

    continuation = lookup.continuation
    current_revision = _transcript_revision(continuation.state.transcript)
    claim = pending_store.claim(
        run_id,
        call_id,
        digest,
        current_revision,
    )
    if claim.status != "claimed":
        return _approval_failure_outcome(
            status=claim.status,
            run_id=run_id,
            continuation=claim.continuation,
        )
    if claim.pending_call is None:
        return _approval_failure_outcome(
            status="approval_stale",
            run_id=run_id,
            continuation=claim.continuation,
        )

    pending_call = claim.pending_call
    state = continuation.state
    if decision == "approve":
        execution = await run_tool(
            pending_call.tool_name,
            copy.deepcopy(pending_call.arguments),
            call_id=pending_call.call_id,
            registry=state.registry,
        )
    else:
        execution = _operator_denied_execution(pending_call)
    state.transcript.append(execution.tool_message())

    if decision == "approve" and execution.status != "success":
        state.errors += 1
        if state.errors >= state.error_budget:
            return _state_outcome(
                state,
                status="error_budget",
                status_message="Tool failures exhausted the error budget.",
            )

    outcome = await _process_calls(
        state,
        continuation.remaining_calls,
        pending_store,
    )
    if outcome is not None:
        return outcome
    return await _continue_executor_loop(state, pending_store)
