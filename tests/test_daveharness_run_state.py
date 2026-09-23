"""H3 serializable state, exact decisions, and compare-and-swap races."""

import asyncio
import copy
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from daveharness import KNOWN_PERMISSIONS, RunBudget, ToolDefinition, ToolRegistry
from daveharness.contracts import ContractDecodeError
from daveharness.state import (
    ApprovalDecision, PendingToolCall, RunSnapshot, RUN_STATUSES,
    TERMINAL_RUN_STATUSES, transition_run,
)
from daveharness.state_engine import decide_run, resume_run


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def process_handler(_args):
    return "ok"


def clock():
    return NOW


def snapshot(run_id="run_1", budget=None):
    return RunSnapshot.create(
        run_id=run_id, transcript=[{"role": "user", "content": "go"}],
        created_at=NOW.isoformat(), budget=budget,
        allowed_permissions=tuple(sorted(KNOWN_PERMISSIONS)),
    )


class FakeCAS:
    """Round-trip every boundary to simulate a separate process/store."""

    def __init__(self, value):
        self._value = self._roundtrip(value)
        self._lock = threading.Lock()
        self.fail_next = False

    @staticmethod
    def _roundtrip(value):
        return RunSnapshot.from_dict(json.loads(json.dumps(value.to_dict(), sort_keys=True)))

    def load(self, run_id):
        with self._lock:
            return self._roundtrip(self._value) if run_id == self._value.run_id else None

    def compare_and_swap(self, run_id, expected_version, value):
        with self._lock:
            if self.fail_next:
                self.fail_next = False
                return False
            if run_id != self._value.run_id or expected_version != self._value.optimistic_version:
                return False
            self._value = self._roundtrip(value)
            return True


def definition(name, effects, *, permission="read", approval=False, schema=None):
    def handler(args):
        effects.append((name, copy.deepcopy(args)))
        return "ok"

    return ToolDefinition(
        name=name, description=name, parameters=schema or {"type": "object"},
        handler=handler, permission=permission,
        approval_required=approval, cancellation="bounded" if approval or permission == "write" else "abandon",
    )


def native_call(call_id, name, args="{}"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def approval(pending, *, decision="approve", decision_id="decision_1", **changes):
    values = {
        "run_id": "run_1", "call_id": pending.call_id, "digest": pending.digest,
        "definition_fingerprint": pending.definition_fingerprint,
        "permission": pending.permission, "nonce": pending.nonce,
        "decision_id": decision_id, "decision": decision,
        "issued_at": pending.created_at, "expires_at": pending.expires_at,
    }
    values.update(changes)
    return ApprovalDecision(**values)


def test_snapshot_round_trip_and_unknown_versions_fail_closed():
    value = snapshot()
    encoded = value.to_dict()
    assert RunSnapshot.from_dict(encoded) == value
    encoded["transcript"][0]["content"] = "mutated"
    assert value.transcript[0]["content"] == "go"
    for changed in (
        {**value.to_dict(), "contract_version": 2},
        {key: item for key, item in value.to_dict().items() if key != "run_id"},
        {**value.to_dict(), "status": "unknown"},
    ):
        with pytest.raises(ContractDecodeError):
            RunSnapshot.from_dict(changed)
    for changed in (
        {**value.to_dict(), "approved_tools": ["write"]},
        {**value.to_dict(), "steps": 1},
        {**value.to_dict(), "model_inflight": True},
    ):
        with pytest.raises(ContractDecodeError):
            RunSnapshot.from_dict(changed)
    with pytest.raises(ValueError):
        replace(value, transcript_json='[{"value":NaN}]')


def test_pending_call_requires_canonical_finite_json():
    pending = PendingToolCall.create(
        call_id="call_1", tool_name="write", arguments={},
        definition_fingerprint="f" * 64, permission="write",
        registry_instance_id="registry_1", registration_revision=1,
        transcript_revision="t" * 64, nonce="nonce",
        created_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=300)).isoformat(),
    )
    with pytest.raises(ValueError):
        replace(pending, arguments_json='{"x":NaN}')
    with pytest.raises(ValueError):
        replace(pending, remaining_calls_json='[{"x":NaN}]')


