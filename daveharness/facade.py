"""Instance-owned DaveHarness lifecycle over injected host dependencies."""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, cast

from .limits import PayloadLimits, bounded_loads, text_bytes, validate_payload
from .budgets import RunBudget
from .engine import DEFAULT_MODEL_TIMEOUT_SECONDS, ModelInvoker
from .events import EventJournal, EventSink, RunEvent
from .policy import KNOWN_PERMISSIONS, ToolPolicy
from .registry import ToolRegistry
from .runtime import CancellableToolRunner, MonotonicClock, OperationController, monotonic_now
from .state import ApprovalDecision, RunSnapshot, TERMINAL_RUN_STATUSES
from .state_engine import RunCommandResult, cancel_run, decide_run, resume_run
from .store import InMemoryRunStore, RunStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Defensively owned generic input; the host owns project/BRAIN context."""

    messages_json: str
    run_id: str | None = None
    model_id: str | None = None
    budget: RunBudget = field(default_factory=RunBudget)
    deadline_at: str | None = None
    allowed_permissions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.run_id is not None and (not isinstance(self.run_id, str) or not self.run_id.strip()):
            raise ValueError("run_id must be nonempty")
        if self.model_id is not None and (
            not isinstance(self.model_id, str) or not self.model_id.strip()
            or len(self.model_id) > 128 or "://" in self.model_id
        ):
            raise ValueError("model_id must be a local model label")
        if not isinstance(self.budget, RunBudget):
            raise ValueError("budget must be a RunBudget")
        if not isinstance(self.allowed_permissions, tuple) or any(
            permission not in KNOWN_PERMISSIONS for permission in self.allowed_permissions
        ) or len(set(self.allowed_permissions)) != len(self.allowed_permissions):
            raise ValueError("permissions must be known and unique")
        if self.deadline_at is not None:
            try:
                deadline = datetime.fromisoformat(self.deadline_at)
            except (TypeError, ValueError) as exc:
                raise ValueError("deadline_at must be an ISO timestamp") from exc
            if deadline.tzinfo is None or deadline.utcoffset() is None:
                raise ValueError("deadline_at must include a timezone")
        try:
            if self.budget.transcript_bytes is not None:
                text_bytes(self.messages_json, self.budget.transcript_bytes)
            messages = bounded_loads(self.messages_json)
            canonical = json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("messages must be finite JSON") from exc
        if canonical != self.messages_json or not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a nonempty canonical JSON array")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}:
                raise ValueError("unsupported message shape")
            if "content" in message and message["content"] is not None and not isinstance(message["content"], str):
                raise ValueError("message content must be text or null")

    @classmethod
    def create(
        cls, messages: list[dict[str, Any]], *, run_id: str | None = None,
        model_id: str | None = None, budget: RunBudget | None = None,
        deadline_at: str | None = None, allowed_permissions: tuple[str, ...] = (),
    ) -> RunRequest:
        try:
            validate_payload(messages, PayloadLimits(max_bytes=(budget or RunBudget()).transcript_bytes or 16_777_216))
            encoded = json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValueError("messages must be finite JSON") from exc
        return cls(encoded, run_id, model_id, budget or RunBudget(), deadline_at, allowed_permissions)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], bounded_loads(self.messages_json))


class Harness:
    """A host-owned run engine with no DaveLLM persistence or global tools."""

    def __init__(
        self, *, registry: ToolRegistry, invoke_model: ModelInvoker,
        policy: ToolPolicy | None = None, runner: CancellableToolRunner | None = None,
        store: RunStore | None = None, clock: Callable[[], datetime] = _utc_now,
        monotonic_clock: MonotonicClock = monotonic_now,
        event_sink: EventSink | None = None,
        model_timeout_seconds: float = DEFAULT_MODEL_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(model_timeout_seconds, bool) or not isinstance(model_timeout_seconds, (int, float)) or not math.isfinite(model_timeout_seconds) or model_timeout_seconds <= 0:
            raise ValueError("model_timeout_seconds must be finite and positive")
        self.registry = registry
        self.invoke_model = invoke_model
        self.policy = policy or ToolPolicy()
        self.runner = runner
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self.event_sink = event_sink
        self.model_timeout_seconds = model_timeout_seconds
        self._journals: dict[str, EventJournal] = {}
        self._controllers: dict[str, OperationController] = {}
        self.store = store if store is not None else InMemoryRunStore(clock=clock)
        if isinstance(self.store, InMemoryRunStore):
            self.store.add_remove_listener(self._forget)

    def _forget(self, run_id: str) -> None:
        journal = self._journals.pop(run_id, None)
        if journal is not None:
            journal.forget(run_id)
        self._controllers.pop(run_id, None)

    def _journal(self, run_id: str, budget: RunBudget | None = None) -> EventJournal:
        journal = self._journals.get(run_id)
        if journal is None:
            limit = budget.event_count if budget is not None else None
            journal = EventJournal(sink=self.event_sink, max_count=limit or 1_000)
            self._journals[run_id] = journal
        return journal

    def _controller(self, run_id: str) -> tuple[OperationController, bool]:
        existing = self._controllers.get(run_id)
        if existing is not None:
            return existing, False
        controller = OperationController()
        self._controllers[run_id] = controller
        return controller, True

    def _release(self, run_id: str, controller: OperationController, owner: bool) -> None:
        if owner and self._controllers.get(run_id) is controller:
            self._controllers.pop(run_id, None)

    async def start(self, request: RunRequest) -> RunCommandResult:
        run_id = request.run_id or uuid.uuid4().hex
        current_time = self.clock()
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("harness clock must be timezone-aware")
        created_at = current_time.astimezone(timezone.utc).isoformat()
        snapshot = RunSnapshot.create(
            run_id=run_id, transcript=request.messages, created_at=created_at,
            budget=request.budget, deadline_at=request.deadline_at,
            allowed_permissions=request.allowed_permissions,
        )
        try:
            created = self.store.create(snapshot)
        except ValueError as exc:
            if str(exc) != "run snapshot exceeds store byte ceiling":
                raise
            return RunCommandResult("budget_exceeded", None, "run_store_limit")
        if not created:
            return RunCommandResult("run_conflict", self.store.load(run_id), "run_exists")
        return await self.resume(run_id)

    def snapshot(self, run_id: str) -> RunCommandResult:
        value = self.store.load(run_id)
        if value is not None:
            return RunCommandResult(value.status, value, value.status)
        reason = self.store.missing_reason(run_id) if isinstance(self.store, InMemoryRunStore) else "run_not_found"
        return RunCommandResult("run_expired", None, reason)

    def events(self, run_id: str, *, after: int = 0) -> tuple[RunEvent, ...]:
        journal = self._journals.get(run_id)
        return journal.events(run_id, after=after) if journal is not None else ()

    async def resume(self, run_id: str) -> RunCommandResult:
        value = self.store.load(run_id)
        if value is None:
            return self.snapshot(run_id)
        controller, owner = self._controller(run_id)
        try:
            return await resume_run(
                self.store, run_id, self.registry, self.invoke_model,
                policy=self.policy, clock=self.clock,
                model_timeout_seconds=self.model_timeout_seconds,
                monotonic_clock=self.monotonic_clock, controller=controller,
                runner=self.runner, events=self._journal(run_id, value.budget),
            )
        finally:
            self._release(run_id, controller, owner)

    async def decide(self, decision: ApprovalDecision) -> RunCommandResult:
        value = self.store.load(decision.run_id)
        if value is None:
            return self.snapshot(decision.run_id)
        controller, owner = self._controller(decision.run_id)
        try:
            return await decide_run(
                self.store, decision, self.registry, self.invoke_model,
                policy=self.policy, clock=self.clock,
                model_timeout_seconds=self.model_timeout_seconds,
                monotonic_clock=self.monotonic_clock, controller=controller,
                runner=self.runner, events=self._journal(decision.run_id, value.budget),
            )
        finally:
            self._release(decision.run_id, controller, owner)

    async def cancel(self, run_id: str) -> RunCommandResult:
        value = self.store.load(run_id)
        if value is None:
            return self.snapshot(run_id)
        result = await cancel_run(
            self.store, run_id, controller=self._controllers.get(run_id),
            clock=self.clock, events=self._journal(run_id, value.budget),
        )
        if result.status in TERMINAL_RUN_STATUSES:
            self._controllers.pop(run_id, None)
        return result
