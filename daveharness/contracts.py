"""Typed DaveHarness leaf contracts and their versioned wire format."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .limits import validate_payload


CONTRACT_VERSION = 1


class ContractDecodeError(ValueError):
    """Reject an unsupported or malformed versioned contract before use."""


def _nonempty(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _string(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")


def _optional_string(value: Any, field: str) -> None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")


def _timestamp(value: Any, field: str) -> None:
    _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")


def _finite_nonnegative(value: Any, field: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or value < 0
    ):
        raise ValueError(f"{field} must be a finite non-negative number")


def _nonnegative_integer(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")


def _copy_json(value: Any, field: str, ancestors: set[int] | None = None) -> Any:
    """Validate and own only JSON-compatible values, including nested values."""
    if ancestors is None:
        validate_payload(value)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must contain finite JSON numbers")
        return value
    if not isinstance(value, (dict, list)):
        raise ValueError(f"{field} must contain JSON-compatible values")
    seen = set() if ancestors is None else ancestors
    identity = id(value)
    if identity in seen:
        raise ValueError(f"{field} must not contain cycles")
    seen.add(identity)
    try:
        if isinstance(value, list):
            return [_copy_json(item, f"{field}[{index}]", seen) for index, item in enumerate(value)]
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} must use string object keys")
            result[key] = _copy_json(item, f"{field}.{key}", seen)
        return result
    finally:
        seen.remove(identity)


def _copy_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a JSON object")
    copied: dict[str, Any] = _copy_json(value, field)
    return copied


def _copy_transcript(value: Any) -> list[dict[str, Any]]:
    validate_payload(value)
    if not isinstance(value, list):
        raise ValueError("transcript must be a JSON array")
    return [_copy_object(message, f"transcript[{index}]") for index, message in enumerate(value)]


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

    def __post_init__(self) -> None:
        for field in ("call_id", "status", "termination"):
            _nonempty(getattr(self, field), field)
        _string(self.name, "name")
        if not isinstance(self.result, str):
            raise ValueError("result must be a string")
        _optional_string(self.error, "error")
        _timestamp(self.started_at, "started_at")
        _timestamp(self.completed_at, "completed_at")
        _finite_nonnegative(self.duration_ms, "duration_ms")

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


@dataclass(frozen=True, slots=True)
class ParsedToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        _nonempty(self.call_id, "call_id")
        _string(self.name, "name")
        object.__setattr__(self, "arguments", _copy_object(self.arguments, "arguments"))

    def as_openai_call(self) -> dict[str, Any]:
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


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

    def __post_init__(self) -> None:
        for field in ("call_id", "tool_name", "digest", "transcript_revision", "nonce"):
            _nonempty(getattr(self, field), field)
        object.__setattr__(self, "arguments", _copy_object(self.arguments, "arguments"))
        _timestamp(self.created_at, "created_at")
        _timestamp(self.expires_at, "expires_at")

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
class ExecutorOutcome:
    status: str
    transcript: list[dict[str, Any]]
    final_answer: str | None
    steps: int
    errors: int
    status_message: str
    pending_tool_call: dict[str, Any] | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        _nonempty(self.status, "status")
        _optional_string(self.final_answer, "final_answer")
        _nonnegative_integer(self.steps, "steps")
        _nonnegative_integer(self.errors, "errors")
        if not isinstance(self.status_message, str):
            raise ValueError("status_message must be a string")
        if self.run_id is not None:
            _nonempty(self.run_id, "run_id")
        object.__setattr__(self, "transcript", _copy_transcript(self.transcript))
        if self.pending_tool_call is not None:
            object.__setattr__(
                self,
                "pending_tool_call",
                _copy_object(self.pending_tool_call, "pending_tool_call"),
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def reason_code(self) -> str:
        """Expose a stable in-process reason without changing legacy wire fields."""
        return self.status_message if self.status == "budget_exceeded" else self.status


ContractValue = ParsedToolCall | PendingCall | ToolExecution | ExecutorOutcome

_CONTRACT_FIELDS: dict[str, frozenset[str]] = {
    "ParsedToolCall": frozenset({"call_id", "name", "arguments"}),
    "PendingCall": frozenset(
        {"call_id", "tool_name", "arguments", "digest", "transcript_revision", "nonce", "created_at", "expires_at"}
    ),
    "ToolExecution": frozenset(
        {"call_id", "name", "status", "result", "error", "started_at", "completed_at", "duration_ms", "termination"}
    ),
    "ExecutorOutcome": frozenset(
        {"status", "transcript", "final_answer", "steps", "errors", "status_message", "pending_tool_call", "run_id"}
    ),
}


def _checked_contract(contract_type: str, payload: Any) -> ContractValue:
    if not isinstance(payload, dict):
        raise ContractDecodeError("payload must be a JSON object")
    if any(not isinstance(key, str) for key in payload):
        raise ContractDecodeError("payload keys must be strings")
    expected = _CONTRACT_FIELDS[contract_type]
    if payload.keys() != expected:
        missing = sorted(expected - payload.keys())
        extra = sorted(payload.keys() - expected)
        raise ContractDecodeError(f"payload fields mismatch: missing={missing}, extra={extra}")
    try:
        if contract_type in {"ParsedToolCall", "ToolExecution"}:
            _nonempty(payload["name"], "name")
        if contract_type == "ParsedToolCall":
            return ParsedToolCall(**payload)
        if contract_type == "PendingCall":
            return PendingCall(**payload)
        if contract_type == "ToolExecution":
            return ToolExecution(**payload)
        return ExecutorOutcome(**payload)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContractDecodeError(f"invalid {contract_type} payload: {exc}") from exc


def encode_contract(value: ContractValue) -> dict[str, Any]:
    """Return a detached v1 envelope without changing legacy response serializers."""
    contract_type = type(value).__name__
    if contract_type not in _CONTRACT_FIELDS or type(value) not in (
        ParsedToolCall, PendingCall, ToolExecution, ExecutorOutcome
    ):
        raise TypeError("unsupported DaveHarness contract type")
    validate_payload({name: getattr(value, name) for name in _CONTRACT_FIELDS[contract_type]})
    checked = _checked_contract(contract_type, asdict(value))
    payload: dict[str, Any] = json.loads(
        json.dumps(asdict(checked), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )
    return {
        "contract_type": contract_type,
        "contract_version": CONTRACT_VERSION,
        "payload": payload,
    }


def decode_contract(envelope: dict[str, Any]) -> ContractValue:
    """Validate the entire v1 envelope before constructing a leaf value."""
    if not isinstance(envelope, dict) or envelope.keys() != {
        "contract_version", "contract_type", "payload"
    }:
        raise ContractDecodeError("contract envelope fields must be exact")
    version = envelope["contract_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != CONTRACT_VERSION:
        raise ContractDecodeError(f"unsupported contract version: {version!r}")
    contract_type = envelope["contract_type"]
    if not isinstance(contract_type, str) or contract_type not in _CONTRACT_FIELDS:
        raise ContractDecodeError(f"unsupported contract type: {contract_type!r}")
    return _checked_contract(contract_type, envelope["payload"])