def test_all_statuses_validate_and_terminal_state_is_immutable():
    base = snapshot()
    running = transition_run(base, "running", updated_at=NOW.isoformat(), steps=1)
    for status in RUN_STATUSES:
        if status == "approval_required":
            pending = PendingToolCall.create(
                call_id="call_1", tool_name="write", arguments={},
                definition_fingerprint="f" * 64, permission="write",
                registry_instance_id="registry_1", registration_revision=1,
                transcript_revision="t" * 64, nonce="nonce",
                created_at=NOW.isoformat(),
                expires_at=(NOW + timedelta(seconds=300)).isoformat(),
            )
            value = replace(running, status=status, tool_calls=1, pending_call=pending)
        elif status == "created":
            value = base
        else:
            value = replace(running, status=status)
        assert RunSnapshot.from_dict(value.to_dict()) == value
        if status in TERMINAL_RUN_STATUSES:
            assert transition_run(value, status, updated_at=NOW.isoformat()) == value
            with pytest.raises(ValueError):
                transition_run(value, "running", updated_at=NOW.isoformat())
    started = transition_run(base, "running", updated_at=NOW.isoformat())
    assert started.optimistic_version == base.optimistic_version + 1
    assert transition_run(started, "running", updated_at=NOW.isoformat()) == started
    with pytest.raises(ValueError):
        transition_run(base, "completed", updated_at=NOW.isoformat())
    with pytest.raises(ValueError):
        transition_run(started, "created", updated_at=NOW.isoformat())


def test_transition_matrix_rejects_all_unlisted_edges():
    base = snapshot()
    running = transition_run(base, "running", updated_at=NOW.isoformat(), steps=1)
    pending = PendingToolCall.create(
        call_id="call_1", tool_name="write", arguments={},
        definition_fingerprint="f" * 64, permission="write",
        registry_instance_id="registry_1", registration_revision=1,
        transcript_revision="t" * 64, nonce="nonce",
        created_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=300)).isoformat(),
    )
    approval_state = transition_run(
        running, "approval_required", updated_at=NOW.isoformat(), pending_call=pending,
        tool_calls=1,
    )
    legal = {
        "created": {"running", "run_expired"},
        "running": {"approval_required", "completed", "model_timeout", "model_error", "error_budget", "step_limit", "budget_exceeded", "cancelling", "run_conflict", "run_expired"},
        "approval_required": {"running", "approval_rejected", "run_expired", "run_conflict"},
        "cancelling": {"cancelled", "cancellation_failed", "run_conflict"},
    }
    states = {
        "created": base,
        "running": running,
        "approval_required": approval_state,
        "cancelling": transition_run(running, "cancelling", updated_at=NOW.isoformat()),
    }
    for source, value in states.items():
        for target in RUN_STATUSES - {source}:
            changes = {"pending_call": pending, "tool_calls": 1} if target == "approval_required" else {}
            if target == "completed":
                changes["steps"] = max(value.steps, 1)
            if source == "approval_required":
                changes["pending_call"] = None
            if target in legal[source]:
                assert transition_run(value, target, updated_at=NOW.isoformat(), **changes).status == target
            else:
                with pytest.raises(ValueError):
                    transition_run(value, target, updated_at=NOW.isoformat(), **changes)


@pytest.mark.asyncio
async def test_approval_round_trip_executes_once_without_replaying_model_step():
    effects = []
    registry = ToolRegistry()
    registry.register(definition("write", effects, permission="write", approval=True))
    calls = []

    async def model(messages, _schemas):
        calls.append(len(messages))
        if len(calls) == 1:
            return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]}
        return {"role": "assistant", "content": "done"}

    store = FakeCAS(snapshot())
    paused = await resume_run(store, "run_1", registry, model, clock=clock)
    assert paused.status == "approval_required"
    assert len(calls) == 1 and effects == []
    pending = store.load("run_1").pending_call
    assert PendingToolCall.from_dict(pending.to_dict()) == pending
    decision = approval(pending)
    assert ApprovalDecision.from_dict(decision.to_dict()) == decision

    resumed = await decide_run(store, decision, registry, model, clock=clock)
    assert resumed.status == "completed"
    assert len(calls) == 2
    assert effects == [("write", {})]
    assert resumed.snapshot.executed_call_ids == ("call_1",)
    assert resumed.snapshot.used_decision_ids == ("decision_1",)
    assert resumed.snapshot.tool_calls == 1
    replay = await decide_run(store, decision, registry, model, clock=clock)
    assert replay.reason_code == "approval_not_pending"
    assert effects == [("write", {})]


