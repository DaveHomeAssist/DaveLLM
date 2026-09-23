"""Serializable run operations with host-supplied compare-and-swap persistence."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .contracts import ParsedToolCall, ToolExecution
from .engine import (
    DEFAULT_MODEL_TIMEOUT_SECONDS, ModelInvoker, _canonical_arguments,
    _canonical_json, _invoke_model, _log_tool_result, _transcript_revision, run_tool,
)
from .parser import parse_tool_calls
from .policy import RunPolicyContext, ToolPolicy
from .registry import DEFAULT_TOOL_TIMEOUT_SECONDS, ToolRegistry
from .runtime import (
    CancellationToken, CancellableToolRunner, ExecutionContext, MonotonicClock,
    OperationController, absolute_deadline, monotonic_now,
    remaining_wall_seconds,
)
from .schema import SchemaValidationError
from .state import (
    ApprovalDecision, PendingToolCall, RunSnapshot, TERMINAL_RUN_STATUSES,
    transition_run,
)


class SnapshotCAS(Protocol):
    """H3's narrow host seam; H6 supplies a bounded production implementation."""

    def load(self, run_id: str) -> RunSnapshot | None: ...

    def compare_and_swap(self, run_id: str, expected_version: int, snapshot: RunSnapshot) -> bool: ...


