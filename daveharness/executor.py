"""Compatibility re-exports for the original DaveHarness executor module."""

from __future__ import annotations

from typing import Any

from .budgets import RunBudget
from .contracts import (
    CONTRACT_VERSION,
    ContractDecodeError,
    ExecutorOutcome,
    ParsedToolCall,
    PendingCall,
    ToolExecution,
    decode_contract,
    encode_contract,
)
from .engine import (
    DEFAULT_ERROR_BUDGET,
    DEFAULT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_STEP_LIMIT,
    InMemoryPendingCallStore,
    ModelInvoker,
    PENDING_CALL_TTL_SECONDS,
    PendingCallClaim,
    PendingCallStore,
    _resume_executor_loop,
    _run_executor_loop,
    _run_tool,
    utc_timestamp,
)
from .parser import parse_tool_calls
from .policy import KNOWN_PERMISSIONS, PolicyDecision, RunPolicyContext, ToolPolicy
from .registry import (
    DEFAULT_TOOL_REGISTRY,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    ToolDefinition,
    ToolHandler,
    ToolRegistry,
)
from .schema import SchemaValidationError, validate_json_schema
from .runtime import (
    CancellationToken, CancellableToolRunner, CooperativeToolRunner,
    ExecutionContext, OperationController, absolute_deadline,
    remaining_wall_seconds,
)
from .state import (
    ApprovalDecision, PendingToolCall, RunSnapshot, RUN_STATE_VERSION,
    RUN_STATUSES, TERMINAL_RUN_STATUSES, transition_run,
)
from .state_engine import RunCommandResult, SnapshotCAS, cancel_run, decide_run, resume_run


# Mutable defaults live only at this compatibility boundary. The state engine,
# H6 facade, and engine implementation pass their dependencies explicitly.
DEFAULT_PENDING_CALL_STORE = InMemoryPendingCallStore()


async def run_tool(
    name: str, args: dict[str, Any], *, call_id: str | None = None,
    registry: ToolRegistry = DEFAULT_TOOL_REGISTRY,
    timeout_seconds: float | None = None, policy: ToolPolicy | None = None,
    policy_context: RunPolicyContext | None = None,
    expected_fingerprint: str | None = None,
    expected_permission: str | None = None,
    expected_registration_revision: int | None = None,
    output_bytes: int | None = None,
    execution_context: ExecutionContext | None = None,
    runner: CancellableToolRunner | None = None,
    log_result: bool = True,
) -> ToolExecution:
    return await _run_tool(
        name, args, call_id=call_id, registry=registry,
        timeout_seconds=timeout_seconds, policy=policy,
        policy_context=policy_context,
        expected_fingerprint=expected_fingerprint,
        expected_permission=expected_permission,
        expected_registration_revision=expected_registration_revision,
        output_bytes=output_bytes, execution_context=execution_context,
        runner=runner, log_result=log_result,
    )


async def run_executor_loop(
    messages: list[dict[str, Any]], invoke_model: ModelInvoker, *,
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
    return await _run_executor_loop(
        messages, invoke_model, registry=registry, step_limit=step_limit,
        error_budget=error_budget, approved_tools=approved_tools,
        model_timeout_seconds=model_timeout_seconds, pending_store=pending_store,
        run_id=run_id, budget=budget, policy=policy,
        policy_context=policy_context,
    )


async def resume_executor_loop(
    *, run_id: str, call_id: str, digest: str, decision: str,
    pending_store: PendingCallStore = DEFAULT_PENDING_CALL_STORE,
) -> ExecutorOutcome:
    return await _resume_executor_loop(
        run_id=run_id, call_id=call_id, digest=digest, decision=decision,
        pending_store=pending_store,
    )

__all__ = [
    "ApprovalDecision",
    "CancellationToken",
    "CancellableToolRunner",
    "CooperativeToolRunner",
    "CONTRACT_VERSION",
    "ContractDecodeError",
    "DEFAULT_ERROR_BUDGET",
    "DEFAULT_MODEL_TIMEOUT_SECONDS",
    "DEFAULT_PENDING_CALL_STORE",
    "DEFAULT_STEP_LIMIT",
    "DEFAULT_TOOL_REGISTRY",
    "DEFAULT_TOOL_TIMEOUT_SECONDS",
    "ExecutorOutcome",
    "ExecutionContext",
    "InMemoryPendingCallStore",
    "ModelInvoker",
    "OperationController",
    "KNOWN_PERMISSIONS",
    "PENDING_CALL_TTL_SECONDS",
    "PendingCall",
    "PendingCallClaim",
    "PendingCallStore",
    "PolicyDecision",
    "ParsedToolCall",
    "SchemaValidationError",
    "RunBudget",
    "RunCommandResult",
    "RunSnapshot",
    "RUN_STATE_VERSION",
    "RUN_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "RunPolicyContext",
    "ToolDefinition",
    "ToolExecution",
    "ToolHandler",
    "ToolRegistry",
    "ToolPolicy",
    "PendingToolCall",
    "SnapshotCAS",
    "absolute_deadline",
    "cancel_run",
    "decide_run",
    "decode_contract",
    "encode_contract",
    "parse_tool_calls",
    "resume_executor_loop",
    "resume_run",
    "remaining_wall_seconds",
    "run_executor_loop",
    "run_tool",
    "utc_timestamp",
    "transition_run",
    "validate_json_schema",
]
