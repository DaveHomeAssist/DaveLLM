"""H4 deadlines, cooperative cancellation, and legacy timeout truthfulness."""

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from daveharness import RunBudget, ToolDefinition, ToolRegistry, run_tool
from daveharness.runtime import (
    CancellationToken, CooperativeToolRunner, ExecutionContext,
    OperationController, absolute_deadline, remaining_wall_seconds,
)
from daveharness.state import RunSnapshot, transition_run
from daveharness.state_engine import cancel_run, resume_run


NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.wall = NOW
        self.mono = 1000.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


class FakeCAS:
    def __init__(self, snapshot):
        self.value = RunSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
        self.lock = threading.Lock()

    def load(self, run_id):
        with self.lock:
            return RunSnapshot.from_dict(json.loads(json.dumps(self.value.to_dict()))) if run_id == self.value.run_id else None

    def compare_and_swap(self, run_id, expected_version, snapshot):
        with self.lock:
            if run_id != self.value.run_id or expected_version != self.value.optimistic_version:
                return False
            self.value = RunSnapshot.from_dict(json.loads(json.dumps(snapshot.to_dict())))
            return True


def state(*, budget=None):
    return RunSnapshot.create(
        run_id="run_1", transcript=[{"role": "user", "content": "go"}],
        created_at=NOW.isoformat(), budget=budget,
        allowed_permissions=("read", "write"),
    )


def call(name="read", call_id="call_1"):
    return {"role": "assistant", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }]}


def test_deadline_calculation_uses_absolute_monotonic_boundaries():
    clock = FakeClock()
    deadline = (NOW + timedelta(seconds=30)).isoformat()
    remaining = remaining_wall_seconds(deadline, clock.now())
    assert remaining == 30
    assert absolute_deadline(clock.monotonic, 120, remaining) == 1030
    assert absolute_deadline(clock.monotonic, 10, remaining) == 1010
    clock.advance(30)
    assert remaining_wall_seconds(deadline, clock.now()) == 0
    assert absolute_deadline(clock.monotonic, 120, 0) == 1030
    context = ExecutionContext(
        "run_1", "call_1", 1030, None, 1030, 100,
        CancellationToken(), clock.monotonic,
    )
    assert context.remaining("run") == 0
    bounded = RunSnapshot.create(
        run_id="run_1", transcript=[{"role": "user", "content": "go"}],
        created_at=NOW.isoformat(), budget=RunBudget(total_wall_seconds=30),
        deadline_at=(NOW + timedelta(seconds=300)).isoformat(),
    )
    assert bounded.deadline_at == deadline


@pytest.mark.asyncio
async def test_wall_clock_rollback_does_not_extend_one_active_run():
    clock = FakeClock()

    async def model(_messages, _schemas):
        clock.wall -= timedelta(seconds=100)
        clock.mono += 31
        return {"role": "assistant", "content": "late"}

    store = FakeCAS(state(budget=RunBudget(total_wall_seconds=30)))
    result = await resume_run(
        store, "run_1", ToolRegistry(), model,
        clock=clock.now, monotonic_clock=clock.monotonic,
    )
    assert result.status == "run_expired"
    assert store.load("run_1").last_content is None


@pytest.mark.asyncio
async def test_cancel_before_start_is_nonexecuting_and_idle_running_can_stop():
    store = FakeCAS(state())
    before = await cancel_run(store, "run_1", clock=lambda: NOW)
    assert before.status == "created" and before.reason_code == "run_not_running"
    assert store.load("run_1").steps == 0
    running = transition_run(store.load("run_1"), "running", updated_at=NOW.isoformat())
    assert store.compare_and_swap("run_1", 1, running)
    cancelled = await cancel_run(store, "run_1", clock=lambda: NOW)
    assert cancelled.status == "cancelled"
    assert store.load("run_1").status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_during_async_model_waits_for_stop_acknowledgment():
    clock = FakeClock()
    entered = asyncio.Event()
    controller = OperationController()

    async def model(_messages, _schemas):
        entered.set()
        await asyncio.Event().wait()

    store = FakeCAS(state())
    running = asyncio.create_task(resume_run(
        store, "run_1", ToolRegistry(), model, clock=clock.now,
        monotonic_clock=clock.monotonic, controller=controller,
    ))
    await entered.wait()
    cancelled = await cancel_run(store, "run_1", controller=controller, clock=clock.now)
    assert cancelled.status == "cancelled"
    await running
    assert store.load("run_1").status == "cancelled"
    assert store.load("run_1").model_inflight is False


