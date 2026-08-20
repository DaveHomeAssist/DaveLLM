import json
import sqlite3

import httpx
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL


AUTH = {"X-API-Key": TEST_API_KEY}
MODEL_ID = "inventory-model:latest"


def mock_inventory(mock):
    return mock.get(f"{TEST_NODE_URL}/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={"models": [{"name": MODEL_ID, "model": MODEL_ID}]},
        )
    )


def test_health_and_authentication(router_factory):
    _, client, _ = router_factory()
    assert client.get("/health").status_code == 200
    assert client.get("/nodes").status_code == 401
    assert client.get("/nodes", headers={"X-API-Key": "wrong"}).status_code == 401
    response = client.get("/nodes", headers=AUTH)
    assert response.status_code == 200
    assert response.json()[0]["id"] == "node-test"

    _, unconfigured_client, _ = router_factory(api_key=None)
    response = unconfigured_client.get("/nodes")
    assert response.status_code == 503
    assert "DAVE_API_KEY" in response.json()["detail"]


def test_static_root_is_isolated(router_factory):
    _, client, _ = router_factory()
    assert client.get("/").status_code == 200
    assert "DaveLLM" in client.get("/").text
    for path in (
        "/app.py",
        "/.git/config",
        "/dave_projects.json",
        "/dave_conversations.json",
        "/feedback.db",
        "/performance.db",
        "/dave_vectors.db",
        "/cost_log.jsonl",
    ):
        assert client.get(path).status_code == 404, path


def test_inventory_backed_node_and_model_selection(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        inventory = client.get("/nodes/node-test/models", headers=AUTH)
        assert inventory.status_code == 200
        assert inventory.json()["models"] == [{"id": MODEL_ID, "vision": False}]

        node_chat = mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "selected correctly"}}]},
            )
        )
        wrong_node = client.post(
            "/chat",
            headers=AUTH,
            json={"conversation_id": "bad-node", "prompt": "hello", "node_id": "invented", "model": MODEL_ID},
        )
        assert wrong_node.status_code == 404
        wrong_model = client.post(
            "/chat",
            headers=AUTH,
            json={"conversation_id": "bad-model", "prompt": "hello", "node_id": "node-test", "model": "invented"},
        )
        assert wrong_model.status_code == 400

        effective_prompt = "[SUPPORT] Review this\n\n[Attached file: note.txt]\nBody\n"
        response = client.post(
            "/chat",
            headers=AUTH,
            json={"conversation_id": "selected", "prompt": effective_prompt, "node_id": "node-test", "model": MODEL_ID},
        )
        assert response.status_code == 200
        assert response.json()["response"] == "selected correctly"
        payload = json.loads(node_chat.calls.last.request.content)
        assert payload["model"] == MODEL_ID
        assert payload["messages"][-1]["content"] == effective_prompt
        assert router.CONVERSATIONS["selected"]["messages"][0]["content"] == effective_prompt
        assert router.CONVERSATIONS["selected"]["title"] == "[SUPPORT] Review this\n\n[Attach..."


