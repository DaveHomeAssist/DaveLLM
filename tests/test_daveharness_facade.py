"""H6 bounded store and independent public lifecycle facade."""

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from daveharness import (
    ApprovalDecision, Harness, InMemoryRunStore, KNOWN_PERMISSIONS,
    RunBudget, RunRequest, RunSnapshot, ToolDefinition, ToolRegistry,
    transition_run,
)


NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def snapshot(run_id):
    return RunSnapshot.create(run_id=run_id, transcript=[{"role": "user", "content": "go"}], created_at=NOW.isoformat())


def native_call(call_id, name):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def approval(pending, run_id):
    return ApprovalDecision(
        run_id=run_id, call_id=pending.call_id, digest=pending.digest,
        definition_fingerprint=pending.definition_fingerprint, permission=pending.permission,
        nonce=pending.nonce, decision_id="decision_1", decision="approve",
        issued_at=pending.created_at, expires_at=pending.expires_at,
    )


def test_request_owns_messages_and_rejects_unsupported_input():
    messages = [{"role": "user", "content": "original"}]
    request = RunRequest.create(messages, model_id="local:latest")
    messages[0]["content"] = "mutated"
    assert request.messages[0]["content"] == "original"
    copy_of_messages = request.messages
    copy_of_messages[0]["content"] = "mutated again"
    assert request.messages[0]["content"] == "original"
    with pytest.raises(ValueError, match="unsupported"):
        RunRequest.create([{"role": "developer", "content": "bad"}])
    with pytest.raises(ValueError, match="finite"):
        RunRequest.create([{"role": "user", "content": float("nan")}])
    with pytest.raises(ValueError, match="permissions"):
        RunRequest.create([{"role": "user", "content": "go"}], allowed_permissions=("unknown",))
    with pytest.raises(ValueError, match="model_id"):
        RunRequest.create([{"role": "user", "content": "go"}], model_id="https://node.example")


def test_store_cas_round_trip_conflict_and_copy_isolation():
    store = InMemoryRunStore(clock=lambda: NOW)
    created = snapshot("run_1")
    assert store.create(created)
    assert not store.create(created)
    loaded = store.load("run_1")
    loaded.transcript[0]["content"] = "edited"
    assert store.load("run_1").transcript[0]["content"] == "go"
    running = transition_run(created, "running", updated_at=NOW.isoformat())
    assert not store.compare_and_swap("run_1", 2, running)
    assert not store.compare_and_swap("other", 1, running)
    assert store.compare_and_swap("run_1", 1, running)
    assert not store.compare_and_swap("run_1", 1, running)
    assert store.load("run_1").optimistic_version == 2


def test_store_concurrent_cas_has_one_winner():
    store = InMemoryRunStore(clock=lambda: NOW)
    created = snapshot("run_1")
    assert store.create(created)
    running = transition_run(created, "running", updated_at=NOW.isoformat())
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(lambda _index: store.compare_and_swap("run_1", 1, running), range(2)))
    assert sorted(winners) == [False, True]
    assert store.load("run_1").optimistic_version == 2


def test_store_expiry_eviction_and_byte_ceiling():
    now = [NOW]
    removed = []
    first = snapshot("first")
    sample_bytes = len(json.dumps(first.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    store = InMemoryRunStore(
        max_runs=2, max_bytes=sample_bytes * 2 + 20, ttl_seconds=10,
        clock=lambda: now[0], on_remove=removed.append,
    )
    assert store.create(first)
    assert store.create(snapshot("second"))
    assert store.run_count == 2 and store.stored_bytes <= store.max_bytes
    assert store.create(snapshot("third"))
    assert store.load("first") is None and removed == ["first"]
    assert store.run_count == 2 and store.stored_bytes <= store.max_bytes
    now[0] += timedelta(seconds=10)
    assert store.load("third") is None
    assert store.missing_reason("third") == "run_expired"
    assert store.run_count == 0 and store.stored_bytes == 0
    now[0] -= timedelta(seconds=20)
    assert store.missing_reason("third") == "run_expired"
    with pytest.raises(ValueError, match="byte ceiling"):
        InMemoryRunStore(max_bytes=10, clock=lambda: NOW).create(first)
    growth_limited = InMemoryRunStore(max_bytes=sample_bytes + 50, clock=lambda: NOW)
    assert growth_limited.create(first)
    grown = transition_run(
        first, "running", updated_at=NOW.isoformat(),
        transcript=[{"role": "user", "content": "x" * 1_000}],
    )
    assert not growth_limited.compare_and_swap("first", 1, grown)
    assert growth_limited.load("first").optimistic_version == 1
    assert growth_limited.stored_bytes <= growth_limited.max_bytes
    for kwargs in ({"max_runs": 0}, {"max_bytes": 0}, {"ttl_seconds": float("nan")}):
        with pytest.raises(ValueError):
            InMemoryRunStore(**kwargs)


@pytest.mark.asyncio
async def test_facade_lifecycle_approval_replay_and_event_cursor():
    effects = []
    model_calls = []
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="write", description="Write", parameters={"type": "object"},
        handler=lambda args: effects.append(copy.deepcopy(args)) or "ok",
        permission="write", approval_required=True, cancellation="bounded",
    ))

    async def model(messages, _schemas):
        model_calls.append(1)
        return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]} if len(messages) == 1 else {"role": "assistant", "content": "done"}

    harness = Harness(registry=registry, invoke_model=model, clock=lambda: NOW)
    request = RunRequest.create(
        [{"role": "user", "content": "go"}], run_id="run_1",
        allowed_permissions=tuple(sorted(KNOWN_PERMISSIONS)),
    )
    paused = await harness.start(request)
    assert paused.status == "approval_required"
    assert harness.snapshot("run_1").snapshot.pending_call is not None
    assert model_calls == [1] and not effects
    pending = paused.snapshot.pending_call
    decided = await harness.decide(approval(pending, "run_1"))
    assert decided.status == "completed"
    assert effects == [{}] and model_calls == [1, 1]
    assert decided.snapshot.event_cursor == len(harness.events("run_1"))
    assert [item.kind for item in harness.events("run_1")].count("terminal") == 1
    assert harness.events("run_1", after=paused.snapshot.event_cursor)[0].kind == "approval_decision"
    assert (await harness.decide(approval(pending, "run_1"))).reason_code == "approval_not_pending"
    assert (await harness.resume("run_1")).reason_code == "run_not_running"
    assert (await harness.cancel("run_1")).reason_code == "run_not_running"


