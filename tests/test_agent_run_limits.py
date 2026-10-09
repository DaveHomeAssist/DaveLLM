"""Opt-in lifecycle limits, exercised with fake providers only."""

import asyncio
import json
import time
from datetime import datetime

import httpx
import pytest
import respx

from conftest import TEST_NODE_URL
from test_agent_lifecycle_api import AUTH, MODEL, inventory, new_run


def settled(client, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/tools/agent/runs/{run_id}", headers=AUTH).json()
        if result["status"] not in {"created", "running"}:
            return result
        time.sleep(0.02)
    raise AssertionError(f"Run did not settle: {result['status']}")


async def run_and_settle(router, **overrides):
    # Keep creation, background work and polling on one event loop. A synchronous
    # TestClient without a lifespan context closes its portal after each request.
    router.MODEL_INVENTORY["node-test"] = {MODEL}
    request = {"messages": [{"role": "user", "content": "Say done"}],
               "node_id": "node-test", "model": MODEL, **overrides}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=router.app),
                                base_url="http://router.test", headers=AUTH) as client:
        created = await client.post("/tools/agent/runs", json=request)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        await asyncio.wait_for(asyncio.gather(*tuple(router.HOST_RUN_TASKS)), timeout=5)
        final = (await client.get(f"/tools/agent/runs/{run_id}")).json()
        assert final["status"] not in {"created", "running"}
        return final


@pytest.mark.parametrize("field,value", [
    ("max_input_tokens", 0), ("max_input_tokens", 262145),
    ("max_input_tokens", True), ("max_input_tokens", 1.5),
    ("total_wall_seconds", 0), ("total_wall_seconds", 301),
    ("total_wall_seconds", True), ("total_wall_seconds", "nan"),
])
def test_invalid_limit_never_creates_run(router_factory, field, value):
    router, client, _ = router_factory(tools=True)
    assert new_run(client, **{field: value}).status_code == 422
    assert router.HARNESS_STORE.run_count == 0


def test_initial_limit_rejects_without_provider_call(router_factory):
    router, client, _ = router_factory(tools=True)
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        assert new_run(client, max_input_tokens=1).status_code == 413
    assert router.HARNESS_STORE.run_count == 0


def test_default_deadline_usage_and_selected_schema_estimate(router_factory):
    router, client, _ = router_factory(tools=True)
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "done"},
                       "prompt_eval_count": 42, "eval_count": 7, "done": True},
        ))
        created = new_run(client, selected_tools=["system.info"], max_tokens=1024)
        final = settled(client, created.json()["run_id"])
    assert final["status"] == "completed"
    assert final["limits"] == {"max_input_tokens": None, "total_wall_seconds": 300.0}
    payload = json.loads(route.calls.last.request.content)
    assert payload["options"]["num_predict"] == 1024
    assert len(route.calls) == 1
    usage = final["model_usage"][0]
    expected = max(1, len(json.dumps(payload["tools"], ensure_ascii=False, separators=(",", ":"))) // 3)
    assert usage["estimated_input_tokens"] == expected + router.tool_run_message_tokens(payload["messages"])
    assert usage["request_sent"] is True and usage["status"] == "completed"
    assert usage["prompt_tokens"] == 42 and usage["generated_tokens"] == 7
    snapshot = final["snapshot"]
    assert 299 <= (datetime.fromisoformat(snapshot["deadline_at"]) - datetime.fromisoformat(snapshot["created_at"])).total_seconds() <= 300


@pytest.mark.asyncio
async def test_growth_after_tool_result_is_refused_before_second_request(router_factory, tmp_path):
    (tmp_path / "large.txt").write_text("x" * 5000)
    router, _, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    messages = [{"role": "user", "content": "Read large.txt then report"}]
    schemas = [s for s in router.HARNESS_REGISTRY.model_schemas() if s["function"]["name"] == "file.read"]
    limit = max(1, len(json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))) // 3) + router.tool_run_message_tokens(messages) + 100
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "read_once", "function": {"name": "file.read", "arguments": {"path": str(tmp_path / "large.txt")}}},
            ]}, "done": True},
        ))
        final = await run_and_settle(router, messages=messages, selected_tools=["file.read"],
                                     max_input_tokens=limit)
    assert len(route.calls) == 1
    assert final["status"] == "model_error"
    assert final["terminal_explanation"]["reason_code"] == "input_limit"
    assert final["terminal_explanation"]["source"] == "harness"
    assert final["model_usage"][-1]["request_sent"] is False
    assert final["model_usage"][-1]["estimated_input_tokens"] > limit
    assert final["model_usage"][0]["generated_tokens"] is None
    assert final["snapshot"]["last_content"] != final["terminal_explanation"]["message"]


@pytest.mark.asyncio
async def test_short_deadline_cancels_owned_provider_and_stops_run(router_factory):
    router, _, _ = router_factory(tools=True)
    cancelled = []
    received = []

    async def slow_provider(request):
        received.append(request)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(200, json={"message": {"content": "too late"}})

    # RESPX records a route call after its callback returns; cancellation cannot
    # return a response, so count entry to the fake provider instead.
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(side_effect=slow_provider)
        started = time.monotonic()
        final = await run_and_settle(router, total_wall_seconds=1.0, selected_tools=["system.info"])
    assert time.monotonic() - started < 3
    assert final["status"] == "run_expired" and final["reason_code"] == "run_expired"
    assert final["limits"]["total_wall_seconds"] == 1.0
    assert cancelled == [True] and len(received) == 1
    assert final["model_usage"][0]["status"] == "cancelled"
    assert final["snapshot"]["model_inflight"] is False


@pytest.mark.asyncio
async def test_step_limit_still_caps_provider_requests(router_factory):
    router, _, _ = router_factory(tools=True)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "sys_once", "function": {"name": "system.info", "arguments": {}}},
            ]}, "eval_count": 1, "done": True},
        ))
        final = await run_and_settle(router, step_limit=1, max_input_tokens=4000,
                                     total_wall_seconds=180, selected_tools=["system.info"],
                                     max_tokens=1024)
    assert len(route.calls) == 1 and final["status"] == "step_limit"
    assert final["limits"] == {"max_input_tokens": 4000, "total_wall_seconds": 180.0}
