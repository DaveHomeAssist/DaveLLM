"""Optional tool preflight: a certain failure is refused before the approval pause, never authorized.

A preflight runs only for an approval-required call that passed schema
validation and would otherwise pause. A nonempty string refuses the call as an
ordinary tool error whose handler never ran (status "error", termination
"denied") and creates no pending call. Anything else, including an exception or
a timeout, leaves the call to pause exactly as before.
"""

import asyncio
import contextvars
import copy
import functools
import hashlib
import json
import threading
from datetime import datetime, timezone

import pytest

from daveharness import (
    ApprovalDecision, Harness, InMemoryPendingCallStore, KNOWN_PERMISSIONS, RunBudget, RunRequest,
    ToolDefinition, ToolRegistry, run_executor_loop,
)
from daveharness.limits import validate_payload
from daveharness.registry import _handler_provenance


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
PERMISSIONS = tuple(sorted(KNOWN_PERMISSIONS))
SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
CALLER = contextvars.ContextVar("caller", default=None)


def write_tool(effects, preflight=None, **overrides):
    fields = dict(
        name="note.write", description="Write a note", parameters=SCHEMA,
        handler=lambda args: effects.append(copy.deepcopy(args)) or "written",
        permission="write", approval_required=True, cancellation="bounded", preflight=preflight,
    )
    fields.update(overrides)
    return ToolDefinition(**fields)


def registry_with(*definitions):
    registry = ToolRegistry()
    for definition in definitions:
        registry.register(definition)
    return registry


def native_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def scripted_model(*replies):
    """Return each reply in turn, then a final answer."""
    calls = []

    async def model(messages, _schemas):
        calls.append(copy.deepcopy(messages))
        if len(calls) <= len(replies):
            return {"role": "assistant", "content": "", "tool_calls": replies[len(calls) - 1]}
        return {"role": "assistant", "content": "done"}

    return model, calls


def harness(registry, model, budget=None):
    return Harness(registry=registry, invoke_model=model, clock=lambda: NOW), RunRequest.create(
        [{"role": "user", "content": "go"}], run_id="run_1", allowed_permissions=PERMISSIONS,
        budget=budget,
    )


def tool_envelopes(transcript):
    return [(message["tool_call_id"], json.loads(message["content"]))
            for message in transcript if message.get("role") == "tool"]


def approval(pending):
    return ApprovalDecision(
        run_id="run_1", call_id=pending.call_id, digest=pending.digest,
        definition_fingerprint=pending.definition_fingerprint, permission=pending.permission,
        nonce=pending.nonce, decision_id="decision_1", decision="approve",
        issued_at=pending.created_at, expires_at=pending.expires_at,
    )


# Registration -----------------------------------------------------------------------------

def refuse(_arguments, _context):
    return "refused"


@pytest.mark.parametrize("overrides, message", [
    ({"approval_required": False, "permission": "read", "cancellation": "abandon"}, "requires approval_required"),
    ({"preflight": "not callable"}, "must be callable"),
    ({"preflight": lambda arguments: None}, "must accept arguments and context"),
    ({"preflight": None, "preflight_version": "v1"}, "preflight_version requires a preflight"),
])
def test_registration_rejects_a_preflight_that_could_never_run_as_declared(overrides, message):
    with pytest.raises(ValueError, match=message):
        registry_with(write_tool([], **{"preflight": refuse, **overrides}))


def test_registration_rejects_an_async_preflight():
    async def preflight(_arguments, _context):
        return None

    with pytest.raises(ValueError, match="must be synchronous"):
        registry_with(write_tool([], preflight=preflight))


def test_opaque_preflight_state_needs_an_explicit_version():
    opaque = functools.partial(lambda lock, _arguments, _context: None, threading.Lock())
    with pytest.raises(ValueError, match="explicit handler_version"):
        registry_with(write_tool([], preflight=opaque))
    registry = registry_with(write_tool([], preflight=opaque, preflight_version="ledger-v1"))
    assert registry.get("note.write").preflight is opaque


# Fingerprints ----------------------------------------------------------------------------

