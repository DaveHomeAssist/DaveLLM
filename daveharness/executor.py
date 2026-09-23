"""Compatibility re-exports for the original DaveHarness executor module."""

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
from .registry import (
    DEFAULT_TOOL_REGISTRY,
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    ToolDefinition,
    ToolHandler,
    ToolRegistry,
)
from .schema import SchemaValidationError, validate_json_schema

__all__ = [
    "CONTRACT_VERSION",
    "ContractDecodeError",
    "DEFAULT_ERROR_BUDGET",
    "DEFAULT_MODEL_TIMEOUT_SECONDS",
    "DEFAULT_PENDING_CALL_STORE",
    "DEFAULT_STEP_LIMIT",
    "DEFAULT_TOOL_REGISTRY",
    "DEFAULT_TOOL_TIMEOUT_SECONDS",
    "ExecutorOutcome",
    "InMemoryPendingCallStore",
    "ModelInvoker",
    "PENDING_CALL_TTL_SECONDS",
    "PendingCall",
    "PendingCallClaim",
    "PendingCallStore",
    "ParsedToolCall",
    "SchemaValidationError",
    "ToolDefinition",
    "ToolExecution",
    "ToolHandler",
    "ToolRegistry",
    "decode_contract",
    "encode_contract",
    "parse_tool_calls",
    "resume_executor_loop",
    "run_executor_loop",
    "run_tool",
    "utc_timestamp",
    "validate_json_schema",
]
