"""H5 event ordering, bounds, redaction, and failure isolation."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import pytest

from daveharness import KNOWN_PERMISSIONS, OperationController, ToolDefinition, ToolRegistry, run_tool
from daveharness.events import EVENT_KINDS, EventJournal, RunEvent, safe_identifier
from daveharness.state import ApprovalDecision, RunSnapshot
from daveharness.state_engine import cancel_run, decide_run, resume_run


NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


class Store:
    def __init__(self, snapshot):
        self.value = snapshot
        self.fail_next = False

    def load(self, run_id):
        return RunSnapshot.from_dict(self.value.to_dict()) if run_id == self.value.run_id else None

    def compare_and_swap(self, run_id, expected_version, snapshot):
        if self.fail_next:
            self.fail_next = False
            return False
        if run_id != self.value.run_id or expected_version != self.value.optimistic_version:
            return False
        self.value = RunSnapshot.from_dict(snapshot.to_dict())
        return True


def store(run_id="run_1"):
    return Store(RunSnapshot.create(
        run_id=run_id, transcript=[{"role": "user", "content": "SECRET_PROMPT"}],
        created_at=NOW.isoformat(), allowed_permissions=tuple(sorted(KNOWN_PERMISSIONS)),
    ))


def event(sequence, kind="model_result", *, run_id="run_1"):
    return RunEvent(
        run_id=run_id, sequence=sequence, kind=kind, step=1,
        call_id=None, tool_name=None, status=None, reason_code=None,
        timestamp=NOW.isoformat(),
    )


def test_versioned_event_fixtures_have_exact_safe_shape():
    for kind in EVENT_KINDS:
        value = event(1, kind)
        assert RunEvent.from_dict(json.loads(json.dumps(value.to_dict()))) == value
        assert set(value.to_dict()) == set(RunEvent.__dataclass_fields__)
        assert "SECRET_PROMPT" not in json.dumps(value.to_dict())
    invalid = event(1).to_dict()
    invalid["event_version"] = 2
    with pytest.raises(ValueError, match="version"):
        RunEvent.from_dict(invalid)
    invalid = event(1).to_dict()
    invalid["content"] = "SECRET_PROMPT"
    with pytest.raises(ValueError, match="fields"):
        RunEvent.from_dict(invalid)
    with pytest.raises(ValueError, match="safe identifier"):
        RunEvent(**{**event(1).to_dict(), "call_id": "https://secret.example/path"})


@pytest.mark.asyncio
async def test_completion_events_are_cas_ordered_and_redacted():
    value = store("https://secret.example/run")
    journal = EventJournal()

    async def model(_messages, _schemas):
        return {"role": "assistant", "content": "SECRET_RESULT"}

    result = await resume_run(value, value.value.run_id, ToolRegistry(), model, events=journal, clock=lambda: NOW)
    await journal.flush()
    assert result.status == "completed"
    events = journal.events(value.value.run_id)
    assert [item.kind for item in events] == ["created", "run_started", "model_start", "model_result", "terminal"]
    assert [item.sequence for item in events] == list(range(1, 6))
    assert result.snapshot.event_cursor == 5
    assert events[-1].status == "completed"
    assert journal.events(value.value.run_id, after=3) == events[3:]
    assert "secret.example" not in json.dumps([item.to_dict() for item in events])
    assert "SECRET_PROMPT" not in json.dumps([item.to_dict() for item in events])
    assert "SECRET_RESULT" not in json.dumps([item.to_dict() for item in events])


@pytest.mark.asyncio
async def test_failed_cas_has_no_event_or_model_effect():
    value = store()
    value.fail_next = True
    journal = EventJournal()
    calls = []

    async def model(_messages, _schemas):
        calls.append(1)
        return {"role": "assistant", "content": "done"}

    result = await resume_run(value, "run_1", ToolRegistry(), model, events=journal, clock=lambda: NOW)
    assert result.status == "run_conflict"
    assert not journal.events("run_1")
    assert not calls
    assert value.value.event_cursor == 0


@pytest.mark.asyncio
async def test_sink_failure_cannot_change_outcome_or_repeat_effect():
    class FailingSink:
        async def emit(self, _event):
            raise RuntimeError("SECRET_SINK")

    journal = EventJournal(sink=FailingSink())
    value = store()
    calls = []

    async def model(_messages, _schemas):
        calls.append(1)
        return {"role": "assistant", "content": "done"}

    result = await resume_run(value, "run_1", ToolRegistry(), model, events=journal, clock=lambda: NOW)
    await journal.flush()
    assert result.status == "completed"
    assert calls == [1]
    assert journal.sink_failures == 5
    assert journal.sink_failure("run_1").kind == "sink_failed"
    assert [item.kind for item in journal.events("run_1")].count("terminal") == 1
    assert "SECRET_SINK" not in json.dumps(journal.sink_failure("run_1").to_dict())


@pytest.mark.asyncio
async def test_retention_preserves_first_truncation_latest_and_terminal():
    journal = EventJournal(max_count=4, max_bytes=2_000)
    for sequence in range(1, 30):
        journal.record(event(sequence, "created" if sequence == 1 else "terminal" if sequence == 29 else "model_result"))
    await journal.flush()
    retained = journal.events("run_1")
    assert retained[0].kind == "created"
    assert retained[-1].kind == "terminal"
    assert len(retained) <= 4
    assert [item.kind for item in retained].count("truncated") == 1
    assert sum(item.byte_size for item in retained) <= 2_000
    assert [item.sequence for item in retained] == sorted(item.sequence for item in retained)
    assert journal.events("run_1", after=retained[-2].sequence) == (retained[-1],)


@pytest.mark.asyncio
async def test_byte_overflow_and_small_count_stay_bounded():
    journal = EventJournal(max_count=10, max_bytes=800)
    for sequence in range(1, 20):
        journal.record(event(sequence, "created" if sequence == 1 else "terminal" if sequence == 19 else "model_result"))
    await journal.flush()
    retained = journal.events("run_1")
    assert retained[0].kind == "created"
    assert retained[-1].kind == "terminal"
    assert [item.kind for item in retained].count("truncated") == 1
    assert sum(item.byte_size for item in retained) <= 800
    tiny = EventJournal(max_count=2)
    for sequence in range(1, 5):
        tiny.record(event(sequence, "created" if sequence == 1 else "terminal" if sequence == 4 else "model_result"))
    await tiny.flush()
    assert [item.kind for item in tiny.events("run_1")] == ["created", "terminal"]


@pytest.mark.asyncio
async def test_approval_events_survive_store_round_trip_without_model_replay():
    effects = []
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="write", description="Write", parameters={"type": "object"},
        handler=lambda args: effects.append(args) or "ok", permission="write",
        approval_required=True, cancellation="bounded",
    ))
    model_calls = []

    async def model(messages, _schemas):
        model_calls.append(1)
        if len(messages) == 1:
            return {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "write", "arguments": "{}"}}]}
        return {"role": "assistant", "content": "done"}

    value = store()
    journal = EventJournal()
    paused = await resume_run(value, "run_1", registry, model, events=journal, clock=lambda: NOW)
    assert paused.status == "approval_required"
    pending = value.load("run_1").pending_call
    decision = ApprovalDecision(
        run_id="run_1", call_id=pending.call_id, digest=pending.digest,
        definition_fingerprint=pending.definition_fingerprint, permission=pending.permission,
        nonce=pending.nonce, decision_id="decision_1", decision="approve",
        issued_at=pending.created_at, expires_at=pending.expires_at,
    )
    finished = await decide_run(value, decision, registry, model, events=journal, clock=lambda: NOW)
    await journal.flush()
    assert finished.status == "completed"
    assert effects == [{}]
    assert model_calls == [1, 1]
    kinds = [item.kind for item in journal.events("run_1")]
    assert kinds == [
        "created", "run_started", "model_start", "model_result", "policy_decision",
        "approval_required", "approval_decision", "policy_decision", "tool_start",
        "tool_result", "model_start", "model_result", "terminal",
    ]
    assert finished.snapshot.event_cursor == len(kinds)
    replay = await decide_run(value, decision, registry, model, events=journal, clock=lambda: NOW)
    assert replay.reason_code == "approval_not_pending"
    assert effects == [{}]
    assert len(journal.events("run_1")) == len(kinds)


@pytest.mark.asyncio
async def test_model_cancel_race_emits_one_terminal():
    entered = asyncio.Event()
    controller = OperationController()
    value = store()
    journal = EventJournal()

    async def model(_messages, _schemas):
        entered.set()
        await asyncio.Event().wait()

    running = asyncio.create_task(resume_run(
        value, "run_1", ToolRegistry(), model, controller=controller,
        events=journal, clock=lambda: NOW,
    ))
    await entered.wait()
    cancelled = await cancel_run(value, "run_1", controller=controller, events=journal, clock=lambda: NOW)
    await running
    await journal.flush()
    assert cancelled.status == "cancelled"
    assert [item.kind for item in journal.events("run_1")].count("terminal") == 1


@pytest.mark.asyncio
async def test_tool_event_canaries_never_enter_metadata_or_logs(caplog):
    tool_name = "https://secret.example/tool"
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name=tool_name, description="Test", parameters={"type": "object"},
        handler=lambda _args: "SECRET_RESULT", permission="read",
    ))
    value = store()
    journal = EventJournal()

    async def model(messages, _schemas):
        if len(messages) == 1:
            return {"role": "assistant", "tool_calls": [{
                "id": "SECRET_CALL", "type": "function",
                "function": {"name": tool_name, "arguments": '{"SECRET_KEY":"SECRET_ARG"}'},
            }]}
        return {"role": "assistant", "content": "done"}

    with caplog.at_level(logging.INFO, logger="dave_llm.tools"):
        result = await resume_run(value, "run_1", registry, model, events=journal, clock=lambda: NOW)
    await journal.flush()
    assert result.status == "completed"
    exposed = json.dumps([item.to_dict() for item in journal.events("run_1")]) + "\n" + "\n".join(item.message for item in caplog.records)
    for canary in ("SECRET_PROMPT", "SECRET_RESULT", "SECRET_CALL", "SECRET_KEY", "SECRET_ARG", "secret.example", "/tool"):
        assert canary not in exposed


@pytest.mark.asyncio
async def test_logs_redact_hostile_identifiers_and_argument_keys(caplog):
    registry = ToolRegistry()
    registry.register(ToolDefinition(
        name="echo", description="Echo", parameters={"type": "object"}, handler=lambda args: args,
    ))
    with caplog.at_level(logging.INFO, logger="dave_llm.tools"):
        await run_tool(
            "echo", {"SECRET_KEY": "SECRET_ARG"},
            call_id="https://secret.example/path", registry=registry,
        )
    records = "\n".join(item.message for item in caplog.records)
    for canary in ("SECRET_KEY", "SECRET_ARG", "secret.example", "/path"):
        assert canary not in records
    assert safe_identifier("https://secret.example/path") in records