def fingerprint_without_preflight_key(definition):
    """The fingerprint algorithm as it was before preflights existed."""
    payload = {
        "name": definition.name, "description": definition.description,
        "parameters": definition.parameters, "permission": definition.permission,
        "approval_required": definition.approval_required, "timeout_seconds": definition.timeout_seconds,
        "cancellation": definition.cancellation, "async_handler": definition.async_handler,
        "context_handler": definition.context_handler,
        "handler": _handler_provenance(definition.handler, definition.handler_version),
    }
    validate_payload(payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_a_definition_without_a_preflight_keeps_its_fingerprint():
    definition = write_tool([])
    assert definition.fingerprint() == fingerprint_without_preflight_key(definition)
    read = ToolDefinition("note.read", "Read", SCHEMA, lambda args: "x", cancellation="bounded")
    assert read.fingerprint() == fingerprint_without_preflight_key(read)


def test_the_preflight_code_and_version_are_part_of_the_fingerprint():
    def other(_arguments, _context):
        return "other"

    plain = write_tool([])
    with_refuse = write_tool([], preflight=refuse)
    assert len({plain.fingerprint(), with_refuse.fingerprint(), write_tool([], preflight=other).fingerprint()}) == 3
    opaque = functools.partial(refuse, threading.Lock())
    assert (write_tool([], preflight=opaque, preflight_version="v1").fingerprint()
            != write_tool([], preflight=opaque, preflight_version="v2").fingerprint())
    registry = registry_with(with_refuse)
    _definition, _revision, fingerprint = registry.get_with_revision("note.write")
    assert fingerprint == with_refuse.fingerprint()


# Lifecycle runs ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_refused_call_never_pauses_and_reads_as_a_tool_error_that_never_ran():
    effects, seen = [], []

    def preflight(arguments, context):
        seen.append((arguments, context.run_id, context.call_id, context.output_budget_bytes, CALLER.get()))
        return "old text was not found; nothing was written"

    model, model_calls = scripted_model([native_call("call_1", "note.write", {"text": "hello"})])
    facade, request = harness(registry_with(write_tool(effects, preflight=preflight)), model)
    token = CALLER.set("host binding")
    try:
        result = await facade.start(request)
    finally:
        CALLER.reset(token)
    assert result.status == "completed"
    snapshot = result.snapshot
    assert snapshot.pending_call is None and not effects
    assert seen == [({"text": "hello"}, "run_1", "call_1", 65_536, "host binding")]
    [(call_id, envelope)] = tool_envelopes(snapshot.transcript)
    assert call_id == "call_1"
    assert (envelope["status"], envelope["termination"], envelope["result"]) == ("error", "denied", "")
    assert envelope["error"] == "old text was not found; nothing was written"
    assert (snapshot.errors, snapshot.tool_calls, snapshot.executed_call_ids) == (1, 1, ("call_1",))
    assert len(model_calls) == 2
    events = [(event.kind, event.status, event.reason_code) for event in facade.events("run_1")
              if event.call_id == "call_1"]
    assert events == [
        ("policy_decision", "pause", "approval_required"),
        ("tool_result", "error", "preflight_refused"),
    ]


@pytest.mark.asyncio
async def test_a_passing_preflight_pauses_once_and_approval_runs_the_handler_without_asking_again():
    effects, seen = [], []

    def preflight(arguments, _context):
        seen.append(copy.deepcopy(arguments))
        arguments["text"] = "mutated by preflight"
        return None

    model, _ = scripted_model([native_call("call_1", "note.write", {"text": "hello"})])
    facade, request = harness(registry_with(write_tool(effects, preflight=preflight)), model)
    paused = await facade.start(request)
    assert paused.status == "approval_required"
    pending = paused.snapshot.pending_call
    assert pending.arguments == {"text": "hello"}  # the preflight saw a copy
    assert [event.kind for event in facade.events("run_1")].count("approval_required") == 1
    decided = await facade.decide(approval(pending))
    assert decided.status == "completed"
    assert effects == [{"text": "hello"}]
    assert seen == [{"text": "hello"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [None, "", True, {"error": "x"}, "x" * 70_000])
async def test_anything_but_a_refusal_that_fits_the_output_budget_pauses(answer):
    model, _ = scripted_model([native_call("call_1", "note.write", {"text": "hello"})])
    facade, request = harness(registry_with(write_tool([], preflight=lambda _a, _c: answer)), model)
    assert (await facade.start(request)).status == "approval_required"


@pytest.mark.asyncio
async def test_a_failing_preflight_has_no_say():
    def preflight(_arguments, _context):
        raise RuntimeError("ledger unavailable")

    model, _ = scripted_model([native_call("call_1", "note.write", {"text": "hello"})])
    facade, request = harness(registry_with(write_tool([], preflight=preflight)), model)
    assert (await facade.start(request)).status == "approval_required"


@pytest.mark.asyncio
async def test_a_late_preflight_is_abandoned_and_the_call_pauses():
    release = threading.Event()

    def preflight(_arguments, _context):
        release.wait(5)
        return "too late"

    model, _ = scripted_model([native_call("call_1", "note.write", {"text": "hello"})])
    definition = write_tool([], preflight=preflight, preflight_version="late-v1", timeout_seconds=0.05)
    facade, request = harness(registry_with(definition), model)
    try:
        assert (await facade.start(request)).status == "approval_required"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_invalid_arguments_never_reach_the_preflight():
    seen = []
    model, _ = scripted_model([native_call("call_1", "note.write", {"text": 7})])
    definition = write_tool([], preflight=lambda arguments, _c: seen.append(arguments) or "refused")
    facade, request = harness(registry_with(definition), model)
    result = await facade.start(request)
    [(_, envelope)] = tool_envelopes(result.snapshot.transcript)
    assert envelope["status"] == "validation_error" and seen == []


@pytest.mark.asyncio
async def test_refusals_count_against_the_error_budget_and_later_calls_still_run():
    effects, reads = [], []
    model, _ = scripted_model(
        [native_call("call_1", "note.write", {"text": "a"}), native_call("call_2", "note.read", {"text": "b"})],
        [native_call("call_3", "note.write", {"text": "c"})],
    )
    registry = registry_with(
        write_tool(effects, preflight=refuse),
        ToolDefinition("note.read", "Read", SCHEMA, lambda args: reads.append(args) or "read", cancellation="bounded"),
    )
    facade, request = harness(registry, model, budget=RunBudget(errors=2))
    result = await facade.start(request)
    assert result.status == "error_budget"
    assert reads == [{"text": "b"}] and not effects
    assert [(call_id, envelope["status"]) for call_id, envelope in tool_envelopes(result.snapshot.transcript)] == [
        ("call_1", "error"), ("call_2", "success"), ("call_3", "error"),
    ]


# The shipped executor loop -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_executor_loop_refuses_without_saving_a_pending_call():
    effects, store = [], InMemoryPendingCallStore()
    model, _ = scripted_model([native_call("call_1", "note.write", {"text": "hello"})])
    outcome = await run_executor_loop(
        [{"role": "user", "content": "go"}], model, registry=registry_with(write_tool(effects, preflight=refuse)),
        pending_store=store, run_id="run_legacy",
    )
    assert outcome.status == "completed" and outcome.pending_tool_call is None and not effects
    [(_, envelope)] = tool_envelopes(outcome.transcript)
    assert (envelope["status"], envelope["termination"], envelope["error"]) == ("error", "denied", "refused")
    assert store.lookup("run_legacy", "call_1").status == "approval_not_found"


@pytest.mark.asyncio
async def test_the_executor_loop_pauses_when_the_preflight_passes_and_skips_it_for_preapproved_tools():
    seen = []
    model, _ = scripted_model([native_call("call_1", "note.write", {"text": "hello"})])
    registry = registry_with(write_tool([], preflight=lambda arguments, _c: seen.append(arguments)))
    outcome = await run_executor_loop(
        [{"role": "user", "content": "go"}], model, registry=registry, pending_store=InMemoryPendingCallStore(),
    )
    assert outcome.status == "approval_required" and seen == [{"text": "hello"}]

    effects = []
    model, _ = scripted_model([native_call("call_1", "note.write", {"text": "hello"})])
    outcome = await run_executor_loop(
        [{"role": "user", "content": "go"}], model, registry=registry_with(write_tool(effects, preflight=refuse)),
        approved_tools={"note.write"}, pending_store=InMemoryPendingCallStore(),
    )
    assert outcome.status == "completed" and effects == [{"text": "hello"}]
