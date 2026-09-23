"""Seeded schedules at reservation, approval, completion, and delivery boundaries."""

import asyncio
import random
import threading
from dataclasses import replace

import pytest

from daveharness import ApprovalDecision, Harness, InMemoryRunStore, RunRequest, ToolDefinition, ToolRegistry


def call(name):
    return {"tool_calls": [{"id": "call_1", "function": {"name": name, "arguments": "{}"}}]}


def decision(result):
    pending = result.snapshot.pending_call
    return ApprovalDecision(
        run_id=result.snapshot.run_id, call_id=pending.call_id, digest=pending.digest,
        definition_fingerprint=pending.definition_fingerprint, permission=pending.permission,
        nonce=pending.nonce, decision_id="decision_1", decision="approve",
        issued_at=pending.created_at, expires_at=pending.expires_at,
    )


class FailingSink:
    async def emit(self, _event):
        raise RuntimeError("SECRET_SINK_CANARY")


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(8))
async def test_duplicate_decisions_with_sink_failure_have_one_effect_and_terminal(seed):
    effects = []
    registry = ToolRegistry()
    registry.register(ToolDefinition("write", "", {}, lambda _: effects.append(1) or "ok",
                                     permission="write", approval_required=True, cancellation="bounded"))
    async def model(messages, _schemas):
        return call("write") if len(messages) == 1 else {"content": "done"}
    harness = Harness(registry=registry, invoke_model=model, event_sink=FailingSink())
    paused = await harness.start(RunRequest.create([{"role": "user", "content": "go"}], allowed_permissions=("write",)))
    exact = decision(paused)
    delays = list(range(4))
    random.Random(seed).shuffle(delays)
    async def decide(delay):
        for _ in range(delay):
            await asyncio.sleep(0)
        return await harness.decide(exact)
    await asyncio.gather(*(decide(delay) for delay in delays))
    await harness._journals[exact.run_id].flush()
    assert effects == [1]
    assert harness.snapshot(exact.run_id).status == "completed"
    assert sum(item.kind == "terminal" for item in harness.events(exact.run_id)) == 1
    assert harness._journals[exact.run_id].sink_failures > 0


@pytest.mark.asyncio
async def test_revocation_and_cas_conflict_during_approval_never_execute_stale_call():
    class ConflictStore(InMemoryRunStore):
        fail_next = False
        def compare_and_swap(self, *args):
            if self.fail_next:
                self.fail_next = False
                return False
            return super().compare_and_swap(*args)

    for revoke in (False, True):
        effects = []
        store = ConflictStore()
        registry = ToolRegistry()
        registry.register(ToolDefinition("write", "", {}, lambda _: effects.append(1) or "ok",
                                         permission="write", approval_required=True, cancellation="bounded"))
        async def model(messages, _schemas):
            return call("write") if len(messages) == 1 else {"content": "done"}
        harness = Harness(registry=registry, invoke_model=model, store=store)
        paused = await harness.start(RunRequest.create([{"role": "user", "content": "go"}], allowed_permissions=("write",)))
        exact = decision(paused)
        if revoke:
            registry.revoke("write")
        else:
            store.fail_next = True
        result = await harness.decide(exact)
        assert result.status == "run_conflict" and effects == []
        if not revoke:
            assert (await harness.decide(exact)).status == "completed"
            assert effects == [1]
        else:
            await harness.decide(replace(exact, decision="reject"))
        assert sum(item.kind == "terminal" for item in harness.events(exact.run_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(8))
async def test_cancel_at_completion_keeps_one_terminal_and_no_late_content(seed):
    entered, release = asyncio.Event(), asyncio.Event()
    async def model(*_):
        entered.set()
        await release.wait()
        return {"content": "done"}
    harness = Harness(registry=ToolRegistry(), invoke_model=model)
    work = asyncio.create_task(harness.start(RunRequest.create([{"role": "user", "content": "go"}], run_id="race")))
    await entered.wait()
    if random.Random(seed).choice([True, False]):
        release.set()
        await asyncio.sleep(0)
    cancelled = asyncio.create_task(harness.cancel("race"))
    release.set()
    await asyncio.gather(work, cancelled)
    final = harness.snapshot("race").snapshot
    assert final.status in {"completed", "cancelled"}
    assert sum(item.kind == "terminal" for item in harness.events("race")) == 1
    await harness.cancel("race")
    assert harness.snapshot("race").snapshot == final


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["model", "tool"])
async def test_late_sync_result_cannot_reopen_terminal(operation):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    effects = []
    def blocked(*_):
        entered.set()
        release.wait(2)
        finished.set()
        return {"content": "late"} if operation == "model" else "late"
    registry = ToolRegistry()
    registry.register(ToolDefinition("read", "", {}, blocked, handler_version="late-v1"))
    async def model(messages, _schemas):
        effects.append("model")
        return call("read") if len(messages) == 1 else {"content": "done"}
    harness = Harness(registry=registry, invoke_model=blocked if operation == "model" else model)
    work = asyncio.create_task(harness.start(RunRequest.create([{"role": "user", "content": "go"}], run_id="late", allowed_permissions=("read",))))
    assert await asyncio.to_thread(entered.wait, 1)
    try:
        await harness.cancel("late")
        terminal = harness.snapshot("late").snapshot
    finally:
        release.set()
    await work
    assert await asyncio.to_thread(finished.wait, 1)
    assert terminal.status == "cancellation_failed"
    assert harness.snapshot("late").snapshot == terminal
    assert sum(item.kind == "terminal" for item in harness.events("late")) == 1
    assert effects == ([] if operation == "model" else ["model"])
