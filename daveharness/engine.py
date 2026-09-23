"""Bounded tool execution, exact-call approval, and loop continuation."""

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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence, cast

from .budgets import RunBudget
from .contracts import ExecutorOutcome, ParsedToolCall, PendingCall, ToolExecution
from .parser import parse_tool_calls
from .policy import RunPolicyContext, ToolPolicy
from .registry import DEFAULT_TOOL_REGISTRY, ToolDefinition, ToolRegistry
from .schema import SchemaValidationError, validate_json_schema

LOGGER = logging.getLogger("dave_llm.tools")
DEFAULT_STEP_LIMIT = 8
DEFAULT_ERROR_BUDGET = 2
DEFAULT_MODEL_TIMEOUT_SECONDS = 120.0
PENDING_CALL_TTL_SECONDS = 300

ModelInvoker = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


class _ToolGateError(Exception):
    def __init__(self, status: str, reason_code: str) -> None:
        self.status = status
        self.reason_code = reason_code
        super().__init__(reason_code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp() -> str:
    """Return an ISO 8601 UTC timestamp suitable for transcripts and logs."""
    return _utc_now().isoformat()


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
    policy: ToolPolicy | None = None,
    policy_context: RunPolicyContext | None = None,
    expected_fingerprint: str | None = None,
    expected_permission: str | None = None,
    expected_registration_revision: int | None = None,
    output_bytes: int | None = None,
) -> ToolExecution:
    """Validate and run one registered tool, returning errors instead of raising."""
    resolved_call_id = call_id or f"call_{uuid.uuid4().hex}"
    started_at = utc_timestamp()
    started_monotonic = time.monotonic()
    definition, initial_revision, initial_fingerprint = registry.get_with_revision(name)
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
            context = policy_context or RunPolicyContext(approved_tools=frozenset({name}))
            decision = (policy or ToolPolicy()).evaluate(
                registry, name, context,
                expected_fingerprint=expected_fingerprint or initial_fingerprint,
                expected_permission=expected_permission or definition.permission,
                expected_registration_revision=expected_registration_revision
                if expected_registration_revision is not None else initial_revision,
            )
            if decision.action != "allow":
                raise _ToolGateError(
                    "revoked" if decision.reason_code in {"tool_revoked", "tool_unknown", "definition_changed", "permission_changed", "registration_changed"} else "denied",
                    decision.reason_code,
                )
            if decision.fingerprint is None or decision.registration_revision is None:
                raise _ToolGateError("revoked", "definition_changed")
            current_definition = registry.begin_execution(name, decision.fingerprint, decision.registration_revision)
            if current_definition is None:
                raise _ToolGateError("revoked", "definition_changed")
            definition = current_definition
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
        except _ToolGateError as exc:
            execution = _execution_error(
                call_id=resolved_call_id, name=name, status=exc.status,
                error=exc.reason_code, started_at=started_at,
                started_monotonic=started_monotonic,
                termination="error" if exc.status == "output_limit" else "denied",
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

    if output_bytes is not None and (
        len(execution.result.encode("utf-8")) + len((execution.error or "").encode("utf-8")) > output_bytes
    ):
        execution = _execution_error(
            call_id=resolved_call_id, name=name, status="output_limit",
            error="tool_output_limit", started_at=started_at,
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
    return cast(dict[str, Any], json.loads(_canonical_json(arguments)))


def _arguments_digest(arguments: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(arguments).encode("utf-8")).hexdigest()


def _transcript_revision(transcript: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical_json(transcript).encode("utf-8")).hexdigest()

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
            return cast(Mapping[str, Any], choice["message"])
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
    budget: RunBudget
    approved_tools: set[str]
    allowed_permissions: frozenset[str]
    model_timeout_seconds: float
    policy: ToolPolicy
    started_monotonic: float
    step: int = 0
    errors: int = 0
    tool_calls: int = 0
    last_content: str | None = None


@dataclass(slots=True)
class _PendingContinuation:
    state: _ExecutorState
    remaining_calls: list[ParsedToolCall]
    definition_fingerprint: str
    permission: str
    registration_revision: int


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


def _budget_stop(state: _ExecutorState, reason_code: str) -> ExecutorOutcome:
    return _state_outcome(
        state, status="budget_exceeded", status_message=reason_code,
    )


def _transcript_too_large(state: _ExecutorState) -> bool:
    if state.budget.transcript_bytes is None:
        return False
    return state.budget.exceeded(
        "transcript_bytes", len(_canonical_json(state.transcript).encode("utf-8"))
    )


def _wall_time_exceeded(state: _ExecutorState) -> bool:
    return state.budget.wall_expired(state.started_monotonic, time.monotonic())


def _remaining_wall_time(state: _ExecutorState) -> float | None:
    limit = state.budget.total_wall_seconds
    if limit is None:
        return None
    return max(0.0, limit - (time.monotonic() - state.started_monotonic))


def _bounded_tool_timeout(state: _ExecutorState, definition: ToolDefinition | None) -> float | None:
    remaining = _remaining_wall_time(state)
    if remaining is None or definition is None:
        return None
    return min(definition.timeout_seconds, remaining)


async def _process_calls(
    state: _ExecutorState,
    calls: list[ParsedToolCall],
    pending_store: PendingCallStore,
) -> ExecutorOutcome | None:
    for index, call in enumerate(calls):
        state.tool_calls += 1
        if state.budget.exceeded("tool_calls", state.tool_calls):
            return _budget_stop(state, "tool_call_limit")
        if _wall_time_exceeded(state):
            return _budget_stop(state, "run_wall_limit")
        context = RunPolicyContext(
            approved_tools=frozenset(state.approved_tools),
            allowed_permissions=state.allowed_permissions,
        )
        decision = state.policy.evaluate(state.registry, call.name, context)
        definition = state.registry.get(call.name)
        if decision.action == "deny":
            now = utc_timestamp()
            execution = ToolExecution(
                call_id=call.call_id, name=call.name,
                status="revoked" if decision.reason_code in {"tool_revoked", "tool_unknown", "definition_changed", "permission_changed", "registration_changed"} else "denied",
                result="", error=decision.reason_code, started_at=now,
                completed_at=now, duration_ms=0.0, termination="denied",
            )
        elif (
            definition
            and decision.action == "pause"
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
                    policy=state.policy,
                    policy_context=context,
                    expected_fingerprint=decision.fingerprint,
                    expected_permission=decision.permission,
                    expected_registration_revision=decision.registration_revision,
                    output_bytes=state.budget.tool_output_bytes,
                    timeout_seconds=_bounded_tool_timeout(state, definition),
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
                        definition_fingerprint=decision.fingerprint or "",
                        permission=decision.permission or "",
                        registration_revision=decision.registration_revision or 0,
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
                policy=state.policy,
                policy_context=context,
                expected_fingerprint=decision.fingerprint,
                expected_permission=decision.permission,
                expected_registration_revision=decision.registration_revision,
                output_bytes=state.budget.tool_output_bytes,
                timeout_seconds=_bounded_tool_timeout(state, definition),
            )

        state.transcript.append(execution.tool_message())
        if _wall_time_exceeded(state):
            return _budget_stop(state, "run_wall_limit")
        if _transcript_too_large(state):
            return _budget_stop(state, "transcript_limit")
        if execution.status != "success":
            state.errors += 1
            if state.errors >= state.budget.errors:
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
    while state.step < state.budget.model_steps:
        if _wall_time_exceeded(state):
            return _budget_stop(state, "run_wall_limit")
        if _transcript_too_large(state):
            return _budget_stop(state, "transcript_limit")
        state.step += 1
        remaining = _remaining_wall_time(state)
        timeout = min(state.model_timeout_seconds, remaining) if remaining is not None else state.model_timeout_seconds
        try:
            model_message = await _invoke_model(
                state.invoke_model,
                state.transcript,
                state.registry.model_schemas(),
                timeout,
            )
        except asyncio.TimeoutError:
            state.errors += 1
            if _wall_time_exceeded(state):
                return _budget_stop(state, "run_wall_limit")
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

        if _wall_time_exceeded(state):
            return _budget_stop(state, "run_wall_limit")

        content = model_message.get("content")
        if isinstance(content, str) and content.strip():
            state.last_content = content
        calls, parse_error = parse_tool_calls(model_message)

        if parse_error:
            state.transcript.append(dict(model_message))
            if _transcript_too_large(state):
                return _budget_stop(state, "transcript_limit")
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
            if state.errors >= state.budget.errors:
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
            if _transcript_too_large(state):
                return _budget_stop(state, "transcript_limit")
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
        if _transcript_too_large(state):
            return _budget_stop(state, "transcript_limit")
        outcome = await _process_calls(state, calls, pending_store)
        if outcome is not None:
            return outcome

    return _state_outcome(
        state,
        status="step_limit",
        status_message=(
            f"Stopped after the configured {state.budget.model_steps} model steps. "
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
    budget: RunBudget | None = None,
    policy: ToolPolicy | None = None,
    policy_context: RunPolicyContext | None = None,
) -> ExecutorOutcome:
    """Run model and tools until a final answer, ceiling, approval gate, or error budget."""
    if step_limit < 1:
        raise ValueError("step_limit must be at least 1")
    if error_budget < 1:
        raise ValueError("error_budget must be at least 1")

    run_context = policy_context or RunPolicyContext()
    state = _ExecutorState(
        run_id=run_id or f"run_{uuid.uuid4().hex}",
        transcript=copy.deepcopy(messages),
        invoke_model=invoke_model,
        registry=registry,
        budget=budget or RunBudget.legacy(model_steps=step_limit, errors=error_budget),
        approved_tools=set(approved_tools or set()) | set(run_context.approved_tools),
        allowed_permissions=run_context.allowed_permissions,
        model_timeout_seconds=model_timeout_seconds,
        policy=policy or ToolPolicy(),
        started_monotonic=time.monotonic(),
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
    if decision == "approve":
        pending_for_check = lookup.pending_call
        if pending_for_check is None:
            return _approval_failure_outcome(
                status="approval_stale", run_id=run_id, continuation=continuation,
            )
        policy_decision = continuation.state.policy.evaluate(
            continuation.state.registry, pending_for_check.tool_name,
            RunPolicyContext(
                approved_tools=frozenset({pending_for_check.tool_name}),
                allowed_permissions=continuation.state.allowed_permissions,
            ),
            expected_fingerprint=continuation.definition_fingerprint,
            expected_permission=continuation.permission,
            expected_registration_revision=continuation.registration_revision,
        )
        if policy_decision.action != "allow":
            return _approval_failure_outcome(
                status="approval_stale", run_id=run_id, continuation=continuation,
            )
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
    if _wall_time_exceeded(state):
        return _budget_stop(state, "run_wall_limit")
    if decision == "approve":
        execution = await run_tool(
            pending_call.tool_name,
            copy.deepcopy(pending_call.arguments),
            call_id=pending_call.call_id,
            registry=state.registry,
            policy=state.policy,
            policy_context=RunPolicyContext(
                approved_tools=frozenset({pending_call.tool_name}),
                allowed_permissions=state.allowed_permissions,
            ),
            expected_fingerprint=continuation.definition_fingerprint,
            expected_permission=continuation.permission,
            expected_registration_revision=continuation.registration_revision,
            output_bytes=state.budget.tool_output_bytes,
            timeout_seconds=_bounded_tool_timeout(state, state.registry.get(pending_call.tool_name)),
        )
    else:
        execution = _operator_denied_execution(pending_call)
    state.transcript.append(execution.tool_message())
    if _transcript_too_large(state):
        return _budget_stop(state, "transcript_limit")

    if decision == "approve" and execution.status != "success":
        state.errors += 1
        if state.errors >= state.budget.errors:
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