@pytest.mark.asyncio
async def test_cooperative_tool_stops_before_cancelled_status():
    clock = FakeClock()
    entered = asyncio.Event()
    stopped = asyncio.Event()
    registry = ToolRegistry(async_handler_allowlist={"read"})

    async def handler(_args, context):
        entered.set()
        await context.cancellation.wait()
        stopped.set()
        return "stopped"

    registry.register(ToolDefinition(
        name="read", description="read", parameters={"type": "object"},
        handler=handler, async_handler=True, context_handler=True,
        cancellation="bounded", handler_version="cooperative-test-v1",
    ))

    async def model(messages, _schemas):
        return call() if len(messages) == 1 else {"role": "assistant", "content": "done"}

    store = FakeCAS(state())
    controller = OperationController()
    runner = CooperativeToolRunner(stop_grace_seconds=0.5)
    work = asyncio.create_task(resume_run(
        store, "run_1", registry, model, clock=clock.now,
        monotonic_clock=clock.monotonic, controller=controller, runner=runner,
    ))
    await entered.wait()
    outcome = await cancel_run(store, "run_1", controller=controller, clock=clock.now)
    assert stopped.is_set()
    assert outcome.status == "cancelled"
    await work
    assert store.load("run_1").status == "cancelled"


@pytest.mark.asyncio
async def test_uncancellable_legacy_tool_reports_failed_stop_then_late_result_is_ignored():
    clock = FakeClock()
    started = threading.Event()
    release = threading.Event()
    registry = ToolRegistry()

    def handler(_args):
        started.set()
        release.wait(timeout=1)
        return "late"

    registry.register(ToolDefinition(
        name="read", description="read", parameters={"type": "object"},
        handler=handler, timeout_seconds=0.5,
        handler_version="legacy-test-v1",
    ))

    async def model(messages, _schemas):
        return call() if len(messages) == 1 else {"role": "assistant", "content": "done"}

    store = FakeCAS(state())
    controller = OperationController()
    work = asyncio.create_task(resume_run(
        store, "run_1", registry, model, clock=clock.now,
        monotonic_clock=clock.monotonic, controller=controller,
    ))
    await asyncio.to_thread(started.wait, 1)
    try:
        result = await cancel_run(store, "run_1", controller=controller, clock=clock.now)
        assert result.status == "cancellation_failed"
        assert not release.is_set()
    finally:
        release.set()
    await work
    assert store.load("run_1").status == "cancellation_failed"


