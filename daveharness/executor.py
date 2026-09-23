"""Compatibility re-exports for the original DaveHarness executor module."""

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
    DEFAULT_PENDING_CALL_STORE,
    DEFAULT_STEP_LIMIT,
    InMemoryPendingCallStore,
    ModelInvoker,
    PENDING_CALL_TTL_SECONDS,
    PendingCallClaim,
    PendingCallStore,
    resume_executor_loop,
    run_executor_loop,
    run_tool,
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