@pytest.mark.asyncio
async def test_concurrent_resume_reserves_one_model_invocation():
    registry = ToolRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()
    invocations = []

    async def model(_messages, _schemas):
        invocations.append(True)
        entered.set()
        await release.wait()
        return {"role": "assistant", "content": "done"}

    store = FakeCAS(snapshot())
    first = asyncio.create_task(resume_run(store, "run_1", registry, model, clock=clock))
    await entered.wait()
    second = await resume_run(store, "run_1", registry, model, clock=clock)
    assert second.reason_code == "run_conflict"
    assert len(invocations) == 1
    release.set()
    assert (await first).status == "completed"


@pytest.mark.asyncio
async def test_name_level_approval_cannot_bypass_exact_decision():
    registry = ToolRegistry()
    effects = []
    registry.register(definition("write", effects, permission="write", approval=True))

    async def model(_messages, _schemas):
        return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]}

    store = FakeCAS(snapshot())
    unsafe = {**store.load("run_1").to_dict(), "approved_tools": ["write"]}
    with pytest.raises(ContractDecodeError):
        RunSnapshot.from_dict(unsafe)
    result = await resume_run(store, "run_1", registry, model, clock=clock)
    assert result.status == "approval_required"
    assert effects == []


@pytest.mark.asyncio
async def test_tail_survives_approval_without_repeating_prior_call_or_budget_charge():
    effects = []
    registry = ToolRegistry()
    registry.register(definition("read", effects))
    registry.register(definition("write", effects, permission="write", approval=True))
    model_calls = []

    async def model(_messages, _schemas):
        model_calls.append(True)
        if len(model_calls) == 1:
            return {"role": "assistant", "tool_calls": [
                native_call("first", "read"), native_call("second", "write"),
                native_call("third", "read"),
            ]}
        return {"role": "assistant", "content": "done"}

    store = FakeCAS(snapshot(budget=RunBudget(tool_calls=3)))
    paused = await resume_run(store, "run_1", registry, model, clock=clock)
    assert paused.status == "approval_required"
    assert paused.snapshot.tool_calls == 2
    assert paused.snapshot.executed_call_ids == ("first",)
    assert [item[0] for item in effects] == ["read"]
    pending = paused.snapshot.pending_call
    assert [item["call_id"] for item in pending.remaining_calls] == ["third"]

    done = await decide_run(store, approval(pending), registry, model, clock=clock)
    assert done.status == "completed"
    assert done.snapshot.tool_calls == 3
    assert [item[0] for item in effects] == ["read", "write", "read"]
    assert len(model_calls) == 2


@pytest.mark.asyncio
async def test_reject_expiry_mismatch_revocation_and_conflict_have_no_effect():
    effects = []
    registry = ToolRegistry()
    item = definition("write", effects, permission="write", approval=True)
    registry.register(item)

    async def model(messages, _schemas):
        return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]} if len(messages) == 1 else {"role": "assistant", "content": "done"}

    async def paused_store():
        store = FakeCAS(snapshot())
        pause = await resume_run(store, "run_1", registry, model, clock=clock)
        assert pause.status == "approval_required"
        return store, pause.snapshot.pending_call

    store, pending = await paused_store()
    mismatched = await decide_run(store, approval(pending, digest="0" * 64), registry, model, clock=clock)
    assert mismatched.reason_code == "approval_mismatch" and effects == []
    rejected = await decide_run(store, approval(pending, decision="reject"), registry, model, clock=clock)
    assert rejected.status == "approval_rejected" and effects == []

    store, pending = await paused_store()
    future = lambda: NOW + timedelta(seconds=301)
    expired = await decide_run(store, approval(pending), registry, model, clock=future)
    assert expired.status == "run_expired" and effects == []

    store, pending = await paused_store()
    registry.revoke("write")
    registry.register(item)
    stale = await decide_run(store, approval(pending), registry, model, clock=clock)
    assert stale.status == "run_conflict" and effects == []

    store, pending = await paused_store()
    store.fail_next = True
    conflict = await decide_run(store, approval(pending), registry, model, clock=clock)
    assert conflict.status == "run_conflict" and effects == []


