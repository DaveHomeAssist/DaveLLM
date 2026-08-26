import asyncio
import json
import logging
import time

import httpx
import pytest
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL
from tool_executor import (
    ToolDefinition,
    ToolRegistry,
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

    async def remember(args):
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

    async def stall(_args):
        await asyncio.sleep(0.05)
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

    async def mutate(_args):
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