def test_stream_success_and_failure_are_explicit(router_factory):
    _, client, _ = router_factory()
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200

        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions")
        route.mock(
            return_value=httpx.Response(
                200,
                content=(
                    'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
                    'data: {"choices":[{"delta":{"content":" Dave"}}]}\n\n'
                    "[DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        )
        success = client.post(
            "/chat/stream",
            headers=AUTH,
            json={"conversation_id": "stream-ok", "prompt": "go", "node_id": "node-test", "model": MODEL_ID},
        )
        assert success.status_code == 200
        assert '"token": "Hello"' in success.text
        assert '"token": " Dave"' in success.text
        assert '"done": true' in success.text

        route.mock(return_value=httpx.Response(503, text="node offline"))
        failure = client.post(
            "/chat/stream",
            headers=AUTH,
            json={"conversation_id": "stream-fail", "prompt": "go", "node_id": "node-test", "model": MODEL_ID},
        )
        assert failure.status_code == 200
        assert '"error": "Node error 503: node offline"' in failure.text


def test_template_body_and_authenticated_markdown_export(router_factory):
    router, client, _ = router_factory()
    response = client.post(
        "/conversations/from_template",
        headers=AUTH,
        json={"template_name": "code_review", "project_id": None},
    )
    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    expected_prompt = router.TEMPLATES["code_review"]["system_prompt"]
    assert response.json()["system_prompt"] == expected_prompt
    assert router.CONVERSATIONS[conversation_id]["system_prompt"] == expected_prompt
    assert router.CONVERSATIONS[conversation_id]["messages"] == []

    assert client.get(f"/conversations/{conversation_id}/export").status_code == 401
    exported = client.get(f"/conversations/{conversation_id}/export", headers=AUTH)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in exported.headers["content-disposition"]
    assert exported.text.startswith("# Code Review Session")


def test_project_template_sync_chat_uses_one_primary_system_prompt(router_factory):
    router, client, _ = router_factory()
    project_prompt = "Use only the linked project instructions."
    project = client.post(
        "/projects",
        headers=AUTH,
        json={
            "name": "Prompt Contract",
            "system_prompt": project_prompt,
            "preferred_model": MODEL_ID,
        },
    )
    assert project.status_code == 200
    project_id = project.json()["project_id"]
    created = client.post(
        "/conversations/from_template",
        headers=AUTH,
        json={"template_name": "general", "project_id": project_id},
    )
    assert created.status_code == 200
    conversation_id = created.json()["conversation_id"]
    assert created.json()["system_prompt"] == project_prompt
    assert router.CONVERSATIONS[conversation_id]["system_prompt"] == project_prompt
    assert router.CONVERSATIONS[conversation_id]["messages"] == []

    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        node_chat = mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "project answer"}}]},
            )
        )
        response = client.post(
            "/chat",
            headers=AUTH,
            json={
                "conversation_id": conversation_id,
                "prompt": "Use the project",
                "node_id": "node-test",
                "model": MODEL_ID,
                "project_id": project_id,
            },
        )
        assert response.status_code == 200

    payload = json.loads(node_chat.calls.last.request.content)
    assert payload["messages"][0] == {"role": "system", "content": project_prompt}
    assert sum(
        message.get("content") == project_prompt for message in payload["messages"]
    ) == 1


def test_general_template_stream_uses_one_primary_system_prompt(router_factory):
    router, client, _ = router_factory()
    created = client.post(
        "/conversations/from_template",
        headers=AUTH,
        json={"template_name": "general"},
    )
    assert created.status_code == 200
    conversation_id = created.json()["conversation_id"]
    assert router.CONVERSATIONS[conversation_id]["messages"] == []

    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        node_chat = mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=(
                    'data: {"choices":[{"delta":{"content":"general answer"}}]}\n\n'
                    "[DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
            )
        )
        response = client.post(
            "/chat/stream",
            headers=AUTH,
            json={
                "conversation_id": conversation_id,
                "prompt": "Use the general template",
                "node_id": "node-test",
                "model": MODEL_ID,
            },
        )
        assert response.status_code == 200
        assert '"token": "general answer"' in response.text

    payload = json.loads(node_chat.calls.last.request.content)
    expected_prompt = router.SYSTEM_PROMPT
    assert payload["messages"][0] == {"role": "system", "content": expected_prompt}
    assert sum(
        message.get("content") == expected_prompt for message in payload["messages"]
    ) == 1


def test_legacy_instructions_are_canonicalized_and_summary_is_preserved(
    router_factory,
    monkeypatch,
):
    router, client, _ = router_factory()
    legacy_prompt = "Preserve these legacy instructions."
    router.CONVERSATIONS["legacy"] = {
        "title": "Legacy",
        "messages": [
            {"role": "system", "content": legacy_prompt},
            *[
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"old-{index}",
                }
                for index in range(12)
            ],
        ],
        "user_id": "default",
    }
    monkeypatch.setattr(
        router,
        "generate_conversation_summary",
        lambda _messages: "Summary context",
    )

    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        node_chat = mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "legacy answer"}}]},
            )
        )
        response = client.post(
            "/chat",
            headers=AUTH,
            json={
                "conversation_id": "legacy",
                "prompt": "Continue",
                "node_id": "node-test",
                "model": MODEL_ID,
            },
        )
        assert response.status_code == 200

    payload = json.loads(node_chat.calls.last.request.content)
    assert payload["messages"][0] == {"role": "system", "content": legacy_prompt}
    assert sum(
        message.get("content") == legacy_prompt for message in payload["messages"]
    ) == 1
    assert {"role": "system", "content": "Summary context"} in payload["messages"]
    assert router.CONVERSATIONS["legacy"]["messages"][0] == {
        "role": "system",
        "content": legacy_prompt,
    }
    assert router.CONVERSATIONS["legacy"]["system_prompt"] == legacy_prompt