@pytest.mark.asyncio
async def test_legacy_thread_timeout_keeps_deadline_abandoned_metadata():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    registry = ToolRegistry()

    def handler(_args):
        started.set()
        release.wait(timeout=1)
        finished.set()
        return "ok"

    registry.register(ToolDefinition(
        name="read", description="read", parameters={"type": "object"},
        handler=handler, timeout_seconds=0.01, handler_version="timeout-test-v1",
    ))
    try:
        execution = await run_tool("read", {}, registry=registry)
        assert started.is_set()
        assert execution.status == "timeout"
        assert execution.termination == "deadline_abandoned"
        assert not finished.is_set()
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.asyncio
async def test_injected_process_runner_acknowledges_stop_before_cancelled():
    entered = asyncio.Event()
    acknowledged = asyncio.Event()
    finished = asyncio.Event()
    registry = ToolRegistry(async_handler_allowlist={"read"})

    async def handler(_args, _context):
        await acknowledged.wait()
        return "stopped"

    registry.register(ToolDefinition(
        name="read", description="read", parameters={"type": "object"},
        handler=handler, async_handler=True, context_handler=True,
        cancellation="bounded", handler_version="process-test-v1",
    ))

    class ProcessRunner:
        async def invoke(self, definition, args, context):
            entered.set()
            try:
                return await definition.handler(args, context)
            finally:
                finished.set()

        async def stop(self, _context):
            acknowledged.set()
            await finished.wait()
            return True

    async def model(messages, _schemas):
        return call() if len(messages) == 1 else {"role": "assistant", "content": "done"}

    store = FakeCAS(state())
    controller = OperationController()
    work = asyncio.create_task(resume_run(
        store, "run_1", registry, model, clock=lambda: NOW,
        controller=controller, runner=ProcessRunner(),
    ))
    await entered.wait()
    result = await cancel_run(store, "run_1", controller=controller, clock=lambda: NOW)
    assert acknowledged.is_set()
    assert finished.is_set()
    assert result.status == "cancelled"
    await work
    assert store.load("run_1").status == "cancelled"


@pytest.mark.asyncio
async def test_runner_refusal_marks_cancellation_failed_and_discards_late_result():
    entered = asyncio.Event()
    release = asyncio.Event()
    registry = ToolRegistry(async_handler_allowlist={"read"})

    async def handler(_args, _context):
        await release.wait()
        return "late"

    registry.register(ToolDefinition(
        name="read", description="read", parameters={"type": "object"},
        handler=handler, async_handler=True, context_handler=True,
        cancellation="bounded", handler_version="refusal-test-v1",
    ))

    class RefusingRunner:
        async def invoke(self, definition, args, context):
            entered.set()
            return await definition.handler(args, context)

        async def stop(self, _context):
            return False

    async def model(messages, _schemas):
        return call() if len(messages) == 1 else {"role": "assistant", "content": "done"}

    store = FakeCAS(state())
    controller = OperationController()
    work = asyncio.create_task(resume_run(
        store, "run_1", registry, model, clock=lambda: NOW,
        controller=controller, runner=RefusingRunner(),
    ))
    await entered.wait()
    result = await cancel_run(store, "run_1", controller=controller, clock=lambda: NOW)
    assert result.status == "cancellation_failed"
    terminal_version = store.load("run_1").optimistic_version
    release.set()
    await work
    assert store.load("run_1").optimistic_version == terminal_version
    assert (await cancel_run(store, "run_1", clock=lambda: NOW)).status == "cancellation_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_mode", ["refuse", "cancel"])
