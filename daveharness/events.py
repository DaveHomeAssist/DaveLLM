"""Metadata-only lifecycle events and bounded, host-owned delivery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Protocol


EVENT_VERSION = 1
EVENT_KINDS = frozenset({
    "created", "run_started", "model_start", "model_result", "parse_repair",
    "policy_decision", "approval_required", "approval_decision", "tool_start",
    "tool_result", "cancellation_requested", "budget_stop", "terminal",
    "truncated", "sink_failed",
})
_SAFE_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")
_SAFE_STATUS = frozenset({
    "created", "running", "approval_required", "completed", "cancelled",
    "cancelling", "cancellation_failed", "run_expired", "run_conflict",
    "budget_exceeded", "error_budget", "step_limit", "model_timeout",
    "model_error", "success", "denied", "revoked", "timeout",
    "validation_error", "execution_error", "error", "output_limit",
    "approve", "reject", "allow", "deny", "pause",
    "approval_rejected",
})
_SAFE_REASON = frozenset({
    "completed", "approval_required", "approval_rejected", "approval_expired",
    "run_deadline", "tool_stop_unacknowledged", "duplicate_call_id", "tool_call_limit",
    "tool_unknown", "transcript_limit", "error_budget", "step_limit", "model_timeout",
    "model_error", "cancellation_failed", "cancelled", "malformed_tool_call",
    "tool_revoked", "definition_changed", "registration_changed", "approved",
    "rejected", "tool_denied", "sink_error", "event_limit", "event_byte_limit",
    "permission_changed", "permission_unknown", "permission_denied",
    "approval_granted", "policy_allowed",
})


def safe_identifier(value: str | None) -> str | None:
    """Expose ordinary IDs while replacing hostile/free-form values with a stable digest."""
    if value is None:
        return None
    sensitive_words = ("secret", "token", "password", "key", "canary", "prompt", "argument", "result", "private")
    if _SAFE_LABEL.fullmatch(value) and not any(part in value.lower() for part in sensitive_words):
        return value
    return "redacted_" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def safe_status(value: str | None) -> str | None:
    return value if value in _SAFE_STATUS else None


def safe_reason(value: str | None) -> str | None:
    return value if value in _SAFE_REASON else None


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    sequence: int
    kind: str
    step: int
    call_id: str | None
    tool_name: str | None
    status: str | None
    reason_code: str | None
    timestamp: str
    duration_ms: float | None = None
    input_bytes: int | None = None
    output_bytes: int | None = None
    event_version: int = EVENT_VERSION

    def __post_init__(self) -> None:
        if type(self.event_version) is not int or self.event_version != EVENT_VERSION:
            raise ValueError("unsupported event version")
        if not isinstance(self.run_id, str) or safe_identifier(self.run_id) != self.run_id:
            raise ValueError("run_id must be a safe identifier")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequence must be positive")
        if self.kind not in EVENT_KINDS:
            raise ValueError("unknown event kind")
        if type(self.step) is not int or self.step < 0:
            raise ValueError("step must be nonnegative")
        for name in ("call_id", "tool_name"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or safe_identifier(value) != value):
                raise ValueError(f"{name} is not a safe identifier")
        if self.status is not None and safe_status(self.status) != self.status:
            raise ValueError("unsafe status")
        if self.reason_code is not None and safe_reason(self.reason_code) != self.reason_code:
            raise ValueError("unsafe reason")
        if not isinstance(self.timestamp, str) or len(self.timestamp) > 64:
            raise ValueError("timestamp must be a string")
        try:
            parsed = datetime.fromisoformat(self.timestamp)
        except ValueError as exc:
            raise ValueError("invalid timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        for name in ("input_bytes", "output_bytes"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be nonnegative")
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, (int, float))
            or not 0 <= self.duration_ms < float("inf")
        ):
            raise ValueError("duration must be finite and nonnegative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> RunEvent:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("event fields do not match version 1")
        return cls(**value)

    @property
    def byte_size(self) -> int:
        return len(json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"))


class EventSink(Protocol):
    async def emit(self, event: RunEvent) -> None: ...


class NoopEventSink:
    async def emit(self, event: RunEvent) -> None:
        return None


@dataclass(frozen=True, slots=True)
class EventNote:
    kind: str
    call_id: str | None = None
    tool_name: str | None = None
    status: str | None = None
    reason_code: str | None = None
    duration_ms: float | None = None
    input_bytes: int | None = None
    output_bytes: int | None = None


class EventJournal:
    """Retain a bounded cursor view; sink delivery cannot change run decisions."""

    def __init__(
        self, *, sink: EventSink | None = None, max_count: int = 1_000,
        max_bytes: int = 1_048_576,
    ) -> None:
        if max_count < 1 or max_bytes < 512:
            raise ValueError("event limits must allow at least one event")
        self.sink = sink or NoopEventSink()
        self.max_count = max_count
        self.max_bytes = max_bytes
        self._events: dict[str, list[RunEvent]] = {}
        self._pending: deque[RunEvent] = deque()
        self._worker: asyncio.Task[None] | None = None
        self.sink_failures = 0
        self.dropped_delivery = 0
        self.record_failures = 0
        self._sink_failure_markers: dict[str, RunEvent] = {}

    def events(self, run_id: str, *, after: int = 0) -> tuple[RunEvent, ...]:
        return tuple(event for event in self._events.get(safe_identifier(run_id) or "redacted_run", ()) if event.sequence > after)

    def sink_failure(self, run_id: str) -> RunEvent | None:
        """One metadata-only diagnostic per run, separate from the CAS event cursor."""
        return self._sink_failure_markers.get(safe_identifier(run_id) or "redacted_run")

    def forget(self, run_id: str) -> None:
        """Let an owning host evict a run alongside its snapshot."""
        key = safe_identifier(run_id) or "redacted_run"
        self._events.pop(key, None)
        self._sink_failure_markers.pop(key, None)

    def record(self, event: RunEvent) -> None:
        retained = self._events.setdefault(event.run_id, [])
        if retained and event.sequence <= retained[-1].sequence:
            raise ValueError("event sequence must advance")
        retained.append(event)
        self._bound(event.run_id)
        if len(self._pending) < self.max_count:
            self._pending.append(event)
            if self._worker is None or self._worker.done():
                self._worker = asyncio.get_running_loop().create_task(self._deliver())
        else:
            self.dropped_delivery += 1

    def _bound(self, run_id: str) -> None:
        events = self._events[run_id]
        first = events[0]
        latest = events[-1]
        terminal = next((item for item in reversed(events) if item.kind == "terminal"), None)
        if len(events) <= self.max_count and sum(item.byte_size for item in events) <= self.max_bytes:
            return
        keep: list[RunEvent] = [latest]
        if terminal is not None and terminal != latest:
            keep.insert(0, terminal)
        if first not in keep and self.max_count > len(keep):
            keep.insert(0, first)
        old_marker = next((item for item in events if item.kind == "truncated"), None)
        marker_sequence = old_marker.sequence if old_marker else first.sequence + 1
        marker = replace(
            latest, sequence=marker_sequence,
            kind="truncated", call_id=None, tool_name=None, status=None,
            reason_code="event_limit", duration_ms=None, input_bytes=None, output_bytes=None,
        )
        if self.max_count >= len(keep) + 1 and marker.sequence not in {item.sequence for item in keep}:
            keep.insert(1 if keep[0] == first else 0, marker)
        for item in reversed(events[1:-1]):
            if item.kind == "truncated" or item in keep or len(keep) >= self.max_count:
                continue
            candidate = sorted(keep + [item], key=lambda value: value.sequence)
            if sum(value.byte_size for value in candidate) <= self.max_bytes:
                keep = candidate
        keep = sorted(keep, key=lambda value: value.sequence)
        while len(keep) > 1 and sum(item.byte_size for item in keep) > self.max_bytes:
            # Prefer first and terminal/latest over the truncation marker when tight.
            removable = next((item for item in keep if item.kind == "truncated"), None)
            if removable is None:
                removable = next((item for item in keep if item not in {first, latest, terminal}), None)
            if removable is None:
                removable = next((item for item in keep if item not in {latest, terminal}), None)
            if removable is None:
                removable = next((item for item in keep if item != latest), None)
            if removable is None:
                break
            keep.remove(removable)
        self._events[run_id] = keep

    async def _deliver(self) -> None:
        while self._pending:
            event = self._pending.popleft()
            try:
                await self.sink.emit(event)
            except Exception:
                self.sink_failures += 1
                self._sink_failure_markers.setdefault(event.run_id, replace(
                    event, kind="sink_failed", call_id=None, tool_name=None,
                    status=None, reason_code="sink_error", duration_ms=None,
                    input_bytes=None, output_bytes=None,
                ))

    async def flush(self) -> None:
        if self._worker is not None:
            await self._worker