@pytest.mark.asyncio
async def test_concurrent_decisions_execute_one_effect():
    effects = []
    registry = ToolRegistry()
    registry.register(definition("write", effects, permission="write", approval=True))

    async def model(messages, _schemas):
        return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]} if len(messages) == 1 else {"role": "assistant", "content": "done"}

    store = FakeCAS(snapshot())
    paused = await resume_run(store, "run_1", registry, model, clock=clock)
    decision = approval(paused.snapshot.pending_call)
    results = await asyncio.gather(
        decide_run(store, decision, registry, model, clock=clock),
        decide_run(store, decision, registry, model, clock=clock),
    )
    assert sorted(result.reason_code for result in results) == ["approval_not_pending", "completed"]
    assert effects == [("write", {})]


@pytest.mark.asyncio
async def test_pending_approval_rebinds_across_registry_process_boundary():
    first = ToolRegistry()
    second = ToolRegistry()
    for registry in (first, second):
        registry.register(ToolDefinition(
            name="write", description="write", parameters={"type": "object"},
            handler=process_handler, permission="write", approval_required=True,
            cancellation="bounded",
        ))

    async def model(messages, _schemas):
        if len(messages) == 1:
            return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]}
        return {"role": "assistant", "content": "done"}

    store = FakeCAS(snapshot())
    paused = await resume_run(store, "run_1", first, model, clock=clock)
    pending = paused.snapshot.pending_call
    assert pending.registry_instance_id != second.instance_id
    resumed = await decide_run(store, approval(pending), second, model, clock=clock)
    assert resumed.status == "completed"
    assert resumed.snapshot.executed_call_ids == ("call_1",)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["digest", "call_id", "permission", "definition_fingerprint", "nonce", "issued_at"])
async def test_exact_decision_rejects_mutated_fields(change):
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="write", description="write", parameters={"type": "object"},
        handler=process_handler, permission="write", approval_required=True,
        cancellation="bounded",
    ))

    async def model(messages, _schemas):
        if len(messages) == 1:
            return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]}
        return {"role": "assistant", "content": "done"}

    store = FakeCAS(snapshot())
    paused = await resume_run(store, "run_1", registry, model, clock=clock)
    pending = paused.snapshot.pending_call
    altered = {
        "digest": "0" * 64, "call_id": "another", "permission": "read",
        "definition_fingerprint": "0" * 64, "nonce": "another",
        "issued_at": (NOW + timedelta(seconds=1)).isoformat(),
    }
    result = await decide_run(store, approval(pending, **{change: altered[change]}), registry, model, clock=clock)
    assert result.reason_code == "approval_mismatch"
    assert store.load("run_1").status == "approval_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["schema", "permission", "tool_name"])
async def test_definition_change_invalidates_pending_approval(change):
    first = ToolRegistry()
    first.register(ToolDefinition(
        name="write", description="write", parameters={"type": "object"},
        handler=process_handler, permission="write", approval_required=True,
        cancellation="bounded",
    ))

    async def model(messages, _schemas):
        if len(messages) == 1:
            return {"role": "assistant", "tool_calls": [native_call("call_1", "write")]}
        return {"role": "assistant", "content": "done"}

    store = FakeCAS(snapshot())
    paused = await resume_run(store, "run_1", first, model, clock=clock)
    pending = paused.snapshot.pending_call
    second = ToolRegistry()
    second.register(ToolDefinition(
        name="other" if change == "tool_name" else "write", description="write",
        parameters={"type": "object", "required": ["x"]} if change == "schema" else {"type": "object"},
        handler=process_handler, permission="read" if change == "permission" else "write",
        approval_required=True, cancellation="bounded",
    ))
    result = await decide_run(store, approval(pending), second, model, clock=clock)
    assert result.status == "run_conflict"
    assert store.load("run_1").pending_call is None