@pytest.mark.asyncio
async def test_two_facades_run_concurrently_without_state_or_tool_leakage():
    effects_a = []
    effects_b = []

    def make_harness(effects, answer):
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="read", description="Read", parameters={"type": "object"},
            handler=lambda _args: effects.append(answer) or answer,
            permission="read",
        ))

        async def model(messages, _schemas):
            return {"role": "assistant", "tool_calls": [native_call("call_1", "read")]} if len(messages) == 1 else {"role": "assistant", "content": answer}

        return Harness(registry=registry, invoke_model=model, clock=lambda: NOW)

    first = make_harness(effects_a, "A")
    second = make_harness(effects_b, "B")
    results = await asyncio.gather(
        first.start(RunRequest.create([{"role": "user", "content": "go"}], run_id="same_id", allowed_permissions=("read",))),
        second.start(RunRequest.create([{"role": "user", "content": "go"}], run_id="same_id", allowed_permissions=("read",))),
    )
    assert [result.status for result in results] == ["completed", "completed"]
    assert effects_a == ["A"] and effects_b == ["B"]
    assert results[0].snapshot.last_content == "A" and results[1].snapshot.last_content == "B"
    assert first.events("same_id") != () and second.events("same_id") != ()
    assert first.store is not second.store
    assert first.registry is not second.registry


@pytest.mark.asyncio
async def test_approval_is_instance_owned_even_with_identical_run_and_call_ids():
    effects = []

    def make_harness(label):
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            name="write", description="Write", parameters={"type": "object"},
            handler=lambda _args: effects.append(label) or "ok",
            permission="write", approval_required=True, cancellation="bounded",
        ))

        async def model(messages, _schemas):
            return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]} if len(messages) == 1 else {"role": "assistant", "content": label}

        return Harness(registry=registry, invoke_model=model, clock=lambda: NOW)

    first = make_harness("A")
    second = make_harness("B")
    request = RunRequest.create([{"role": "user", "content": "go"}], run_id="same_id", allowed_permissions=("write",))
    paused_a, paused_b = await asyncio.gather(first.start(request), second.start(request))
    assert paused_a.status == paused_b.status == "approval_required"
    assert effects == []
    assert (await first.decide(approval(paused_a.snapshot.pending_call, "same_id"))).status == "completed"
    assert effects == ["A"]
    assert second.snapshot("same_id").status == "approval_required"
    assert len(first.events("same_id")) > len(second.events("same_id"))
    assert (await second.decide(approval(paused_b.snapshot.pending_call, "same_id"))).status == "completed"
    assert effects == ["A", "B"]


@pytest.mark.asyncio
async def test_facade_cancel_active_model_and_default_store_cleanup():
    entered = asyncio.Event()

    async def model(_messages, _schemas):
        entered.set()
        await asyncio.Event().wait()

    harness = Harness(registry=ToolRegistry(), invoke_model=model, clock=lambda: NOW)
    task = asyncio.create_task(harness.start(RunRequest.create([{"role": "user", "content": "go"}], run_id="run_1")))
    await entered.wait()
    cancelled = await harness.cancel("run_1")
    await task
    assert cancelled.status == "cancelled"
    assert [item.kind for item in harness.events("run_1")].count("terminal") == 1

    # Default store eviction removes its journal alongside the run.
    harness.store.max_runs = 1
    async def complete(_messages, _schemas):
        return {"role": "assistant", "content": "done"}
    harness.invoke_model = complete
    assert (await harness.start(RunRequest.create([{"role": "user", "content": "go"}], run_id="run_2"))).status == "completed"
    assert harness.snapshot("run_1").snapshot is None
    assert not harness.events("run_1")


@pytest.mark.asyncio
async def test_injected_memory_store_eviction_forgets_its_facade_journal():
    async def model(_messages, _schemas):
        return {"role": "assistant", "content": "done"}

    injected = InMemoryRunStore(max_runs=1, clock=lambda: NOW)
    harness = Harness(registry=ToolRegistry(), invoke_model=model, store=injected, clock=lambda: NOW)
    for run_id in ("first", "second"):
        assert (await harness.start(RunRequest.create([{"role": "user", "content": "go"}], run_id=run_id))).status == "completed"
    assert not harness.events("first")
    assert harness.events("second")
