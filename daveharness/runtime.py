"""Injected clocks, cancellation, and stoppable execution contracts."""

from __future__ import annotations

import asyncio
import inspect
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from .registry import ToolDefinition


MonotonicClock = Callable[[], float]
EventCallback = Callable[[str], Awaitable[None]]


def monotonic_now() -> float:
    return time.monotonic()


def remaining_wall_seconds(deadline_at: str | None, now: datetime) -> float | None:
    """Translate a durable UTC deadline into one process's monotonic timebase."""
    if deadline_at is None:
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("wall clock must be timezone-aware")
    deadline = datetime.fromisoformat(deadline_at)
    return max(0.0, (deadline - now.astimezone(timezone.utc)).total_seconds())


def absolute_deadline(
    monotonic_clock: MonotonicClock, duration_seconds: float | None,
    run_remaining_seconds: float | None,
) -> float | None:
    """Compute one absolute operation deadline from the injected monotonic clock."""
    if duration_seconds is not None and (not math.isfinite(duration_seconds) or duration_seconds <= 0):
        raise ValueError("operation timeout must be finite and positive")
    now = monotonic_clock()
    limits = [value for value in (duration_seconds, run_remaining_seconds) if value is not None]
    return now + min(limits) if limits else None


class CancellationToken:
    """Thread-observable request signal; the runner owns stop acknowledgment."""

    def __init__(self) -> None:
        self._flag = threading.Event()
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_requested(self) -> bool:
        return self._flag.is_set()

    def request_cancel(self) -> None:
        self._flag.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.is_requested:
            return
        await self._event.wait()


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    run_id: str
    call_id: str
    run_deadline: float | None
    model_deadline: float | None
    tool_deadline: float | None
    output_budget_bytes: int | None
    cancellation: CancellationToken
    monotonic_clock: MonotonicClock = monotonic_now
    event: EventCallback | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.call_id:
            raise ValueError("execution IDs must be nonempty")
        for name in ("run_deadline", "model_deadline", "tool_deadline"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.output_budget_bytes is not None and self.output_budget_bytes < 1:
            raise ValueError("output budget must be positive")

    def remaining(self, kind: str) -> float | None:
        if kind not in {"run", "model", "tool"}:
            raise ValueError("unknown deadline kind")
        deadline = getattr(self, f"{kind}_deadline")
        return None if deadline is None else max(0.0, deadline - self.monotonic_clock())


class CancellableToolRunner(Protocol):
    async def invoke(
        self, definition: ToolDefinition, arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> Any: ...

    async def stop(self, context: ExecutionContext) -> bool: ...


class CooperativeToolRunner:
    """Opt-in context handlers; a stop is acknowledged only when work finishes."""

    def __init__(self, *, stop_grace_seconds: float = 1.0) -> None:
        if not math.isfinite(stop_grace_seconds) or stop_grace_seconds <= 0:
            raise ValueError("stop grace must be finite and positive")
        self.stop_grace_seconds = stop_grace_seconds
        self._active: dict[tuple[str, str], asyncio.Task[Any]] = {}

    async def invoke(
        self, definition: ToolDefinition, arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> Any:
        if not definition.context_handler:
            raise ValueError("tool has not opted in to context execution")

        async def execute() -> Any:
            if inspect.iscoroutinefunction(definition.handler):
                return await definition.handler(arguments, context)
            return await asyncio.to_thread(definition.handler, arguments, context)

        key = (context.run_id, context.call_id)
        if key in self._active:
            raise ValueError("tool call already running")
        task = asyncio.create_task(execute())
        self._active[key] = task

        def cleanup(completed: asyncio.Task[Any]) -> None:
            if self._active.get(key) is completed:
                self._active.pop(key, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(cleanup)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._active.pop(key, None)

    async def stop(self, context: ExecutionContext) -> bool:
        context.cancellation.request_cancel()
        key = (context.run_id, context.call_id)
        task = self._active.get(key)
        if task is None:
            return False
        done, _pending = await asyncio.wait({task}, timeout=self.stop_grace_seconds)
        if not done:
            return False
        self._active.pop(key, None)
        return True


class OperationController:
    """Per-run active work handle owned by a future Harness instance."""

    def __init__(self, *, stop_grace_seconds: float = 1.0) -> None:
        if not math.isfinite(stop_grace_seconds) or stop_grace_seconds <= 0:
            raise ValueError("stop grace must be finite and positive")
        self.token = CancellationToken()
        self.stop_grace_seconds = stop_grace_seconds
        self.model_task: asyncio.Task[Any] | None = None
        self.model_cancellable = False
        self.tool_context: ExecutionContext | None = None
        self.tool_runner: CancellableToolRunner | None = None
        self.active_legacy_tool = False
        self._run_id: str | None = None
        self._run_deadline: float | None = None

    def remaining_run(
        self, run_id: str, wall_remaining: float | None,
        monotonic_clock: MonotonicClock,
    ) -> float | None:
        """Pin one monotonic deadline; UTC may tighten it but never extend it."""
        if self._run_id is None:
            self._run_id = run_id
        elif self._run_id != run_id:
            raise ValueError("operation controller belongs to another run")
        if wall_remaining is not None:
            candidate = monotonic_clock() + wall_remaining
            self._run_deadline = (
                candidate if self._run_deadline is None
                else min(self._run_deadline, candidate)
            )
        if self._run_deadline is None:
            return None
        return max(0.0, self._run_deadline - monotonic_clock())

    async def stop(self) -> bool:
        self.token.request_cancel()
        if self.tool_context is not None:
            return await self.stop_tool()
        if self.active_legacy_tool:
            return False
        if self.model_task is not None:
            if not self.model_cancellable:
                return False
            self.model_task.cancel()
            done, _pending = await asyncio.wait(
                {self.model_task}, timeout=self.stop_grace_seconds,
            )
            return bool(done)
        return True

    async def stop_tool(self) -> bool:
        """Stop only the current tool; a timeout does not cancel the whole run."""
        if self.tool_context is not None:
            self.tool_context.cancellation.request_cancel()
            if self.tool_runner is None:
                return False
            stop_task = asyncio.create_task(self.tool_runner.stop(self.tool_context))
            done, _pending = await asyncio.wait(
                {stop_task}, timeout=self.stop_grace_seconds,
            )
            if not done:
                stop_task.cancel()
                return False
            try:
                return bool(stop_task.result())
            except asyncio.CancelledError:
                return False
            except Exception:
                return False
        return False
