"""Versioned, host-storable run state and exact approval values."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, cast

from .budgets import RunBudget
from .contracts import CONTRACT_VERSION, ContractDecodeError, ParsedToolCall


RUN_STATE_VERSION = 1
RUN_STATUSES = frozenset({
    "created", "running", "approval_required", "completed", "model_timeout",
    "model_error", "error_budget", "step_limit", "budget_exceeded",
    "cancelling", "cancelled", "cancellation_failed", "approval_rejected",
    "run_conflict", "run_expired",
})
TERMINAL_RUN_STATUSES = frozenset({
    "completed", "model_timeout", "model_error", "error_budget", "step_limit",
    "budget_exceeded", "cancelled", "cancellation_failed", "approval_rejected",
    "run_conflict", "run_expired",
})
_TRANSITIONS = {
    "created": frozenset({"running", "run_expired"}),
    "running": frozenset({
        "approval_required", "completed", "model_timeout", "model_error",
        "error_budget", "step_limit", "budget_exceeded", "cancelling",
        "run_conflict", "run_expired",
    }),
    "approval_required": frozenset({"running", "approval_rejected", "run_expired", "run_conflict"}),
    "cancelling": frozenset({"cancelled", "cancellation_failed", "run_conflict"}),
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def _exact_fields(value: Any, names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != names:
        raise ContractDecodeError("run-state fields must be exact")
    return value


@dataclass(frozen=True, slots=True)
class PendingToolCall:
    call_id: str
    tool_name: str
    arguments_json: str
    digest: str
    definition_fingerprint: str
    permission: str
    registry_instance_id: str
    registration_revision: int
    transcript_revision: str
    nonce: str
    created_at: str
    expires_at: str
    remaining_calls_json: str = "[]"
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if type(self.contract_version) is not int or self.contract_version != CONTRACT_VERSION:
            raise ValueError("unsupported pending-call version")
        for name in ("call_id", "tool_name", "digest", "definition_fingerprint", "permission", "registry_instance_id", "transcript_revision", "nonce"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be nonempty")
        if type(self.registration_revision) is not int or self.registration_revision < 1:
            raise ValueError("registration_revision must be positive")
        if not isinstance(self.arguments, dict):
            raise ValueError("arguments must be an object")
        if _canonical(self.arguments) != self.arguments_json:
            raise ValueError("arguments must be canonical finite JSON")
        if hashlib.sha256(self.arguments_json.encode("utf-8")).hexdigest() != self.digest:
            raise ValueError("pending argument digest mismatch")
        if not isinstance(self.remaining_calls, list):
            raise ValueError("remaining calls must be an array")
        if _canonical(self.remaining_calls) != self.remaining_calls_json:
            raise ValueError("remaining calls must be canonical finite JSON")
        for call in self.remaining_calls:
            ParsedToolCall(**call)
        if _timestamp(self.expires_at) <= _timestamp(self.created_at):
            raise ValueError("pending call expiry must follow creation")

    @property
    def arguments(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.arguments_json))

    @property
    def remaining_calls(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], json.loads(self.remaining_calls_json))

    @classmethod
    def create(
        cls, *, call_id: str, tool_name: str, arguments: dict[str, Any],
        definition_fingerprint: str, permission: str, registry_instance_id: str,
        registration_revision: int, transcript_revision: str, nonce: str,
        created_at: str, expires_at: str,
        remaining_calls: list[ParsedToolCall] | None = None,
    ) -> PendingToolCall:
        arguments_json = _canonical(arguments)
        return cls(
            call_id=call_id, tool_name=tool_name, arguments_json=arguments_json,
            digest=hashlib.sha256(arguments_json.encode("utf-8")).hexdigest(),
            definition_fingerprint=definition_fingerprint, permission=permission,
            registry_instance_id=registry_instance_id,
            registration_revision=registration_revision,
            transcript_revision=transcript_revision, nonce=nonce,
            created_at=created_at, expires_at=expires_at,
            remaining_calls_json=_canonical([asdict(call) for call in remaining_calls or []]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "call_id": self.call_id,
            "tool_name": self.tool_name, "arguments": self.arguments,
            "digest": self.digest, "definition_fingerprint": self.definition_fingerprint,
            "permission": self.permission, "registry_instance_id": self.registry_instance_id,
            "registration_revision": self.registration_revision,
            "transcript_revision": self.transcript_revision, "nonce": self.nonce,
            "created_at": self.created_at, "expires_at": self.expires_at,
            "remaining_calls": self.remaining_calls,
        }

    @classmethod
    def from_dict(cls, value: Any) -> PendingToolCall:
        names = {"contract_version", "call_id", "tool_name", "arguments", "digest", "definition_fingerprint", "permission", "registry_instance_id", "registration_revision", "transcript_revision", "nonce", "created_at", "expires_at", "remaining_calls"}
        data = _exact_fields(value, names)
        if type(data["contract_version"]) is not int or data["contract_version"] != CONTRACT_VERSION:
            raise ContractDecodeError("unsupported pending-call version")
        try:
            return cls(
                contract_version=data["contract_version"], call_id=data["call_id"],
                tool_name=data["tool_name"], arguments_json=_canonical(data["arguments"]),
                digest=data["digest"], definition_fingerprint=data["definition_fingerprint"],
                permission=data["permission"], registry_instance_id=data["registry_instance_id"],
                registration_revision=data["registration_revision"],
                transcript_revision=data["transcript_revision"], nonce=data["nonce"],
                created_at=data["created_at"], expires_at=data["expires_at"],
                remaining_calls_json=_canonical(data["remaining_calls"]),
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ContractDecodeError(f"invalid pending call: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    run_id: str
    call_id: str
    digest: str
    definition_fingerprint: str
    permission: str
    nonce: str
    decision_id: str
    decision: str
    issued_at: str
    expires_at: str
    contract_version: int = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if type(self.contract_version) is not int or self.contract_version != CONTRACT_VERSION:
            raise ValueError("unsupported approval version")
        for name in ("run_id", "call_id", "digest", "definition_fingerprint", "permission", "nonce", "decision_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be nonempty")
        if self.decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        if _timestamp(self.expires_at) <= _timestamp(self.issued_at):
            raise ValueError("decision expiry must follow issue time")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> ApprovalDecision:
        data = _exact_fields(value, set(cls.__dataclass_fields__))
        try:
            return cls(**data)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ContractDecodeError(f"invalid approval decision: {exc}") from exc


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    status: str
    optimistic_version: int
    transcript_json: str
    steps: int
    errors: int
    tool_calls: int
    created_at: str
    updated_at: str
    deadline_at: str | None
    pending_call: PendingToolCall | None
    executed_call_ids: tuple[str, ...]
    event_cursor: int
    budget: RunBudget
    allowed_permissions: tuple[str, ...]
    queued_calls_json: str = "[]"
    inflight_call_id: str | None = None
    model_inflight: bool = False
    used_decision_ids: tuple[str, ...] = ()
    last_content: str | None = None
    contract_version: int = RUN_STATE_VERSION

    def __post_init__(self) -> None:
        if type(self.contract_version) is not int or self.contract_version != RUN_STATE_VERSION:
            raise ValueError("unsupported run-state version")
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("run_id must be nonempty")
        if self.status not in RUN_STATUSES:
            raise ValueError("unknown run status")
        for name in ("optimistic_version", "steps", "errors", "tool_calls", "event_cursor"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.optimistic_version < 1:
            raise ValueError("optimistic_version must be positive")
        if not isinstance(self.transcript, list) or any(not isinstance(item, dict) for item in self.transcript):
            raise ValueError("transcript must be an array of objects")
        if _canonical(self.transcript) != self.transcript_json:
            raise ValueError("transcript must be canonical finite JSON")
        if _timestamp(self.updated_at) < _timestamp(self.created_at):
            raise ValueError("updated_at precedes created_at")
        if self.deadline_at is not None:
            _timestamp(self.deadline_at)
        if self.status == "approval_required" and self.pending_call is None:
            raise ValueError("approval_required needs a pending call")
        if self.status != "approval_required" and self.pending_call is not None:
            raise ValueError("pending call requires approval_required")
        if type(self.model_inflight) is not bool:
            raise ValueError("model_inflight must be a boolean")
        if self.model_inflight and (self.status != "running" or self.steps < 1):
            raise ValueError("model invocation must be reserved in running state")
        if self.status == "created" and (
            self.steps or self.errors or self.tool_calls or self.executed_call_ids
            or self.used_decision_ids or self.queued_calls or self.inflight_call_id
        ):
            raise ValueError("created run cannot have progress")
        if self.status == "approval_required" and (
            self.steps < 1 or self.tool_calls < 1 or self.queued_calls
            or self.inflight_call_id or self.model_inflight
        ):
            raise ValueError("approval state must follow a tool call")
        if self.status == "completed" and (
            self.steps < 1 or self.queued_calls or self.inflight_call_id or self.model_inflight
        ):
            raise ValueError("completed run must finish model and tool work")
        if self.pending_call is not None and self.pending_call.call_id in self.executed_call_ids:
            raise ValueError("pending call is already reserved")
        if self.inflight_call_id is not None and (self.status != "running" or self.inflight_call_id not in self.executed_call_ids):
            raise ValueError("in-flight call must be reserved in running state")
        if len(set(self.executed_call_ids)) != len(self.executed_call_ids):
            raise ValueError("executed call IDs must be unique")
        if len(set(self.used_decision_ids)) != len(self.used_decision_ids):
            raise ValueError("decision IDs must be unique")
        if not isinstance(self.queued_calls, list):
            raise ValueError("queued calls must be an array")
        if _canonical(self.queued_calls) != self.queued_calls_json:
            raise ValueError("queued calls must be canonical finite JSON")
        for call in self.queued_calls:
            ParsedToolCall(**call)
        if not isinstance(self.budget, RunBudget):
            raise ValueError("budget must be a RunBudget")
        for values in (self.executed_call_ids, self.allowed_permissions, self.used_decision_ids):
            if not isinstance(values, tuple) or any(not isinstance(value, str) or not value for value in values):
                raise ValueError("ledger and policy values must be string tuples")
        if self.last_content is not None and not isinstance(self.last_content, str):
            raise ValueError("last_content must be a string or null")

    @property
    def transcript(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], json.loads(self.transcript_json))

    @property
    def queued_calls(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], json.loads(self.queued_calls_json))

    @classmethod
    def create(
        cls, *, run_id: str, transcript: list[dict[str, Any]], created_at: str,
        budget: RunBudget | None = None, deadline_at: str | None = None,
        allowed_permissions: tuple[str, ...] = (),
    ) -> RunSnapshot:
        return cls(
            run_id=run_id, status="created", optimistic_version=1,
            transcript_json=_canonical(transcript), steps=0, errors=0, tool_calls=0,
            created_at=created_at, updated_at=created_at, deadline_at=deadline_at,
            pending_call=None, executed_call_ids=(), event_cursor=0,
            budget=budget or RunBudget(), allowed_permissions=allowed_permissions,
            queued_calls_json="[]",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version, "run_id": self.run_id,
            "status": self.status, "optimistic_version": self.optimistic_version,
            "transcript": self.transcript, "steps": self.steps, "errors": self.errors,
            "tool_calls": self.tool_calls, "created_at": self.created_at,
            "updated_at": self.updated_at, "deadline_at": self.deadline_at,
            "pending_call": self.pending_call.to_dict() if self.pending_call else None,
            "executed_call_ids": list(self.executed_call_ids),
            "event_cursor": self.event_cursor, "budget": asdict(self.budget),
            "allowed_permissions": list(self.allowed_permissions),
            "queued_calls": self.queued_calls, "inflight_call_id": self.inflight_call_id,
            "model_inflight": self.model_inflight,
            "used_decision_ids": list(self.used_decision_ids),
            "last_content": self.last_content,
        }

    @classmethod
    def from_dict(cls, value: Any) -> RunSnapshot:
        names = {"contract_version", "run_id", "status", "optimistic_version", "transcript", "steps", "errors", "tool_calls", "created_at", "updated_at", "deadline_at", "pending_call", "executed_call_ids", "event_cursor", "budget", "allowed_permissions", "queued_calls", "inflight_call_id", "model_inflight", "used_decision_ids", "last_content"}
        data = _exact_fields(value, names)
        if type(data["contract_version"]) is not int or data["contract_version"] != RUN_STATE_VERSION:
            raise ContractDecodeError("unsupported run-state version")
        try:
            budget_data = _exact_fields(data["budget"], set(RunBudget.__dataclass_fields__))
            if not all(isinstance(data[name], list) for name in ("executed_call_ids", "allowed_permissions", "queued_calls", "used_decision_ids")):
                raise ValueError("ledger and policy arrays are required")
            return cls(
                contract_version=data["contract_version"], run_id=data["run_id"],
                status=data["status"], optimistic_version=data["optimistic_version"],
                transcript_json=_canonical(data["transcript"]), steps=data["steps"],
                errors=data["errors"], tool_calls=data["tool_calls"],
                created_at=data["created_at"], updated_at=data["updated_at"],
                deadline_at=data["deadline_at"],
                pending_call=PendingToolCall.from_dict(data["pending_call"]) if data["pending_call"] is not None else None,
                executed_call_ids=tuple(data["executed_call_ids"]),
                event_cursor=data["event_cursor"], budget=RunBudget(**budget_data),
                allowed_permissions=tuple(data["allowed_permissions"]),
                queued_calls_json=_canonical(data["queued_calls"]),
                inflight_call_id=data["inflight_call_id"],
                model_inflight=data["model_inflight"],
                used_decision_ids=tuple(data["used_decision_ids"]),
                last_content=data["last_content"],
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise ContractDecodeError(f"invalid run snapshot: {exc}") from exc


def transition_run(snapshot: RunSnapshot, status: str, *, updated_at: str, **changes: Any) -> RunSnapshot:
    """Apply the only legal status/version transition, including running progress."""
    if snapshot.status in TERMINAL_RUN_STATUSES:
        if status == snapshot.status and not changes:
            return snapshot
        raise ValueError("terminal run state is immutable")
    if status == snapshot.status and not changes:
        return snapshot
    if status != snapshot.status and status not in _TRANSITIONS.get(snapshot.status, frozenset()):
        raise ValueError(f"illegal run transition: {snapshot.status} -> {status}")
    if status == snapshot.status and status != "running":
        raise ValueError("only running state can receive progress updates")
    if "optimistic_version" in changes or "status" in changes or "updated_at" in changes:
        raise ValueError("transition-owned fields cannot be overridden")
    if "transcript" in changes:
        changes["transcript_json"] = _canonical(changes.pop("transcript"))
    return replace(
        snapshot, status=status, optimistic_version=snapshot.optimistic_version + 1,
        updated_at=updated_at, **changes,
    )
