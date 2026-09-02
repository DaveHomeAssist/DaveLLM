import inspect
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL
from daveharness import (
    InMemoryPendingCallStore,
    PENDING_CALL_TTL_SECONDS,
    ToolDefinition,
    ToolRegistry,
    resume_executor_loop,
    run_executor_loop,
    run_tool,
)


AUTH = {"X-API-Key": TEST_API_KEY}
MODEL_ID = "tool-model:latest"


def simple_schema():
    return {
        "type": "object",
        "properties": {"value": {"type": "string", "minLength": 1}},
        "required": ["value"],
        "additionalProperties": False,
    }


@pytest.mark.asyncio
async def test_registry_is_runtime_loadable_revocable_and_schema_validated():
    registry = ToolRegistry()
    received = []

    def remember(args):
        received.append(args["value"])
        return {"remembered": args["value"]}

    registry.register(
        ToolDefinition(
            name="remember",
            description="Remember a test value.",
            parameters=simple_schema(),
            handler=remember,
        )
    )

    assert registry.model_schemas()[0]["function"]["parameters"] == simple_schema()
    invalid = await run_tool("remember", {}, registry=registry)
    assert invalid.status == "validation_error"
    assert received == []

    valid = await run_tool("remember", {"value": "safe"}, registry=registry)
    assert valid.status == "success"
    assert json.loads(valid.result) == {"remembered": "safe"}
    assert valid.started_at and valid.completed_at
    assert valid.duration_ms >= 0

    assert registry.revoke("remember") is True
    revoked = await run_tool("remember", {"value": "blocked"}, registry=registry)
    assert revoked.status == "revoked"
    assert received == ["safe"]