@dataclass(frozen=True, slots=True)
class RunCommandResult:
    status: str
    snapshot: RunSnapshot | None
    reason_code: str


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("run clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _updated_at(snapshot: RunSnapshot, clock: Clock) -> str:
    """Keep durable timestamps ordered when the wall clock moves backward."""
    return max(_now(clock), datetime.fromisoformat(snapshot.updated_at)).isoformat()


def _result(snapshot: RunSnapshot | None, reason_code: str) -> RunCommandResult:
    return RunCommandResult(snapshot.status if snapshot else "run_expired", snapshot, reason_code)


def _save(store: SnapshotCAS, old: RunSnapshot, new: RunSnapshot) -> bool:
    return store.compare_and_swap(old.run_id, old.optimistic_version, new)


def _conflict(snapshot: RunSnapshot | None) -> RunCommandResult:
    if snapshot is not None and snapshot.status in TERMINAL_RUN_STATUSES:
        return _result(snapshot, snapshot.status)
    return RunCommandResult("run_conflict", snapshot, "run_conflict")


def _run_remaining(
    snapshot: RunSnapshot, clock: Clock,
    monotonic_clock: MonotonicClock, controller: OperationController | None,
) -> float | None:
    deadline = snapshot.deadline_at
    if snapshot.budget.total_wall_seconds is not None:
        budget_deadline = (
            datetime.fromisoformat(snapshot.created_at)
            + timedelta(seconds=snapshot.budget.total_wall_seconds)
        )
        if deadline is None or datetime.fromisoformat(deadline) > budget_deadline:
            deadline = budget_deadline.isoformat()
    wall_remaining = remaining_wall_seconds(deadline, _now(clock))
    return (
        controller.remaining_run(snapshot.run_id, wall_remaining, monotonic_clock)
        if controller is not None else wall_remaining
    )


def _run_expired(
    snapshot: RunSnapshot, clock: Clock,
    monotonic_clock: MonotonicClock, controller: OperationController | None,
) -> bool:
    remaining = _run_remaining(snapshot, clock, monotonic_clock, controller)
    return remaining is not None and remaining <= 0


def _operation_timeout(
    snapshot: RunSnapshot, clock: Clock, monotonic_clock: MonotonicClock,
    seconds: float, controller: OperationController | None,
) -> float:
    deadline = absolute_deadline(
        monotonic_clock, seconds,
        _run_remaining(snapshot, clock, monotonic_clock, controller),
    )
    assert deadline is not None
    return max(0.0, deadline - monotonic_clock())


def _transcript_over_limit(snapshot: RunSnapshot) -> bool:
    limit = snapshot.budget.transcript_bytes
    return limit is not None and len(snapshot.transcript_json.encode("utf-8")) > limit


def _tool_result(call: ParsedToolCall, status: str, reason: str, clock: Clock) -> ToolExecution:
    timestamp = _now(clock).isoformat()
    return ToolExecution(
        call_id=call.call_id, name=call.name, status=status, result="",
        error=reason, started_at=timestamp, completed_at=timestamp,
        duration_ms=0.0, termination="denied",
    )


def _terminal(
    store: SnapshotCAS, snapshot: RunSnapshot, status: str, reason: str, clock: Clock,
) -> RunCommandResult:
    changes: dict[str, Any] = {"model_inflight": False, "inflight_call_id": None}
    if snapshot.pending_call is not None:
        changes["pending_call"] = None
    next_snapshot = transition_run(snapshot, status, updated_at=_updated_at(snapshot, clock), **changes)
    if not _save(store, snapshot, next_snapshot):
        return _conflict(store.load(snapshot.run_id))
    return _result(next_snapshot, reason)


def _progress(
    store: SnapshotCAS, snapshot: RunSnapshot, clock: Clock, **changes: Any,
) -> RunSnapshot | None:
    next_snapshot = transition_run(snapshot, "running", updated_at=_updated_at(snapshot, clock), **changes)
    return next_snapshot if _save(store, snapshot, next_snapshot) else None


async def _execute_reserved(
    store: SnapshotCAS, snapshot: RunSnapshot, call: ParsedToolCall,
    registry: ToolRegistry, policy: ToolPolicy, clock: Clock,
    *, fingerprint: str, permission: str, registration_revision: int | None,
    monotonic_clock: MonotonicClock, controller: OperationController | None,
    runner: CancellableToolRunner | None,
) -> RunSnapshot | None:
    active_controller = controller or OperationController()
    if _run_expired(snapshot, clock, monotonic_clock, controller):
        expired = _terminal(store, snapshot, "run_expired", "run_deadline", clock)
        return expired.snapshot if expired.status == "run_expired" else None
    definition = registry.get(call.name)
    timeout = _operation_timeout(
        snapshot, clock, monotonic_clock,
        definition.timeout_seconds if definition is not None else DEFAULT_TOOL_TIMEOUT_SECONDS,
        controller,
    )
    if timeout <= 0:
        expired = _terminal(store, snapshot, "run_expired", "run_deadline", clock)
        return expired.snapshot if expired.status == "run_expired" else None
    run_deadline = absolute_deadline(monotonic_clock, None, _run_remaining(snapshot, clock, monotonic_clock, controller))
    tool_deadline = absolute_deadline(monotonic_clock, timeout, _run_remaining(snapshot, clock, monotonic_clock, controller))
    context = ExecutionContext(
        run_id=snapshot.run_id, call_id=call.call_id, run_deadline=run_deadline,
        model_deadline=None, tool_deadline=tool_deadline,
        output_budget_bytes=snapshot.budget.tool_output_bytes,
        cancellation=CancellationToken(),
        monotonic_clock=monotonic_clock,
    )
    cancellable = definition is not None and definition.context_handler and runner is not None
    active_controller.tool_context = context if cancellable else None
    active_controller.tool_runner = runner if cancellable else None
    active_controller.active_legacy_tool = not cancellable
    try:
        execution = await run_tool(
            call.name, call.arguments, call_id=call.call_id, registry=registry,
            policy=policy,
            policy_context=RunPolicyContext(
                approved_tools=frozenset({call.name}),
                allowed_permissions=frozenset(snapshot.allowed_permissions),
            ),
            expected_fingerprint=fingerprint, expected_permission=permission,
            expected_registration_revision=registration_revision,
            output_bytes=snapshot.budget.tool_output_bytes,
            timeout_seconds=timeout,
            execution_context=context if cancellable else None,
            runner=runner if cancellable else None,
            log_result=False,
        )
        if execution.status == "timeout" and cancellable and runner is not None:
            if await active_controller.stop_tool():
                execution = replace(execution, termination="cancelled")
            else:
                _log_tool_result(execution)
                cancelling = transition_run(
                    snapshot, "cancelling", updated_at=_updated_at(snapshot, clock),
                )
                if not _save(store, snapshot, cancelling):
                    return None
                failed = _terminal(
                    store, cancelling, "cancellation_failed",
                    "tool_stop_unacknowledged", clock,
                )
                return failed.snapshot if failed.status == "cancellation_failed" else None
    finally:
        active_controller.tool_context = None
        active_controller.tool_runner = None
        active_controller.active_legacy_tool = False
    _log_tool_result(execution)
    transcript = snapshot.transcript
    transcript.append(execution.tool_message())
    errors = snapshot.errors + (execution.status != "success")
    return _progress(
        store, snapshot, clock, transcript=transcript, errors=errors,
        inflight_call_id=None,
    )


async def _drain_calls(
    store: SnapshotCAS, snapshot: RunSnapshot, registry: ToolRegistry,
    policy: ToolPolicy, clock: Clock, monotonic_clock: MonotonicClock,
    controller: OperationController | None, runner: CancellableToolRunner | None,
) -> RunCommandResult:
    while snapshot.queued_calls:
        if _run_expired(snapshot, clock, monotonic_clock, controller):
            return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
        queued = snapshot.queued_calls
        call = ParsedToolCall(**queued[0])
        tail = queued[1:]
        if call.call_id in snapshot.executed_call_ids:
            return _terminal(store, snapshot, "run_conflict", "duplicate_call_id", clock)
        if snapshot.budget.exceeded("tool_calls", snapshot.tool_calls + 1):
            return _terminal(store, snapshot, "budget_exceeded", "tool_call_limit", clock)
        context = RunPolicyContext(
            approved_tools=frozenset(),
            allowed_permissions=frozenset(snapshot.allowed_permissions),
        )
        decision = policy.evaluate(registry, call.name, context)
        definition = registry.get(call.name)
        if decision.action == "pause" and definition is None:
            return _terminal(store, snapshot, "run_conflict", "tool_unknown", clock)
        if decision.action == "pause" and definition is not None:
            try:
                arguments = _canonical_arguments(definition, call.arguments)
            except (SchemaValidationError, TypeError, ValueError):
                # The normal execution path returns the established validation result.
                decision = policy.evaluate(
                    registry, call.name,
                    RunPolicyContext(
                        approved_tools=frozenset({call.name}),
                        allowed_permissions=frozenset(snapshot.allowed_permissions),
                    ),
                )
            else:
                created = _now(clock)
                pending = PendingToolCall.create(
                    call_id=call.call_id, tool_name=call.name, arguments=arguments,
                    definition_fingerprint=decision.fingerprint or "",
                    permission=decision.permission or "",
                    registry_instance_id=registry.instance_id,
                    registration_revision=decision.registration_revision or 0,
                    transcript_revision=_transcript_revision(snapshot.transcript),
                    nonce=secrets.token_urlsafe(32),
                    created_at=created.isoformat(),
                    expires_at=(created + timedelta(seconds=300)).isoformat(),
                    remaining_calls=[ParsedToolCall(**item) for item in tail],
                )
                next_snapshot = transition_run(
                    snapshot, "approval_required", updated_at=_updated_at(snapshot, clock),
                    tool_calls=snapshot.tool_calls + 1, pending_call=pending,
                    queued_calls_json="[]",
                )
                if not _save(store, snapshot, next_snapshot):
                    return _conflict(store.load(snapshot.run_id))
                return _result(next_snapshot, "approval_required")
        if decision.action == "deny":
            execution = _tool_result(call, "revoked" if decision.reason_code in {"tool_unknown", "tool_revoked", "definition_changed", "registration_changed"} else "denied", decision.reason_code, clock)
            transcript = snapshot.transcript
            transcript.append(execution.tool_message())
            progressed = _progress(
                store, snapshot, clock, transcript=transcript,
                tool_calls=snapshot.tool_calls + 1,
                executed_call_ids=snapshot.executed_call_ids + (call.call_id,),
                queued_calls_json=_canonical_json(tail), errors=snapshot.errors + 1,
            )
            if progressed is None:
                return _conflict(store.load(snapshot.run_id))
            snapshot = progressed
        else:
            # Reserve the exact call before any handler effect. A lost result is not retried.
            reserved = _progress(
                store, snapshot, clock, tool_calls=snapshot.tool_calls + 1,
                executed_call_ids=snapshot.executed_call_ids + (call.call_id,),
                inflight_call_id=call.call_id,
                queued_calls_json=_canonical_json(tail),
            )
            if reserved is None:
                return _conflict(store.load(snapshot.run_id))
            revision = decision.registration_revision
            after = await _execute_reserved(
                store, reserved, call, registry, policy, clock,
                fingerprint=decision.fingerprint or "", permission=decision.permission or "",
                registration_revision=revision,
                monotonic_clock=monotonic_clock, controller=controller, runner=runner,
            )
            if after is None:
                return _conflict(store.load(snapshot.run_id))
            snapshot = after
            if snapshot.status != "running":
                return _result(snapshot, snapshot.status)
        if _transcript_over_limit(snapshot):
            return _terminal(store, snapshot, "budget_exceeded", "transcript_limit", clock)
        if snapshot.errors >= snapshot.budget.errors:
            return _terminal(store, snapshot, "error_budget", "error_budget", clock)
        if _run_expired(snapshot, clock, monotonic_clock, controller):
            return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
    return _result(snapshot, "calls_drained")


async def resume_run(
    store: SnapshotCAS, run_id: str, registry: ToolRegistry, invoke_model: ModelInvoker,
    *, policy: ToolPolicy | None = None, clock: Clock = _utc_now,
    model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS,
    monotonic_clock: MonotonicClock = monotonic_now,
    controller: OperationController | None = None,
    runner: CancellableToolRunner | None = None,
) -> RunCommandResult:
    """Advance serializable state with fresh host dependencies, never replaying a reserved effect."""
    controller = controller or OperationController()
    current_policy = policy or ToolPolicy()
    snapshot = store.load(run_id)
    if snapshot is None:
        return _result(None, "run_not_found")
    if snapshot.status in TERMINAL_RUN_STATUSES:
        return _result(snapshot, "run_not_running")
    if _run_expired(snapshot, clock, monotonic_clock, controller):
        return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
    if snapshot.status == "created":
        started = transition_run(snapshot, "running", updated_at=_updated_at(snapshot, clock))
        if not _save(store, snapshot, started):
            return _conflict(store.load(run_id))
        snapshot = started
    if snapshot.status != "running":
        return _result(snapshot, "run_not_running")
    if snapshot.inflight_call_id is not None or snapshot.model_inflight:
        return _conflict(snapshot)
    while True:
        if _run_expired(snapshot, clock, monotonic_clock, controller):
            return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
        if _transcript_over_limit(snapshot):
            return _terminal(store, snapshot, "budget_exceeded", "transcript_limit", clock)
        if snapshot.queued_calls:
            drained = await _drain_calls(
                store, snapshot, registry, current_policy, clock,
                monotonic_clock, controller, runner,
            )
            if drained.status != "running" or drained.snapshot is None:
                return drained
            snapshot = drained.snapshot
            if _run_expired(snapshot, clock, monotonic_clock, controller):
                return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
        if snapshot.steps >= snapshot.budget.model_steps:
            return _terminal(store, snapshot, "step_limit", "step_limit", clock)
        next_step = _progress(
            store, snapshot, clock, steps=snapshot.steps + 1, model_inflight=True,
        )
        if next_step is None:
            return _conflict(store.load(run_id))
        snapshot = next_step
        try:
            timeout = _operation_timeout(
                snapshot, clock, monotonic_clock, model_timeout_seconds, controller,
            )
            if timeout <= 0:
                return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
            task = asyncio.create_task(_invoke_model(
                invoke_model, snapshot.transcript, registry.model_schemas(), timeout,
            ))
            if controller is not None:
                controller.model_task = task
                controller.model_cancellable = inspect.iscoroutinefunction(invoke_model)
            message = await task
        except asyncio.TimeoutError:
            if _run_expired(snapshot, clock, monotonic_clock, controller):
                return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
            return _terminal(store, snapshot, "model_timeout", "model_timeout", clock)
        except asyncio.CancelledError:
            if controller is not None and controller.token.is_requested:
                return _result(store.load(run_id), "cancellation_requested")
            raise
        except Exception:
            return _terminal(store, snapshot, "model_error", "model_error", clock)
        finally:
            if controller is not None:
                controller.model_task = None
                controller.model_cancellable = False
        if _run_expired(snapshot, clock, monotonic_clock, controller):
            return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
        content = message.get("content")
        calls, parse_error = parse_tool_calls(message)
        transcript = snapshot.transcript
        assistant = dict(message)
        assistant["role"] = "assistant"
        if parse_error:
            transcript.append(assistant)
            transcript.append({
                "role": "system",
                "content": "The prior tool call was malformed and was not executed. Return a valid tool call or final answer.",
            })
            next_snapshot = _progress(
                store, snapshot, clock, transcript=transcript, errors=snapshot.errors + 1,
                model_inflight=False,
            )
            if next_snapshot is None:
                return _conflict(store.load(run_id))
            snapshot = next_snapshot
            if snapshot.errors >= snapshot.budget.errors:
                return _terminal(store, snapshot, "error_budget", "error_budget", clock)
            continue
        if not calls:
            transcript.append(assistant)
            done = transition_run(
                snapshot, "completed", updated_at=_updated_at(snapshot, clock),
                transcript=transcript, last_content=content if isinstance(content, str) else "",
                model_inflight=False,
            )
            if not _save(store, snapshot, done):
                return _conflict(store.load(run_id))
            return _result(done, "completed")
        assistant["content"] = content if isinstance(content, str) else ""
        assistant["tool_calls"] = [call.as_openai_call() for call in calls]
        transcript.append(assistant)
        next_snapshot = _progress(
            store, snapshot, clock, transcript=transcript,
            queued_calls_json=_canonical_json([asdict(call) for call in calls]),
            model_inflight=False,
            last_content=content if isinstance(content, str) and content.strip() else snapshot.last_content,
        )
        if next_snapshot is None:
            return _conflict(store.load(run_id))
        snapshot = next_snapshot


async def decide_run(
    store: SnapshotCAS, decision: ApprovalDecision, registry: ToolRegistry,
    invoke_model: ModelInvoker, *, policy: ToolPolicy | None = None,
    clock: Clock = _utc_now, model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS,
    monotonic_clock: MonotonicClock = monotonic_now,
    controller: OperationController | None = None,
    runner: CancellableToolRunner | None = None,
) -> RunCommandResult:
    """Consume an exact decision through CAS before any tool effect."""
    controller = controller or OperationController()
    snapshot = store.load(decision.run_id)
    if snapshot is None:
        return _result(None, "run_not_found")
    if snapshot.status != "approval_required" or snapshot.pending_call is None:
        return _result(snapshot, "approval_not_pending")
    if _run_expired(snapshot, clock, monotonic_clock, controller):
        return _terminal(store, snapshot, "run_expired", "run_deadline", clock)
    pending = snapshot.pending_call
    now = _now(clock)
    if now >= datetime.fromisoformat(pending.expires_at) or now >= datetime.fromisoformat(decision.expires_at):
        return _terminal(store, snapshot, "run_expired", "approval_expired", clock)
    if decision.decision_id in snapshot.used_decision_ids:
        return _result(snapshot, "approval_replayed")
    exact = (
        decision.run_id == snapshot.run_id
        and decision.call_id == pending.call_id
        and hmac.compare_digest(decision.digest, pending.digest)
        and hmac.compare_digest(decision.definition_fingerprint, pending.definition_fingerprint)
        and decision.permission == pending.permission
        and hmac.compare_digest(decision.nonce, pending.nonce)
        and datetime.fromisoformat(pending.created_at) <= datetime.fromisoformat(decision.issued_at) <= now
        and datetime.fromisoformat(decision.expires_at) <= datetime.fromisoformat(pending.expires_at)
        and _transcript_revision(snapshot.transcript) == pending.transcript_revision
        and pending.call_id not in snapshot.executed_call_ids
    )
    if not exact:
        return _result(snapshot, "approval_mismatch")
    if decision.decision == "reject":
        rejected = transition_run(
            snapshot, "approval_rejected", updated_at=_updated_at(snapshot, clock),
            pending_call=None,
            used_decision_ids=snapshot.used_decision_ids + (decision.decision_id,),
        )
        if not _save(store, snapshot, rejected):
            return _conflict(store.load(snapshot.run_id))
        return _result(rejected, "approval_rejected")

    current_policy = policy or ToolPolicy()
    registration_revision = pending.registration_revision if pending.registry_instance_id == registry.instance_id else None
    checked = current_policy.evaluate(
        registry, pending.tool_name,
        RunPolicyContext(
            approved_tools=frozenset({pending.tool_name}),
            allowed_permissions=frozenset(snapshot.allowed_permissions),
        ),
        expected_fingerprint=pending.definition_fingerprint,
        expected_permission=pending.permission,
        expected_registration_revision=registration_revision,
    )
    if checked.action != "allow":
        return _terminal(store, snapshot, "run_conflict", checked.reason_code, clock)
    reserved = transition_run(
        snapshot, "running", updated_at=_updated_at(snapshot, clock), pending_call=None,
        executed_call_ids=snapshot.executed_call_ids + (pending.call_id,),
        used_decision_ids=snapshot.used_decision_ids + (decision.decision_id,),
        inflight_call_id=pending.call_id,
        queued_calls_json=pending.remaining_calls_json,
    )
    if not _save(store, snapshot, reserved):
        return _conflict(store.load(snapshot.run_id))
    call = ParsedToolCall(pending.call_id, pending.tool_name, pending.arguments)
    after = await _execute_reserved(
        store, reserved, call, registry, current_policy, clock,
        fingerprint=pending.definition_fingerprint, permission=pending.permission,
        registration_revision=registration_revision,
        monotonic_clock=monotonic_clock, controller=controller, runner=runner,
    )
    if after is None:
        return _conflict(store.load(snapshot.run_id))
    if after.status != "running":
        return _result(after, after.status)
    if after.errors >= after.budget.errors:
        return _terminal(store, after, "error_budget", "error_budget", clock)
    return await resume_run(
        store, decision.run_id, registry, invoke_model, policy=current_policy,
        clock=clock, model_timeout_seconds=model_timeout_seconds,
        monotonic_clock=monotonic_clock, controller=controller, runner=runner,
    )


async def cancel_run(
    store: SnapshotCAS, run_id: str, *, controller: OperationController | None = None,
    clock: Clock = _utc_now,
) -> RunCommandResult:
    """Cancel only after active work acknowledges stop; never infer a hard kill."""
    snapshot = store.load(run_id)
    if snapshot is None:
        return _result(None, "run_not_found")
    if snapshot.status != "running":
        return _result(snapshot, "run_not_running")
    cancelling = transition_run(
        snapshot, "cancelling", updated_at=_updated_at(snapshot, clock),
    )
    if not _save(store, snapshot, cancelling):
        return _conflict(store.load(run_id))
    active = snapshot.model_inflight or snapshot.inflight_call_id is not None
    if controller is None:
        stopped = not active
    elif active and (
        controller.model_task is None and controller.tool_context is None
        and not controller.active_legacy_tool
    ):
        stopped = False
    else:
        stopped = await controller.stop()
    terminal = "cancelled" if stopped else "cancellation_failed"
    final = transition_run(
        cancelling, terminal, updated_at=_updated_at(cancelling, clock),
        model_inflight=False, inflight_call_id=None, queued_calls_json="[]",
    )
    if not _save(store, cancelling, final):
        return _conflict(store.load(run_id))
    return _result(final, terminal)
