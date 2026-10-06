"""DL-EVAL-01: `POST /eval/chat` is a stateless completion for evaluation harnesses."""

import hashlib
import json

import httpx
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL, ollama_chat_json

AUTH = {"X-API-Key": TEST_API_KEY}
MODEL_ID = "inventory-model:latest"
SYSTEM = "Return only JSON with an `enhanced` field."


def mock_inventory(mock):
    return mock.get(f"{TEST_NODE_URL}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": MODEL_ID, "model": MODEL_ID}]})
    )


def eval_body(**overrides):
    body = {
        "node_id": "node-test",
        "model": MODEL_ID,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "make this better"}],
        "max_tokens": 4096,
        "temperature": 0.4,
    }
    body.update(overrides)
    return body


def snapshot(data_dir):
    if not data_dir.exists():
        return {}
    return {
        str(path.relative_to(data_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(data_dir.rglob("*"))
        if path.is_file()
    }


def loaded(router_factory):
    router, client, data_dir = router_factory()
    return router, client, data_dir


def test_eval_chat_sends_caller_messages_verbatim_and_persists_nothing(router_factory):
    router, client, data_dir = loaded(router_factory)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        before = snapshot(data_dir)
        conversations_before = dict(router.CONVERSATIONS)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_json(
                '{"enhanced": "better"}', eval_count=12, eval_duration=2_000_000_000,
                prompt_eval_count=40, prompt_eval_duration=1_000_000_000, total_duration=3_000_000_000,
            )
        )
        response = client.post("/eval/chat", headers=AUTH, json=eval_body())

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == '{"enhanced": "better"}'
    assert body["node"] == "Test Ollama"
    assert body["node_id"] == "node-test"
    assert body["model"] == MODEL_ID
    assert body["done_reason"] == "stop"
    assert body["stats"]["gen_tokens"] == 12
    assert body["stats"]["gen_tps"] == 6.0
    assert isinstance(body["latency_ms"], float)
    assert "reason" not in body

    payload = json.loads(route.calls.last.request.content)
    # No DaveLLM persona or history: the node sees exactly the caller's messages.
    assert payload["messages"] == eval_body()["messages"]
    assert payload["options"]["num_predict"] == 4096
    assert payload["options"]["temperature"] == 0.4
    assert payload["stream"] is False
    assert "format" not in payload
    assert "keep_alive" not in payload

    assert snapshot(data_dir) == before
    assert router.CONVERSATIONS == conversations_before
    assert router.MODEL_HEALTH == {}


def test_eval_chat_passes_json_format_through(router_factory):
    _, client, _ = loaded(router_factory)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("{}"))
        response = client.post("/eval/chat", headers=AUTH, json=eval_body(format="json"))

    assert response.status_code == 200
    assert json.loads(route.calls.last.request.content)["format"] == "json"


def test_eval_chat_rejects_unknown_format_and_bad_messages(router_factory):
    _, client, _ = loaded(router_factory)
    assert client.post("/eval/chat", headers=AUTH, json=eval_body(format="yaml")).status_code == 422
    assert client.post("/eval/chat", headers=AUTH, json=eval_body(messages=[])).status_code == 422
    bad_role = eval_body(messages=[{"role": "tool", "content": "x"}])
    assert client.post("/eval/chat", headers=AUTH, json=bad_role).status_code == 422


def test_eval_chat_reports_tool_call_only_replies(router_factory):
    _, client, _ = loaded(router_factory)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": "m",
                    "created_at": "t",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"function": {"name": "browser.run", "arguments": {}}}],
                    },
                    "done": True,
                    "done_reason": "stop",
                },
            )
        )
        response = client.post("/eval/chat", headers=AUTH, json=eval_body())

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == ""
    assert body["reason"] == "tool_call_only"
    assert body["tools"] == ["browser.run"]
    assert body["notice"]


def test_eval_chat_inventory_and_node_errors(router_factory):
    _, client, _ = loaded(router_factory)
    assert client.post("/eval/chat", headers=AUTH, json=eval_body(node_id="missing")).status_code == 404
    assert client.post("/eval/chat", headers=AUTH, json=eval_body()).status_code == 409
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        assert client.post("/eval/chat", headers=AUTH, json=eval_body(model="other:latest")).status_code == 400
        route = mock.post(f"{TEST_NODE_URL}/api/chat")
        route.mock(side_effect=httpx.ConnectError("down"))
        assert client.post("/eval/chat", headers=AUTH, json=eval_body()).status_code == 503
        route.mock(side_effect=httpx.ReadTimeout("slow"))
        assert client.post("/eval/chat", headers=AUTH, json=eval_body()).status_code == 504
        route.mock(return_value=httpx.Response(500, text="boom"))
        assert client.post("/eval/chat", headers=AUTH, json=eval_body()).status_code == 502
        for failure in (httpx.RemoteProtocolError("dropped"), httpx.ReadError("reset")):
            route.mock(side_effect=failure)
            response = client.post("/eval/chat", headers=AUTH, json=eval_body())
            assert response.status_code == 502
            assert type(failure).__name__ in response.json()["detail"]


def test_eval_chat_node_failures_do_not_touch_model_health(router_factory):
    router, client, _ = loaded(router_factory)
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(side_effect=httpx.ConnectError("down"))
        client.post("/eval/chat", headers=AUTH, json=eval_body())
    assert router.MODEL_HEALTH == {}


def test_eval_chat_requires_api_key(router_factory):
    _, client, _ = loaded(router_factory)
    assert client.post("/eval/chat", json=eval_body()).status_code == 401
    assert client.post("/eval/chat", headers={"X-API-Key": "wrong"}, json=eval_body()).status_code == 401


def test_eval_chat_counts_on_node_activity(router_factory):
    router, client, _ = loaded(router_factory)
    seen = []

    def reply(request):
        seen.append(router.NODE_ACTIVITY.in_flight(TEST_NODE_URL))
        return ollama_chat_json("ok")

    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(side_effect=reply)
        assert client.post("/eval/chat", headers=AUTH, json=eval_body()).status_code == 200

    assert seen == [1]
    assert router.NODE_ACTIVITY.in_flight(TEST_NODE_URL) == 0