@pytest.mark.asyncio
async def test_tool_call_and_result_logs_include_timing(caplog):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo text.",
            parameters=simple_schema(),
            handler=lambda args: args["value"],
        )
    )
    caplog.set_level(logging.INFO, logger="dave_llm.tools")

    await run_tool(
        "echo",
        {"value": "logged"},
        call_id="call_logged",
        registry=registry,
    )

    events = [json.loads(record.message) for record in caplog.records]
    call_event = next(item for item in events if item["event"] == "tool_call")
    result_event = next(item for item in events if item["event"] == "tool_result")
    assert call_event["call_id"] == "call_logged"
    assert call_event["timestamp"]
    assert result_event["call_id"] == "call_logged"
    assert result_event["timestamp"]
    assert result_event["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_run_tool_returns_timeout_instead_of_raising():
    registry = ToolRegistry()

    def stall(_args):
        time.sleep(0.05)
        return "late"

    registry.register(
        ToolDefinition(
            name="stall",
            description="Wait too long.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=stall,
            timeout_seconds=0.001,
        )
    )
    result = await run_tool("stall", {}, registry=registry)
    assert result.status == "timeout"
    assert "timeout" in result.error
    assert result.termination == "deadline_abandoned"


@pytest.mark.asyncio
async def test_executor_times_out_a_synchronous_model_adapter():
    def stall(_messages, _schemas):
        time.sleep(0.05)
        return {"role": "assistant", "content": "late"}

    outcome = await run_executor_loop(
        [{"role": "user", "content": "Wait"}],
        stall,
        model_timeout_seconds=0.001,
    )
    assert outcome.status == "model_timeout"
    assert outcome.steps == 1


@pytest.mark.asyncio
async def test_executor_records_tool_call_result_and_final_answer():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo text.",
            parameters=simple_schema(),
            handler=lambda args: args["value"],
        )
    )
    requests = []

    async def invoke(messages, schemas):
        requests.append((messages, schemas))
        if len(requests) == 1:
            return {
                "role": "assistant",
                "content": "Checking.",
                "tool_calls": [
                    {
                        "id": "call_echo",
                        "type": "function",
                        "function": {
                            "name": "echo",
                            "arguments": '{"value":"ready"}',
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "The tool returned ready."}

    outcome = await run_executor_loop(
        [{"role": "user", "content": "Check readiness"}],
        invoke,
        registry=registry,
    )

    assert outcome.status == "completed"
    assert outcome.final_answer == "The tool returned ready."
    assert outcome.steps == 2
    assert len(requests) == 2
    assert requests[0][1] == requests[1][1]
    tool_message = next(item for item in outcome.transcript if item["role"] == "tool")
    tool_payload = json.loads(tool_message["content"])
    assert tool_message["tool_call_id"] == "call_echo"
    assert tool_payload["status"] == "success"
    assert tool_payload["result"] == "ready"
    assert tool_payload["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_executor_repairs_malformed_json_within_error_budget():
    registry = ToolRegistry()
    calls = 0

    async def invoke(_messages, _schemas):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"role": "assistant", "content": '{"tool":"missing",'}
        return {"role": "assistant", "content": "Recovered without executing."}

    outcome = await run_executor_loop(
        [{"role": "user", "content": "Try a tool"}],
        invoke,
        registry=registry,
    )
    assert outcome.status == "completed"
    assert outcome.errors == 1
    assert "malformed" in outcome.transcript[-2]["content"]


@pytest.mark.asyncio
async def test_executor_stops_when_malformed_calls_exhaust_error_budget():
    async def invoke(_messages, _schemas):
        return {"role": "assistant", "content": '{"tool":"missing",'}

    outcome = await run_executor_loop(
        [{"role": "user", "content": "Try a tool"}],
        invoke,
        error_budget=2,
    )
    assert outcome.status == "error_budget"
    assert outcome.errors == 2
    assert outcome.steps == 2
    assert len(outcome.transcript) == 5


@pytest.mark.asyncio
async def test_executor_step_ceiling_surfaces_partial_transcript():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo text.",
            parameters=simple_schema(),
            handler=lambda args: args["value"],
        )
    )
    model_step = 0

    async def invoke(_messages, _schemas):
        nonlocal model_step
        model_step += 1
        return {
            "role": "assistant",
            "content": f"Partial progress {model_step}",
            "tool_calls": [
                {
                    "id": f"call_{model_step}",
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "arguments": '{"value":"continue"}',
                    },
                }
            ],
        }

    outcome = await run_executor_loop(
        [{"role": "user", "content": "Keep going"}],
        invoke,
        registry=registry,
        step_limit=3,
    )
    assert outcome.status == "step_limit"
    assert outcome.steps == 3
    assert outcome.final_answer == "Partial progress 3"
    assert len([item for item in outcome.transcript if item["role"] == "tool"]) == 3
    assert "partial transcript" in outcome.status_message


@pytest.mark.asyncio
async def test_executor_pauses_before_unapproved_mutation():
    registry = ToolRegistry()
    executed = False

    def mutate(_args):
        nonlocal executed
        executed = True
        return "changed"

    registry.register(
        ToolDefinition(
            name="mutate",
            description="Mutate a test value.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=mutate,
            permission="write",
            approval_required=True,
            cancellation="bounded",
        )
    )

    async def invoke(_messages, _schemas):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_mutate",
                    "type": "function",
                    "function": {"name": "mutate", "arguments": "{}"},
                }
            ],
        }

    outcome = await run_executor_loop(
        [{"role": "user", "content": "Change it"}],
        invoke,
        registry=registry,
    )
    assert outcome.status == "approval_required"
    assert outcome.pending_tool_call["name"] == "mutate"
    assert executed is False


def test_first_party_handlers_require_named_async_opt_in(router_factory):
    router, _client, _ = router_factory(tools=True)
    async_names = set()

    for name in router.TOOL_REGISTRY.public_catalog():
        definition = router.TOOL_REGISTRY.get(name)
        assert definition is not None
        handler_is_async = inspect.iscoroutinefunction(definition.handler)
        assert handler_is_async is definition.async_handler
        if handler_is_async:
            async_names.add(name)
            assert name in router.ASYNC_TOOL_HANDLER_ALLOWLIST

    assert async_names == router.ASYNC_TOOL_HANDLER_ALLOWLIST
    assert inspect.iscoroutinefunction(router.tool_web_fetch)
    for handler in (
        router.tool_system_info,
        router.tool_file_read,
        router.tool_file_write,
        router.tool_file_append,
        router.tool_shell_exec,
    ):
        assert not inspect.iscoroutinefunction(handler)


