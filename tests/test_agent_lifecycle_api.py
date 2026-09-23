"""Host lifecycle routes use fake Ollama responses; no live model is contacted."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import respx
import pytest

from conftest import TEST_API_KEY, TEST_NODE_URL


AUTH = {"X-API-Key": TEST_API_KEY}
MODEL = "inventory-model:latest"


def inventory(client, mock):
    mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(
        200, json={"models": [{"name": MODEL, "model": MODEL}]},
    ))
    assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200


def new_run(client, **overrides):
    request = {
        "messages": [{"role": "user", "content": "Say done"}],
        "node_id": "node-test", "model": MODEL,
    }
    request.update(overrides)
    return client.post("/tools/agent/runs", headers=AUTH, json=request)


def settled(client, run_id):
    for _ in range(50):
        response = client.get(f"/tools/agent/runs/{run_id}", headers=AUTH)
        if response.json()["status"] not in {"created", "running"}:
            return response.json()
        time.sleep(0.01)
    raise AssertionError("Run did not settle")


def test_lifecycle_auth_tools_off_validation_and_completed_events(router_factory):
    _, disabled, _ = router_factory()
    assert disabled.post("/tools/agent/runs", headers=AUTH, json={}).status_code == 422
    assert disabled.get("/tools/agent/runs/run_missing", headers=AUTH).status_code == 403

    router, client, _ = router_factory(tools=True)
    assert client.get("/tools/agent/runs/run_missing").status_code == 401
    assert client.get("/tools/agent/runs/run_missing", headers=AUTH).status_code == 404
    assert new_run(client).status_code == 409
    assert new_run(client, node_id="missing-node").status_code == 404
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        assert new_run(client, model="absent").status_code == 400
        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "Done."
            }}]}),
        )
        created = new_run(client)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        final = settled(client, run_id)
    assert final["status"] == "completed"
    assert final["snapshot"]["last_content"] == "Done."
    assert len(route.calls) == 1
    events = client.get(f"/tools/agent/runs/{run_id}/events", headers=AUTH).json()["events"]
    assert [event["kind"] for event in events].count("terminal") == 1
    assert client.get(
        f"/tools/agent/runs/{run_id}/events?after={events[-1]['sequence']}", headers=AUTH,
    ).json()["events"] == []
    streamed = client.get(
        f"/tools/agent/runs/{run_id}/events?stream=true&after=0", headers=AUTH,
    )
    assert streamed.status_code == 200
    assert streamed.text.count("event: terminal") == 1
    assert f"id: {events[-1]['sequence']}" in streamed.text
    router_clock = datetime.now(timezone.utc) + timedelta(hours=2)
    router.HARNESS_STORE.clock = lambda: router_clock
    assert client.get(f"/tools/agent/runs/{run_id}", headers=AUTH).status_code == 410


def test_brain_snapshot_survives_approval_and_later_edit(router_factory):
    router, client, _ = router_factory(tools=True)
    project = client.post("/projects", headers=AUTH, json={
        "name": "Run context", "system_prompt": "Project instructions",
    }).json()
    project_id = project["project_id"]
    router.PROJECT_CONTEXT.update_brain(
        project_id, pinned_text="First decision", expected_revision=1,
    )
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions")
        route.side_effect = [
            httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "Need permission",
                "tool_calls": [{"id": "call_one", "type": "function", "function": {
                    "name": "file.write", "arguments": json.dumps({
                        "path": "/outside/not-allowed", "content": "blocked",
                    }),
                }}],
            }}]}),
            httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "Finished",
            }}]}),
        ]
        created = new_run(client, project_id=project_id)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        paused = settled(client, run_id)
        assert paused["status"] == "approval_required"
        assert paused["context"]["brain_revision"] == 2
        router.PROJECT_CONTEXT.update_brain(
            project_id, pinned_text="Second decision", expected_revision=2,
        )
        pending = paused["snapshot"]["pending_call"]
        decision = {key: pending[key] for key in (
            "call_id", "digest", "definition_fingerprint", "permission", "nonce",
        )}
        assert client.post(
            f"/tools/agent/runs/{run_id}/decisions", headers=AUTH,
            json={**decision, "digest": "0" * 64, "decision": "reject"},
        ).status_code == 409
        rejected = client.post(
            f"/tools/agent/runs/{run_id}/decisions", headers=AUTH,
            json={**decision, "decision": "reject"},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "approval_rejected"
        assert client.post(
            f"/tools/agent/runs/{run_id}/decisions", headers=AUTH,
            json={**decision, "decision": "reject"},
        ).status_code == 409
        assert len(route.calls) == 1
        first_payload = json.loads(route.calls[0].request.content)
        assert "First decision" in first_payload["messages"][1]["content"]
        assert "Second decision" not in first_payload["messages"][1]["content"]


def test_approved_resume_uses_frozen_brain_messages(router_factory, tmp_path):
    router, client, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    project_id = client.post("/projects", headers=AUTH, json={
        "name": "Approved run", "system_prompt": "Project instructions",
    }).json()["project_id"]
    router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="Frozen fact", expected_revision=1)
    target = tmp_path / "approved.txt"
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions")
        route.side_effect = [
            httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "Writing",
                "tool_calls": [{"id": "call_approved", "type": "function", "function": {
                    "name": "file.write", "arguments": json.dumps({
                        "path": str(target), "content": "approved",
                    }),
                }}],
            }}]}),
            httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "Complete",
            }}]}),
        ]
        created = new_run(client, project_id=project_id)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        paused = settled(client, run_id)
        assert paused["status"] == "approval_required"
        router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="Changed fact", expected_revision=2)
        router.PROJECT_CONTEXT.compact_brain(project_id)
        pending = paused["snapshot"]["pending_call"]
        decision = {key: pending[key] for key in (
            "call_id", "digest", "definition_fingerprint", "permission", "nonce",
        )}
        approved = client.post(
            f"/tools/agent/runs/{run_id}/decisions", headers=AUTH,
            json={**decision, "decision": "approve"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "completed"
        assert target.read_text() == "approved"
        assert len(route.calls) == 2
        resumed_messages = json.loads(route.calls[1].request.content)["messages"]
        assert "Frozen fact" in resumed_messages[1]["content"]
        assert "Changed fact" not in resumed_messages[1]["content"]


def test_run_captures_conversation_instruction_layers_and_project_attachment(router_factory):
    _, client, _ = router_factory(tools=True)
    project_id = client.post("/projects", headers=AUTH, json={
        "name": "Instructions", "system_prompt": "Project-only rule",
    }).json()["project_id"]
    conversation_id = client.post("/conversations/from_template", headers=AUTH, json={
        "template_name": "code_review", "project_id": project_id,
    }).json()["conversation_id"]
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        assert new_run(client, conversation_id=conversation_id).status_code == 409
        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "Done",
            }}]}),
        )
        started = new_run(client, project_id=project_id, conversation_id=conversation_id)
        assert started.status_code == 200, started.text
        assert settled(client, started.json()["run_id"])["status"] == "completed"
        system = json.loads(route.calls[0].request.content)["messages"][0]["content"]
        assert "Project-only rule" in system
        assert "Focus on code quality" in system


def test_shell_opt_in_keeps_legacy_route_and_uses_cancellable_lifecycle_runner(router_factory, tmp_path):
    router, client, _ = router_factory(tools=True, shell=True, tool_roots=[str(tmp_path)])
    assert router.TOOL_REGISTRY.get("shell.exec").context_handler is False
    assert router.HARNESS_REGISTRY.get("shell.exec").context_handler is True
    legacy = client.post("/tools/execute", headers=AUTH, json={
        "tool": "shell.exec", "params": {"command": "pwd"},
    })
    assert legacy.status_code == 200
    assert legacy.json()["result"].strip() == str(tmp_path)
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions")
        route.side_effect = [
            httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "Checking root",
                "tool_calls": [{"id": "call_pwd", "type": "function", "function": {
                    "name": "shell.exec", "arguments": json.dumps({"command": "pwd"}),
                }}],
            }}]}),
            httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "Complete",
            }}]}),
        ]
        created = new_run(client)
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        pending = settled(client, run_id)["snapshot"]["pending_call"]
        decision = {key: pending[key] for key in (
            "call_id", "digest", "definition_fingerprint", "permission", "nonce",
        )}
        approved = client.post(
            f"/tools/agent/runs/{run_id}/decisions", headers=AUTH,
            json={**decision, "decision": "approve"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "completed"
        assert len(route.calls) == 2


def test_shell_context_handler_still_requires_separate_opt_in(router_factory):
    router, _, _ = router_factory(tools=True, shell=False)
    result = router.tool_shell_exec({"command": "pwd"}, context=object())
    assert result.status == "error"
    assert "DAVE_ENABLE_SHELL_TOOL" in result.error


@pytest.mark.asyncio
async def test_concurrent_runs_keep_separate_model_bindings(router_factory):
    router, _client, _ = router_factory(tools=True)
    router.MODEL_INVENTORY["node-test"] = {"first:latest", "second:latest"}
    observed = []

    async def fake_model(_messages, _schemas):
        binding = router.HOST_RUN_CONTEXT.get()
        await asyncio.sleep(0.02)
        observed.append(binding.model)
        return {"choices": [{"message": {"role": "assistant", "content": binding.model}}]}

    router.HARNESS.invoke_model = fake_model
    requests = [router.LifecycleRunRequest(
        messages=[{"role": "user", "content": "Model test"}],
        node_id="node-test", model=model,
    ) for model in ("first:latest", "second:latest")]
    results = await asyncio.gather(*(
        router.create_agent_run(request, user_id="default") for request in requests
    ))
    await asyncio.gather(*tuple(router.HOST_RUN_TASKS))
    assert sorted(observed) == ["first:latest", "second:latest"]
    assert sorted(router.HARNESS.snapshot(item["run_id"]).snapshot.last_content for item in results) == sorted(observed)


@pytest.mark.asyncio
async def test_run_admission_preserves_active_ledger_at_capacity(router_factory):
    router, _, _ = router_factory(tools=True)
    router.MODEL_INVENTORY["node-test"] = {MODEL}
    router.HARNESS_STORE.max_runs = 4
    release = asyncio.Event()
    entered = asyncio.Event()

    async def fake_model(messages, _schemas):
        if messages[-1]["content"] == "hold":
            entered.set()
            await release.wait()
        return {"choices": [{"message": {"role": "assistant", "content": "Done"}}]}

    router.HARNESS.invoke_model = fake_model

    def request(content):
        return router.LifecycleRunRequest(
            messages=[{"role": "user", "content": content}],
            node_id="node-test", model=MODEL,
        )

    held = await router.create_agent_run(request("hold"), user_id="default")
    await entered.wait()
    for index in range(3):
        short = await router.create_agent_run(request(f"short {index}"), user_id="default")
        for _ in range(50):
            if router.HARNESS.snapshot(short["run_id"]).snapshot.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Short run did not finish")
    with pytest.raises(router.HTTPException) as full:
        await router.create_agent_run(request("overflow"), user_id="default")
    assert full.value.status_code == 503
    assert router.HARNESS.snapshot(held["run_id"]).snapshot.status == "running"
    release.set()
    await asyncio.gather(*tuple(router.HOST_RUN_TASKS))
    assert router.HARNESS.snapshot(held["run_id"]).snapshot.status == "completed"


@pytest.mark.asyncio
async def test_run_admission_caps_parallel_model_work(router_factory):
    router, _, _ = router_factory(tools=True)
    router.MODEL_INVENTORY["node-test"] = {MODEL}
    release = asyncio.Event()
    entered = 0

    async def fake_model(_messages, _schemas):
        nonlocal entered
        entered += 1
        await release.wait()
        return {"choices": [{"message": {"role": "assistant", "content": "Done"}}]}

    router.HARNESS.invoke_model = fake_model
    request = router.LifecycleRunRequest(
        messages=[{"role": "user", "content": "hold"}],
        node_id="node-test", model=MODEL,
    )
    for _ in range(router.MAX_ACTIVE_HARNESS_RUNS):
        await router.create_agent_run(request, user_id="default")
    for _ in range(50):
        if entered == router.MAX_ACTIVE_HARNESS_RUNS:
            break
        await asyncio.sleep(0.01)
    assert entered == router.MAX_ACTIVE_HARNESS_RUNS
    with pytest.raises(router.HTTPException) as full:
        await router.create_agent_run(request, user_id="default")
    assert full.value.status_code == 429
    release.set()
    await asyncio.gather(*tuple(router.HOST_RUN_TASKS))


@pytest.mark.asyncio
async def test_model_response_byte_limit_stops_chunk_consumption(router_factory):
    router, _, _ = router_factory(tools=True)
    chunks_read = []

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(3):
                chunks_read.append(index)
                yield b"x" * 600_000

    binding = router.HostRunBinding(
        "default", TEST_NODE_URL, MODEL, 2048, 0.7, None, None, None, {},
    )
    token = router.HOST_RUN_CONTEXT.set(binding)
    try:
        with respx.mock(assert_all_called=True) as mock:
            mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
                return_value=httpx.Response(200, stream=OversizedStream()),
            )
            with pytest.raises(ValueError, match="byte limit"):
                await router.invoke_harness_model([{"role": "user", "content": "hi"}], [])
    finally:
        router.HOST_RUN_CONTEXT.reset(token)
    assert chunks_read == [0, 1]
