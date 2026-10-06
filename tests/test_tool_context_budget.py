"""DL-15: tool runs budget for the tool descriptions every step sends to the model."""

import json

import httpx
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL

AUTH = {"X-API-Key": TEST_API_KEY}
MODEL = "inventory-model:latest"


def inventory(client, mock):
    mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(
        200, json={"models": [{"name": MODEL, "model": MODEL}]},
    ))
    assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200


def body(**overrides):
    request = {"messages": [{"role": "user", "content": "Say done"}], "node_id": "node-test", "model": MODEL}
    request.update(overrides)
    return request


def test_schema_tokens_follow_the_registered_tools(router_factory, monkeypatch):
    router, _, _ = router_factory(tools=True)
    base = router.tool_schema_tokens(router.HARNESS_REGISTRY)
    text = json.dumps(router.HARNESS_REGISTRY.model_schemas(), ensure_ascii=False, separators=(",", ":"))
    assert base == len(text) // 3
    monkeypatch.setenv("DAVE_ENABLE_TOOLPACK", "true")
    monkeypatch.setenv("DAVE_TOOLPACK_CONFIG", json.dumps({"enabled_tools": ["calc.eval", "time.convert"]}))
    router, _, _ = router_factory(tools=True)
    assert router.tool_schema_tokens(router.HARNESS_REGISTRY) > base


def test_fit_error_names_every_part_of_the_budget(router_factory):
    router, _, _ = router_factory(tools=True)
    window = router.chat_num_ctx(MODEL)
    messages = [{"role": "user", "content": "x" * 400}]
    assert router.tool_run_fit_error(MODEL, messages, 1_000, 2_048) is None
    error = router.tool_run_fit_error(MODEL, messages, window, 2_048)
    assert f"{window}-token" in error and f"about {window} tokens of tool descriptions" in error
    assert "100 of messages" in error and "2048 reserved for the reply" in error
    assert "enabled_tools" in error


def test_tool_calls_count_toward_the_message_budget(router_factory):
    router, _, _ = router_factory(tools=True)
    window = router.chat_num_ctx(MODEL)
    call = {"id": "call-1", "function": {"name": "calc.eval", "arguments": {"expression": "1+" * (window * 2)}}}
    messages = [{"role": "user", "content": "x" * 400}, {"role": "assistant", "content": "", "tool_calls": [call]}]
    fields = json.dumps({"tool_calls": [call]}, ensure_ascii=False, separators=(",", ":"))
    assert router.tool_run_message_tokens(messages) == 100 + 1 + len(fields) // 3
    assert router.tool_run_message_tokens(messages[:1]) == 100
    error = router.tool_run_fit_error(MODEL, messages, 1_000, 2_048)
    assert error and f"{100 + 1 + len(fields) // 3} of messages" in error


def test_reply_reserve_must_be_positive(router_factory):
    _, client, _ = router_factory(tools=True)
    with respx.mock(assert_all_called=False) as mock:
        inventory(client, mock)
        chat = mock.post(f"{TEST_NODE_URL}/api/chat")
        for endpoint in ("/tools/agent/run", "/tools/agent/runs"):
            for max_tokens in (0, -1_000_000):
                response = client.post(endpoint, headers=AUTH, json=body(max_tokens=max_tokens))
                assert response.status_code == 422, (endpoint, max_tokens, response.text)
        assert not chat.calls


def test_oversized_runs_are_refused_before_any_model_call(router_factory):
    router, client, _ = router_factory(tools=True)
    window = router.chat_num_ctx(MODEL)
    with respx.mock(assert_all_called=False) as mock:
        inventory(client, mock)
        chat = mock.post(f"{TEST_NODE_URL}/api/chat")
        call = {"id": "call-1", "function": {"name": "calc.eval", "arguments": {"expression": "1+" * (window * 2)}}}
        transcript = [{"role": "user", "content": "Add"}, {"role": "assistant", "content": "", "tool_calls": [call]}]
        for endpoint in ("/tools/agent/runs", "/tools/agent/run"):
            for request in (body(max_tokens=window), body(messages=transcript)):
                response = client.post(endpoint, headers=AUTH, json=request)
                assert response.status_code == 413, (endpoint, response.text)
                assert "tool descriptions" in response.json()["detail"]
        assert not chat.calls
    assert not router.HOST_RUN_BINDINGS


def test_runs_that_fit_still_start(router_factory):
    _, client, _ = router_factory(tools=True)
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "Done."}, "done": True},
        ))
        assert client.post("/tools/agent/run", headers=AUTH, json=body()).status_code == 200
        assert client.post("/tools/agent/runs", headers=AUTH, json=body()).status_code == 200


def test_project_runs_reserve_room_for_tool_descriptions(router_factory, monkeypatch):
    router, client, _ = router_factory(tools=True)
    project_id = client.post("/projects", headers=AUTH, json={"name": "Budget"}).json()["project_id"]
    seen = {}
    build = router.build_project_messages_for_node

    def spy(**kwargs):
        seen.update(kwargs)
        return build(**kwargs)

    monkeypatch.setattr(router, "build_project_messages_for_node", spy)
    with respx.mock(assert_all_called=False) as mock:
        inventory(client, mock)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "Done."}, "done": True},
        ))
        response = client.post("/tools/agent/runs", headers=AUTH, json=body(project_id=project_id, max_tokens=1_024))
    assert response.status_code == 200, response.text
    assert seen["output_reserve"] == 1_024 + router.tool_schema_tokens(router.HARNESS_REGISTRY)