@pytest.mark.asyncio
async def test_tool_execution_termination_values_are_additive():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="echo",
            description="Echo a value.",
            parameters=simple_schema(),
            handler=lambda args: args["value"],
        )
    )

    completed = await run_tool("echo", {"value": "ok"}, registry=registry)
    denied = await run_tool("echo", {}, registry=registry)
    revoked = await run_tool("missing", {}, registry=registry)

    def fail(_args):
        raise RuntimeError("failed safely")

    registry.register(
        ToolDefinition(
            name="fail",
            description="Fail predictably.",
            parameters={"type": "object"},
            handler=fail,
        )
    )
    failed = await run_tool("fail", {}, registry=registry)

    assert completed.termination == "completed"
    assert denied.termination == "denied"
    assert revoked.termination == "denied"
    assert failed.termination == "error"
    assert json.loads(completed.tool_message()["content"])["termination"] == "completed"


class MutableClock:
    def __init__(self):
        self.current = datetime(2026, 9, 2, tzinfo=timezone.utc)

    def __call__(self):
        return self.current

    def advance(self, *, seconds):
        self.current += timedelta(seconds=seconds)


async def start_pending_run(*, store, executed, model_messages=None):
    registry = ToolRegistry()

    def mutate(args):
        executed.append(args["value"])
        return f"changed:{args['value']}"

    registry.register(
        ToolDefinition(
            name="mutate",
            description="Mutate one test value.",
            parameters={
                "type": "object",
                "properties": {
                    "value": {"type": "string", "minLength": 1},
                    "count": {"type": "integer", "minimum": 1},
                },
                "required": ["value", "count"],
                "additionalProperties": False,
            },
            handler=mutate,
            permission="write",
            approval_required=True,
            cancellation="bounded",
        )
    )
    requests = []
    responses = list(
        model_messages
        or [
            {
                "role": "assistant",
                "content": "Preparing exact change.",
                "tool_calls": [
                    {
                        "id": "call_exact",
                        "type": "function",
                        "function": {
                            "name": "mutate",
                            "arguments": '{"value":"approved","count":1}',
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "Continuation complete."},
        ]
    )

    async def invoke(messages, _schemas):
        requests.append(messages)
        return responses.pop(0)

    outcome = await run_executor_loop(
        [{"role": "user", "content": "Change it exactly once"}],
        invoke,
        registry=registry,
        pending_store=store,
    )
    return outcome, requests


@pytest.mark.asyncio
async def test_exact_call_approval_resumes_without_replaying_model_step():
    store = InMemoryPendingCallStore()
    executed = []
    pending, requests = await start_pending_run(store=store, executed=executed)

    assert pending.status == "approval_required"
    assert pending.run_id
    assert pending.pending_tool_call["id"] == "call_exact"
    assert list(pending.pending_tool_call["arguments"]) == ["count", "value"]
    assert len(pending.pending_tool_call["digest"]) == 64
    assert executed == []
    assert len(requests) == 1
    nonce = store._entries[(pending.run_id, "call_exact")].pending_call.nonce
    assert nonce not in store._consumed_nonces
    pending.pending_tool_call["arguments"]["value"] = "tampered"

    resumed = await resume_executor_loop(
        run_id=pending.run_id,
        call_id="call_exact",
        digest=pending.pending_tool_call["digest"],
        decision="approve",
        pending_store=store,
    )

    assert resumed.status == "completed"
    assert resumed.run_id == pending.run_id
    assert resumed.steps == 2
    assert executed == ["approved"]
    assert len(requests) == 2
    assert requests[1][-1]["role"] == "tool"
    tool_payload = json.loads(requests[1][-1]["content"])
    assert tool_payload["status"] == "success"
    assert tool_payload["termination"] == "completed"
    assert nonce in store._consumed_nonces

    replay = await resume_executor_loop(
        run_id=pending.run_id,
        call_id="call_exact",
        digest=pending.pending_tool_call["digest"],
        decision="approve",
        pending_store=store,
    )
    assert replay.status == "approval_replayed"
    assert executed == ["approved"]


@pytest.mark.asyncio
async def test_exact_call_distinguishes_missing_run_and_call_mismatch():
    store = InMemoryPendingCallStore()
    executed = []
    pending, requests = await start_pending_run(store=store, executed=executed)

    missing = await resume_executor_loop(
        run_id="run_missing",
        call_id="call_exact",
        digest=pending.pending_tool_call["digest"],
        decision="approve",
        pending_store=store,
    )
    mismatch = await resume_executor_loop(
        run_id=pending.run_id,
        call_id="call_other",
        digest=pending.pending_tool_call["digest"],
        decision="approve",
        pending_store=store,
    )

    assert missing.status == "approval_not_found"
    assert mismatch.status == "approval_call_mismatch"
    assert executed == []
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_exact_call_rejects_digest_mismatch_without_consuming_approval():
    store = InMemoryPendingCallStore()
    executed = []
    pending, requests = await start_pending_run(store=store, executed=executed)

    mismatch = await resume_executor_loop(
        run_id=pending.run_id,
        call_id="call_exact",
        digest="0" * 64,
        decision="approve",
        pending_store=store,
    )
    assert mismatch.status == "approval_digest_mismatch"
    assert executed == []
    assert len(requests) == 1

    resumed = await resume_executor_loop(
        run_id=pending.run_id,
        call_id="call_exact",
        digest=pending.pending_tool_call["digest"],
        decision="approve",
        pending_store=store,
    )
    assert resumed.status == "completed"
    assert executed == ["approved"]


@pytest.mark.asyncio
async def test_exact_call_rejects_changed_transcript_revision():
    store = InMemoryPendingCallStore()
    executed = []
    pending, _requests = await start_pending_run(store=store, executed=executed)
    entry = store._entries[(pending.run_id, "call_exact")]
    entry.continuation.state.transcript.append(
        {"role": "system", "content": "unexpected mutation"}
    )

    stale = await resume_executor_loop(
        run_id=pending.run_id,
        call_id="call_exact",
        digest=pending.pending_tool_call["digest"],
        decision="approve",
        pending_store=store,
    )
    assert stale.status == "approval_stale"
    assert executed == []


@pytest.mark.asyncio
async def test_exact_call_rejects_expired_ttl():
    clock = MutableClock()
    store = InMemoryPendingCallStore(clock=clock)
    executed = []
    pending, requests = await start_pending_run(store=store, executed=executed)
    assert PENDING_CALL_TTL_SECONDS == 300
    clock.advance(seconds=PENDING_CALL_TTL_SECONDS + 1)

    expired = await resume_executor_loop(
        run_id=pending.run_id,
        call_id="call_exact",
        digest=pending.pending_tool_call["digest"],
        decision="approve",
        pending_store=store,
    )
    assert expired.status == "approval_expired"
    assert executed == []
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_denial_appends_tool_result_and_continues_without_error():
    store = InMemoryPendingCallStore()
    executed = []
    pending, requests = await start_pending_run(store=store, executed=executed)

    resumed = await resume_executor_loop(
        run_id=pending.run_id,
        call_id="call_exact",
        digest=pending.pending_tool_call["digest"],
        decision="deny",
        pending_store=store,
    )

    assert resumed.status == "completed"
    assert resumed.final_answer == "Continuation complete."
    assert resumed.errors == 0
    assert executed == []
    assert len(requests) == 2
    denial = json.loads(requests[1][-1]["content"])
    assert denial["status"] == "denied"
    assert denial["termination"] == "denied"


def test_agent_endpoint_sends_schemas_on_every_model_step(router_factory):
    _, client, _ = router_factory(tools=True)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(
            return_value=httpx.Response(
                200,
                json={"models": [{"name": MODEL_ID, "model": MODEL_ID}]},
            )
        )
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_info",
                                        "type": "function",
                                        "function": {
                                            "name": "system.info",
                                            "arguments": '{"type":"python_version"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "Inventory checked."}}
                    ]
                },
            ),
        ]
        response = client.post(
            "/tools/agent/run",
            headers=AUTH,
            json={
                "messages": [{"role": "user", "content": "Check Python"}],
                "node_id": "node-test",
                "model": MODEL_ID,
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert len(route.calls) == 2
    payloads = [json.loads(call.request.content) for call in route.calls]
    assert payloads[0]["tools"] == payloads[1]["tools"]
    assert any(
        item["function"]["name"] == "system.info"
        for item in payloads[0]["tools"]
    )
    assert payloads[1]["messages"][-1]["role"] == "tool"


def test_agent_endpoint_rejects_unsupported_message_roles(router_factory):
    _, client, _ = router_factory(tools=True)
    response = client.post(
        "/tools/agent/run",
        headers=AUTH,
        json={
            "messages": [{"role": "root", "content": "Bypass"}],
            "node_id": "node-test",
            "model": MODEL_ID,
        },
    )
    assert response.status_code == 422


def test_resume_endpoint_denies_exact_call_and_continues(router_factory, tmp_path):
    _, client, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    target = tmp_path / "must-not-exist.txt"
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(
            return_value=httpx.Response(
                200,
                json={"models": [{"name": MODEL_ID, "model": MODEL_ID}]},
            )
        )
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions")
        route.side_effect = [
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Requesting a write.",
                                "tool_calls": [
                                    {
                                        "id": "call_write",
                                        "type": "function",
                                        "function": {
                                            "name": "file.write",
                                            "arguments": json.dumps(
                                                {
                                                    "path": str(target),
                                                    "content": "blocked",
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "The operator denied the write.",
                            }
                        }
                    ]
                },
            ),
        ]
        pending_response = client.post(
            "/tools/agent/run",
            headers=AUTH,
            json={
                "messages": [{"role": "user", "content": "Write a file"}],
                "node_id": "node-test",
                "model": MODEL_ID,
            },
        )
        pending = pending_response.json()
        resumed_response = client.post(
            "/tools/agent/resume",
            headers=AUTH,
            json={
                "run_id": pending["run_id"],
                "call_id": pending["pending_tool_call"]["id"],
                "digest": pending["pending_tool_call"]["digest"],
                "decision": "deny",
            },
        )

    assert pending_response.status_code == 200
    assert pending["status"] == "approval_required"
    assert resumed_response.status_code == 200
    resumed = resumed_response.json()
    assert resumed["status"] == "completed"
    assert resumed["final_answer"] == "The operator denied the write."
    assert not target.exists()
    denial_message = next(
        item for item in resumed["transcript"] if item["role"] == "tool"
    )
    denial = json.loads(denial_message["content"])
    assert denial["status"] == "denied"
    assert denial["termination"] == "denied"
    assert len(route.calls) == 2


def test_resume_endpoint_preserves_auth_and_tools_default_off(router_factory):
    request = {
        "run_id": "run_missing",
        "call_id": "call_missing",
        "digest": "0" * 64,
        "decision": "approve",
    }
    _, disabled_client, _ = router_factory()
    assert (
        disabled_client.post(
            "/tools/agent/resume",
            headers=AUTH,
            json=request,
        ).status_code
        == 403
    )

    _, enabled_client, _ = router_factory(tools=True)
    assert enabled_client.post("/tools/agent/resume", json=request).status_code == 401
