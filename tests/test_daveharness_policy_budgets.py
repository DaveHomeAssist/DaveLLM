"""H2 policy and budget boundaries, including the legacy compatibility seam."""

import pytest
from functools import wraps
from functools import partial
from types import SimpleNamespace

import daveharness.engine as engine

from daveharness import (
    KNOWN_PERMISSIONS, RunBudget, RunPolicyContext, ToolDefinition, ToolPolicy,
    ToolRegistry, run_executor_loop, run_tool, resume_executor_loop,
    InMemoryPendingCallStore,
)


def definition(name="read", *, permission="read", approval=False, schema=None, handler=None):
    return ToolDefinition(
        name=name, description="one", parameters=schema or {"type": "object"},
        handler=handler or (lambda _args: "ok"), permission=permission,
        approval_required=approval, cancellation="bounded" if approval or permission.startswith("write") else "abandon",
    )


def test_definition_fingerprint_is_canonical_and_security_sensitive():
    handler = lambda _args: "ok"
    first = definition(schema={"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "number"}}}, handler=handler)
    reordered = definition(schema={"properties": {"a": {"type": "number"}, "b": {"type": "string"}}, "type": "object"}, handler=handler)
    assert first.fingerprint() == reordered.fingerprint()
    assert len(first.fingerprint()) == 64
    for changed in (
        definition(permission="write", handler=handler),
        definition(approval=True, handler=handler),
        definition(schema={"type": "object", "additionalProperties": False}, handler=handler),
        definition(handler=lambda _args: "different"),
    ):
        assert changed.fingerprint() != first.fingerprint()

    def underlying(_args):
        return "base"

    @wraps(underlying)
    def wrapper_one(_args):
        return "one"

    @wraps(underlying)
    def wrapper_two(_args):
        return "two"

    assert definition(handler=wrapper_one).fingerprint() != definition(handler=wrapper_two).fingerprint()

    class Stateful:
        def __init__(self, label):
            self.label = label

        def __call__(self, _args):
            return self.label

    assert definition(handler=Stateful("one")).fingerprint() != definition(handler=Stateful("two")).fingerprint()
    assert definition(handler=Stateful("one").__call__).fingerprint() != definition(handler=Stateful("two").__call__).fingerprint()

    def prefixed(prefix, _args):
        return prefix

    assert definition(handler=partial(prefixed, "one")).fingerprint() != definition(handler=partial(prefixed, "two")).fingerprint()
    assert definition(handler=len).fingerprint() != definition(handler=sorted).fingerprint()


@pytest.mark.parametrize("permission", sorted(KNOWN_PERMISSIONS))
def test_policy_allows_each_known_permission_when_host_allows_it(permission):
    registry = ToolRegistry()
    registry.register(definition(permission=permission))
    decision = ToolPolicy().evaluate(registry, "read", RunPolicyContext())
    assert (decision.action, decision.reason_code, decision.permission) == ("allow", "policy_allowed", permission)
    denied = ToolPolicy().evaluate(registry, "read", RunPolicyContext(allowed_permissions=KNOWN_PERMISSIONS - {permission}))
    assert (denied.action, denied.reason_code) == ("deny", "permission_denied")


def test_policy_denies_unknown_revoked_and_forbidden_then_pauses_exact_approval():
    registry = ToolRegistry()
    policy = ToolPolicy()
    assert policy.evaluate(registry, "missing", RunPolicyContext()).reason_code == "tool_unknown"
    registry.register(definition())
    assert policy.evaluate(registry, "read", RunPolicyContext(allowed_permissions={"write"})).reason_code == "permission_denied"
    registry.revoke("read")
    assert policy.evaluate(registry, "read", RunPolicyContext()).reason_code == "tool_revoked"
    registry.register(definition(permission="unrecognized"))
    assert policy.evaluate(registry, "read", RunPolicyContext()).reason_code == "permission_unknown"
    registry.revoke("read")
    registry.register(definition(permission="write", approval=True))
    pause = policy.evaluate(registry, "read", RunPolicyContext())
    assert (pause.action, pause.reason_code) == ("pause", "approval_required")
    assert policy.evaluate(registry, "read", RunPolicyContext(approved_tools={"read"})).reason_code == "approval_granted"
    assert policy.evaluate(registry, "read", RunPolicyContext(approved_tools={"read"}), expected_fingerprint="0" * 64).reason_code == "definition_changed"
    assert policy.evaluate(registry, "read", RunPolicyContext(approved_tools={"read"}), expected_permission="read").reason_code == "permission_changed"


def test_budget_defaults_legacy_adapter_and_boundaries():
    budget = RunBudget()
    assert (budget.model_steps, budget.errors, budget.tool_calls, budget.total_wall_seconds) == (8, 2, 32, 300)
    for name in ("model_steps", "errors", "tool_calls", "tool_output_bytes", "transcript_bytes", "event_count"):
        limit = getattr(budget, name)
        assert not budget.exceeded(name, limit)
        assert budget.exceeded(name, limit + 1)
    assert not budget.wall_expired(10, 309.999)
    assert budget.wall_expired(10, 310)
    assert RunBudget.legacy().tool_calls is None
    assert RunBudget.legacy().event_count is None
    with pytest.raises(ValueError):
        RunBudget(tool_calls=0)
    with pytest.raises(ValueError):
        RunBudget(model_steps=None)
    with pytest.raises(ValueError):
        RunBudget(total_wall_seconds=float("nan"))


@pytest.mark.asyncio
async def test_pre_effect_definition_change_and_revocation_fail_closed():
    registry = ToolRegistry()
    effects = []
    registry.register(definition(handler=lambda _args: effects.append("ran")))

    class RevokingPolicy(ToolPolicy):
        def evaluate(self, registry, name, context, **kwargs):
            decision = super().evaluate(registry, name, context, **kwargs)
            registry.revoke(name)
            return decision

    result = await run_tool("read", {}, registry=registry, policy=RevokingPolicy())
    assert (result.status, result.error, effects) == ("revoked", "definition_changed", [])


@pytest.mark.asyncio
async def test_atomic_dispatch_rejects_revocation_before_effect_start():
    effects = []

    class RacingRegistry(ToolRegistry):
        def begin_execution(self, name, fingerprint, revision):
            self.revoke(name)
            return super().begin_execution(name, fingerprint, revision)

    registry = RacingRegistry()
    registry.register(definition(handler=lambda _args: effects.append("ran")))
    result = await run_tool("read", {}, registry=registry)
    assert (result.status, effects) == ("revoked", [])


@pytest.mark.asyncio
async def test_run_context_forbids_permission_before_effect():
    effects = []
    registry = ToolRegistry()
    registry.register(definition(handler=lambda _args: effects.append("ran")))

    async def model(messages, _schemas):
        if len(messages) == 1:
            return {"role": "assistant", "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
        return {"role": "assistant", "content": "done"}

    outcome = await run_executor_loop(
        [{"role": "user", "content": "go"}], model, registry=registry,
        policy_context=RunPolicyContext(allowed_permissions=frozenset()),
    )
    assert outcome.status == "completed"
    assert outcome.transcript[-2]["name"] == "read"
    assert "permission_denied" in outcome.transcript[-2]["content"]
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_changed", [False, True])
async def test_pending_approval_rechecks_definition_before_effect(replacement_changed):
    registry = ToolRegistry()
    effects = []
    original_handler = lambda _args: effects.append("old")
    registry.register(definition(name="write", permission="write", approval=True, handler=original_handler))
    store = InMemoryPendingCallStore()

    async def model(messages, _schemas):
        if len(messages) == 1:
            return {"role": "assistant", "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "write", "arguments": "{}"}}]}
        return {"role": "assistant", "content": "done"}

    pending = await run_executor_loop([{"role": "user", "content": "go"}], model, registry=registry, pending_store=store)
    assert pending.status == "approval_required"
    registry.revoke("write")
    replacement = (lambda _args: effects.append("new")) if replacement_changed else original_handler
    registry.register(definition(name="write", permission="write", approval=True, handler=replacement))
    resumed = await resume_executor_loop(run_id=pending.run_id, call_id="call1", digest=pending.pending_tool_call["digest"], decision="approve", pending_store=store)
    assert resumed.status == "approval_stale"
    assert effects == []


@pytest.mark.asyncio
async def test_new_budget_limits_calls_output_and_transcript_without_tightening_legacy():
    registry = ToolRegistry()
    effects = []
    registry.register(definition(handler=lambda _args: effects.append("ran") or "output"))

    async def two_calls(_messages, _schemas):
        return {"role": "assistant", "tool_calls": [
            {"id": str(index), "type": "function", "function": {"name": "read", "arguments": "{}"}}
            for index in (1, 2)
        ]}

    limited = await run_executor_loop([{"role": "user", "content": "go"}], two_calls, registry=registry, budget=RunBudget(tool_calls=1))
    assert (limited.status, limited.reason_code, effects) == ("budget_exceeded", "tool_call_limit", ["ran"])

    output = await run_tool("read", {}, registry=registry, output_bytes=5)
    assert (output.status, output.error) == ("output_limit", "tool_output_limit")

    error_registry = ToolRegistry()
    error_registry.register(definition(handler=lambda _args: {"status": "error", "error": "ééé"}))
    within = await run_tool("read", {}, registry=error_registry, output_bytes=6)
    over = await run_tool("read", {}, registry=error_registry, output_bytes=5)
    assert (within.status, within.error) == ("error", "ééé")
    assert (over.status, over.error) == ("output_limit", "tool_output_limit")

    def raises(_args):
        raise ValueError("ééé")

    error_registry.revoke("read")
    error_registry.register(definition(handler=raises))
    thrown = await run_tool("read", {}, registry=error_registry, output_bytes=5)
    assert (thrown.status, thrown.error) == ("output_limit", "tool_output_limit")

    model_calls = []
    async def never(_messages, _schemas):
        model_calls.append(1)
        return {"role": "assistant", "content": "done"}
    transcript = await run_executor_loop([{"role": "user", "content": "long message"}], never, budget=RunBudget(transcript_bytes=5))
    assert (transcript.status, transcript.reason_code, model_calls) == ("budget_exceeded", "transcript_limit", [])


@pytest.mark.asyncio
async def test_total_wall_boundary_stops_before_model(monkeypatch):
    readings = iter((10.0, 310.0))
    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: next(readings)))
    invoked = []

    async def model(_messages, _schemas):
        invoked.append(True)
        return {"role": "assistant", "content": "late"}

    outcome = await run_executor_loop([{"role": "user", "content": "go"}], model, budget=RunBudget())
    assert (outcome.status, outcome.reason_code, invoked) == ("budget_exceeded", "run_wall_limit", [])