def test_title_and_embeddings_use_raw_history_indexes(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "answer"}}]},
            )
        )

        first = client.post(
            "/chat",
            headers=AUTH,
            json={"conversation_id": "first", "prompt": "First useful title", "node_id": "node-test", "model": MODEL_ID},
        )
        assert first.status_code == 200
        assert router.CONVERSATIONS["first"]["title"] == "First useful title"

        templated = client.post(
            "/conversations/from_template",
            headers=AUTH,
            json={"template_name": "general"},
        ).json()["conversation_id"]
        template_chat = client.post(
            "/chat",
            headers=AUTH,
            json={"conversation_id": templated, "prompt": "Template title", "node_id": "node-test", "model": MODEL_ID},
        )
        assert template_chat.status_code == 200
        assert router.CONVERSATIONS[templated]["title"] == "Template title"

        router.CONVERSATIONS["long"] = {
            "title": "Existing",
            "messages": [
                {"role": "user" if index % 2 == 0 else "assistant", "content": f"old-{index}"}
                for index in range(12)
            ],
            "user_id": "default",
        }
        response = client.post(
            "/chat",
            headers=AUTH,
            json={"conversation_id": "long", "prompt": "new raw message", "node_id": "node-test", "model": MODEL_ID},
        )
        assert response.status_code == 200

    with sqlite3.connect(router.VECTOR_DB) as connection:
        first_indexes = connection.execute(
            "SELECT message_index FROM embeddings WHERE conversation_id = 'first' ORDER BY message_index"
        ).fetchall()
        long_indexes = connection.execute(
            "SELECT message_index FROM embeddings WHERE conversation_id = 'long' ORDER BY message_index"
        ).fetchall()
        template_indexes = connection.execute(
            "SELECT message_index FROM embeddings WHERE conversation_id = ? ORDER BY message_index",
            (templated,),
        ).fetchall()
    assert first_indexes == [(0,), (1,)]
    assert long_indexes == [(12,), (13,)]
    assert template_indexes == [(0,), (1,)]


def test_unreachable_node_reports_reason_and_preserves_inventory(router_factory):
    router, client, _ = router_factory()

    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
    assert router.MODEL_INVENTORY["node-test"] == {MODEL_ID}

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        response = client.get("/nodes/node-test/models", headers=AUTH)

    assert response.status_code == 200
    payload = response.json()
    assert payload["models"] == []
    assert payload["error"] and TEST_NODE_URL in payload["error"]
    # A transient outage must not erase an inventory that was read successfully.
    assert router.MODEL_INVENTORY["node-test"] == {MODEL_ID}


def test_node_without_pulled_models_is_not_reported_as_an_error(router_factory):
    _, client, _ = router_factory()

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        response = client.get("/nodes/node-test/models", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "node_id": "node-test",
        "node_name": "Test Ollama",
        "models": [],
        "error": None,
    }


def test_node_http_error_is_surfaced(router_factory):
    _, client, _ = router_factory()

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(
            return_value=httpx.Response(500, text="boom")
        )
        response = client.get("/nodes/node-test/models", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["models"] == []
    assert "500" in response.json()["error"]


def test_node_status_reports_offline_reason(router_factory):
    _, client, _ = router_factory()

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        statuses = client.get("/nodes/status", headers=AUTH).json()

    assert statuses[0]["status"] == "offline"
    assert "OLLAMA_HOST" in statuses[0]["error"]


def test_vision_detection_requires_token_boundaries(router_factory):
    _, client, _ = router_factory()

    tags = ["gemma3:12b", "llama3.2:3b", "qwen2.5vl:7b", "llava:13b-vision", "mm:latest"]
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(
            return_value=httpx.Response(
                200, json={"models": [{"name": t, "model": t} for t in tags]}
            )
        )
        models = client.get("/nodes/node-test/models", headers=AUTH).json()["models"]

    vision = {m["id"]: m["vision"] for m in models}
    assert vision["gemma3:12b"] is False
    assert vision["llama3.2:3b"] is False
    assert vision["qwen2.5vl:7b"] is True
    assert vision["llava:13b-vision"] is True
    assert vision["mm:latest"] is True


def test_malformed_node_env_registers_no_nodes_loudly(monkeypatch, tmp_path, capsys):
    import importlib
    import sys

    monkeypatch.setenv("DAVE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DAVE_API_KEY", TEST_API_KEY)
    monkeypatch.setenv("DAVE_NODES", "{not json}")

    sys.modules.pop("app", None)
    try:
        router = importlib.import_module("app")
        assert router.NODE_CONFIGS == []
        # The empty inventory has to explain itself rather than look like
        # "Ollama has no models".
        assert "DAVE_NODES" in capsys.readouterr().out
    finally:
        sys.modules.pop("app", None)