async def test_tool_timeout_without_stop_ack_blocks_following_call(stop_mode):
    release = asyncio.Event()
    effects = []
    registry = ToolRegistry(async_handler_allowlist={"slow"})

    async def slow_handler(_args, _context):
        await release.wait()
        effects.append("slow")
        return "late"

    registry.register(ToolDefinition(
        name="slow", description="slow", parameters={"type": "object"},
        handler=slow_handler, async_handler=True, context_handler=True,
        cancellation="bounded", timeout_seconds=0.01,
        handler_version="timeout-refusal-v1",
    ))
    registry.register(ToolDefinition(
        name="next", description="next", parameters={"type": "object"},
        handler=lambda _args: effects.append("next") or "ok",
    ))

    class RefusingProcessRunner:
        async def invoke(self, definition, args, context):
            return await asyncio.shield(definition.handler(args, context))

        async def stop(self, _context):
            if stop_mode == "cancel":
                raise asyncio.CancelledError
            return False

    async def model(_messages, _schemas):
        return {"role": "assistant", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "slow", "arguments": "{}"}},
            {"id": "call_2", "type": "function", "function": {"name": "next", "arguments": "{}"}},
        ]}

    store = FakeCAS(state())
    try:
        result = await resume_run(
            store, "run_1", registry, model, clock=lambda: NOW,
            runner=RefusingProcessRunner(),
        )
        assert result.status == "cancellation_failed"
        assert effects == []
        assert store.load("run_1").executed_call_ids == ("call_1",)
    finally:
        release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_acknowledged_timeout_uses_fresh_token_and_final_result_log(caplog):
    registry = ToolRegistry(async_handler_allowlist={"slow", "next"})
    token_states = []

    async def slow_handler(_args, context):
        await context.cancellation.wait()
        return "stopped"

    async def next_handler(_args, context):
        token_states.append(context.cancellation.is_requested)
        return "ok"

    for name, handler, timeout in (
        ("slow", slow_handler, 0.01),
        ("next", next_handler, 1.0),
    ):
        registry.register(ToolDefinition(
            name=name, description=name, parameters={"type": "object"},
            handler=handler, async_handler=True, context_handler=True,
            cancellation="bounded", timeout_seconds=timeout,
            handler_version=f"{name}-ack-test-v1",
        ))

    async def model(messages, _schemas):
        if len(messages) == 1:
            return {"role": "assistant", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "slow", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "next", "arguments": "{}"}},
            ]}
        return {"role": "assistant", "content": "done"}

    store = FakeCAS(state())
    with caplog.at_level("INFO", logger="dave_llm.tools"):
        result = await resume_run(
            store, "run_1", registry, model, clock=lambda: NOW,
            runner=CooperativeToolRunner(stop_grace_seconds=0.5),
        )
    assert result.status == "completed"
    assert token_states == [False]
    tool_messages = [json.loads(item["content"]) for item in result.snapshot.transcript if item["role"] == "tool"]
    assert [(item["status"], item["termination"]) for item in tool_messages] == [
        ("timeout", "cancelled"), ("success", "completed"),
    ]
    results = [json.loads(record.message) for record in caplog.records if '"event": "tool_result"' in record.message]
    assert [(item["status"], item["termination"]) for item in results] == [
        ("timeout", "cancelled"), ("success", "completed"),
    ]


@pytest.mark.asyncio
async def test_completion_cancel_race_keeps_one_terminal_state():
    for iteration in range(12):
        entered = asyncio.Event()
        release = asyncio.Event()
        controller = OperationController()

        async def model(_messages, _schemas):
            entered.set()
            await release.wait()
            return {"role": "assistant", "content": "done"}

        store = FakeCAS(state())
        work = asyncio.create_task(resume_run(
            store, "run_1", ToolRegistry(), model, clock=lambda: NOW,
            controller=controller,
        ))
        await entered.wait()
        if iteration % 2:
            release.set()
            await asyncio.sleep(0)
            cancel = asyncio.create_task(cancel_run(
                store, "run_1", controller=controller, clock=lambda: NOW,
            ))
        else:
            cancel = asyncio.create_task(cancel_run(
                store, "run_1", controller=controller, clock=lambda: NOW,
            ))
            await asyncio.sleep(0)
            release.set()
        await asyncio.gather(work, cancel)
        final = store.load("run_1")
        assert final.status in {"completed", "cancelled", "cancellation_failed"}
        version = final.optimistic_version
        await cancel_run(store, "run_1", controller=controller, clock=lambda: NOW)
        assert store.load("run_1").optimistic_version == version


@pytest.mark.asyncio
async def test_total_deadline_does_not_reset_across_successful_model_steps():
    clock = FakeClock()
    calls = []
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="read", description="read", parameters={"type": "object"},
        handler=lambda _args: "ok",
    ))

    async def model(_messages, _schemas):
        calls.append(True)
        clock.advance(160)
        return call(call_id=f"call_{len(calls)}")

    store = FakeCAS(state(budget=RunBudget(model_steps=8, total_wall_seconds=300)))
    result = await resume_run(
        store, "run_1", registry, model, clock=clock.now,
        monotonic_clock=clock.monotonic,
    )
    assert result.status == "run_expired"
    assert len(calls) == 2
    assert store.load("run_1").deadline_at == (NOW + timedelta(seconds=300)).isoformat()
