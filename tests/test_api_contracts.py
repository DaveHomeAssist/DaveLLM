import json
import sqlite3
from pathlib import Path

import httpx
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL, ollama_chat_json, ollama_chat_ndjson


AUTH = {"X-API-Key": TEST_API_KEY}
MODEL_ID = "inventory-model:latest"
PRODUCT_VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()


def mock_inventory(mock):
    return mock.get(f"{TEST_NODE_URL}/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={"models": [{"name": MODEL_ID, "model": MODEL_ID}]},
        )
    )


def test_health_and_authentication(router_factory):
    router, client, _ = router_factory()
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["version"] == router.PRODUCT_VERSION == PRODUCT_VERSION
    assert router.app.version == router.PRODUCT_VERSION
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
        "/dave_settings.json",
        "/dave_conversations.json",
        "/feedback.db",
        "/performance.db",
        "/dave_vectors.db",
        "/dave_project_context.db",
        "/project_uploads/example.txt",
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

        node_chat = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("selected correctly"))
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
        assert payload["stream"] is False
        assert "max_tokens" not in payload
        assert payload["messages"][-1]["content"] == effective_prompt
        assert router.CONVERSATIONS["selected"]["messages"][0]["content"] == effective_prompt
        assert router.CONVERSATIONS["selected"]["title"] == "[SUPPORT] Review this\n\n[Attach..."


def test_stream_success_and_failure_are_explicit(router_factory):
    _, client, _ = router_factory()
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200

        route = mock.post(f"{TEST_NODE_URL}/api/chat")
        route.mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "Hello"}, "done": False},
                {"message": {"role": "assistant", "content": " Dave"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "eval_count": 2},
            )
        )
        success = client.post(
            "/chat/stream",
            headers=AUTH,
            json={"conversation_id": "stream-ok", "prompt": "go", "node_id": "node-test", "model": MODEL_ID},
        )
        assert success.status_code == 200
        # Exact SSE framing: every token event carries "done": false, the terminal event closes.
        assert 'data: {"token": "Hello", "done": false}\n\n' in success.text
        assert 'data: {"token": " Dave", "done": false}\n\n' in success.text
        assert success.text.rstrip().endswith('data: {"token": "", "done": true, "message_count": 2}')
        assert success.text.count('"done": true') == 1

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
    expected_prompt = (
        f"{router.SYSTEM_PROMPT}\n\nSESSION OVERRIDE\n"
        f"{router.TEMPLATES['code_review']['session_override']}"
    )
    assert response.json()["system_prompt"] == expected_prompt
    assert router.CONVERSATIONS[conversation_id]["system_prompt"] == expected_prompt
    assert router.CONVERSATIONS[conversation_id]["messages"] == []
    # Templates carry no source-controlled model path; a General chat has no
    # preferred model and a project chat reports only the project's setting.
    assert response.json()["preferred_model"] is None
    assert all("preferred_model" not in template for template in router.TEMPLATES.values())
    project = client.post(
        "/projects",
        headers=AUTH,
        json={"name": "Preferred", "preferred_model": MODEL_ID},
    ).json()
    project_chat = client.post(
        "/conversations/from_template",
        headers=AUTH,
        json={"template_name": "brainstorm", "project_id": project["project_id"]},
    )
    assert project_chat.status_code == 200
    assert project_chat.json()["preferred_model"] == MODEL_ID

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
    expected_prompt = (
        f"{router.SYSTEM_PROMPT}\n\nPROJECT INSTRUCTIONS\n{project_prompt}"
    )
    assert created.json()["system_prompt"] == expected_prompt
    assert router.CONVERSATIONS[conversation_id]["system_prompt"] == expected_prompt
    assert router.CONVERSATIONS[conversation_id]["messages"] == []

    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        node_chat = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("project answer"))
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
    assert payload["messages"][0] == {"role": "system", "content": expected_prompt}
    assert sum(
        message.get("content") == expected_prompt for message in payload["messages"]
    ) == 1


def test_instruction_layers_save_apply_to_next_message_and_revert(router_factory):
    router, client, data_dir = router_factory()
    project = client.post(
        "/projects",
        headers=AUTH,
        json={"name": "Layered", "system_prompt": "Project initial"},
    ).json()
    conversation_id = client.post(
        "/conversations/from_template",
        headers=AUTH,
        json={"template_name": "general", "project_id": project["project_id"]},
    ).json()["conversation_id"]

    initial = client.get(
        f"/conversations/{conversation_id}/instructions",
        headers=AUTH,
    ).json()
    assert initial["precedence"] == [
        "global_default",
        "project_instructions",
        "session_override",
    ]
    assert initial["layers"]["global_default"]["content"] == router.SYSTEM_PROMPT
    assert initial["layers"]["project_instructions"]["content"] == "Project initial"
    assert initial["layers"]["session_override"]["content"] == ""

    updated = client.put(
        f"/conversations/{conversation_id}/instructions",
        headers=AUTH,
        json={
            "global_default": "Global edited",
            "project_instructions": "Project edited",
            "session_override": "Session edited",
        },
    )
    assert updated.status_code == 200
    effective = (
        "Global edited\n\n"
        "PROJECT INSTRUCTIONS\nProject edited\n\n"
        "SESSION OVERRIDE\nSession edited"
    )
    assert updated.json()["effective"] == effective
    assert updated.json()["character_count"] == len(effective)
    assert (data_dir / "dave_settings.json").exists()

    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        node_chat = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("layered answer"))
        response = client.post(
            "/chat",
            headers=AUTH,
            json={
                "conversation_id": conversation_id,
                "prompt": "Use current instructions",
                "node_id": "node-test",
                "model": MODEL_ID,
            },
        )
        assert response.status_code == 200
    assert json.loads(node_chat.calls.last.request.content)["messages"][0] == {
        "role": "system",
        "content": effective,
    }

    reverted = client.delete(
        f"/conversations/{conversation_id}/instructions/session",
        headers=AUTH,
    ).json()
    assert reverted["layers"]["session_override"]["content"] == ""
    assert reverted["effective"] == (
        "Global edited\n\nPROJECT INSTRUCTIONS\nProject edited"
    )
    reset_global = client.delete("/instructions/global", headers=AUTH).json()
    assert reset_global["content"] == router.SYSTEM_PROMPT
    assert reset_global["is_source_default"] is True


def test_project_notepad_is_plain_project_scoped_and_persistent(router_factory):
    _, client, _ = router_factory()
    first = client.post(
        "/projects",
        headers=AUTH,
        json={"name": "First"},
    ).json()
    second = client.post(
        "/projects",
        headers=AUTH,
        json={"name": "Second"},
    ).json()

    saved = client.put(
        f"/projects/{first['project_id']}/notepad",
        headers=AUTH,
        json={"content": "one\ntwo"},
    )
    assert saved.status_code == 200
    assert saved.json()["character_count"] == 7
    assert client.get(
        f"/projects/{first['project_id']}/notepad",
        headers=AUTH,
    ).json()["content"] == "one\ntwo"
    assert client.get(
        f"/projects/{second['project_id']}/notepad",
        headers=AUTH,
    ).json()["content"] == ""
    listed = {
        item["project_id"]: item["notepad"]
        for item in client.get("/projects", headers=AUTH).json()["projects"]
    }
    assert listed[first["project_id"]] == "one\ntwo"
    assert listed[second["project_id"]] == ""


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
        node_chat = mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "general answer"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
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
        node_chat = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("legacy answer"))
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


def test_attached_legacy_snapshot_remains_exact_until_saved_or_reverted(
    router_factory,
):
    router, client, _ = router_factory()
    project = client.post(
        "/projects",
        headers=AUTH,
        json={"name": "Legacy Project", "system_prompt": "Current project layer"},
    ).json()
    legacy_prompt = "Legacy project-only snapshot"
    router.CONVERSATIONS["legacy-project"] = {
        "title": "Legacy Project Conversation",
        "messages": [],
        "user_id": "default",
        "project_id": project["project_id"],
        "system_prompt": legacy_prompt,
    }

    response = client.get(
        "/conversations/legacy-project/instructions",
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["mode"] == "replace"
    assert response.json()["layers"]["session_override"]["content"] == legacy_prompt
    assert response.json()["effective"] == legacy_prompt


def test_title_and_embeddings_use_raw_history_indexes(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        node_chat = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("answer"))

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
        # "long" crosses max_turns=10, so the real generate_conversation_summary shares this route
        # and must carry its own fixed sampling rather than the chat request's.
        summary_payloads = [
            payload
            for payload in (json.loads(call.request.content) for call in node_chat.calls)
            if payload["messages"][0]["content"] == "You summarize prior chat turns concisely."
        ]
        assert len(summary_payloads) == 1
        assert summary_payloads[0]["options"] == {"top_p": 1.0, "num_predict": 150, "temperature": 0.3}
        assert summary_payloads[0]["stream"] is False

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

    tags = [
        "gemma3:12b",
        "llama3.2:3b",
        "qwen2.5vl:7b",
        "llava:13b-vision",
        "mm:latest",
        "internvl2:latest",
        "deepseek-vl2:latest",
    ]
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
    # A "vl" marker carrying its own version digits is still vision-language.
    assert vision["internvl2:latest"] is True
    assert vision["deepseek-vl2:latest"] is True


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


# --- DL-TRANSPORT-01a: native /api/chat parity for the plain chat paths ---


OPENAI_ONLY_KEYS = ("max_tokens", "temperature", "keep_alive", "think", "tools", "images")
# The node timeouts the transport migration must not change (DL-TRANSPORT-01a); httpx records
# the value each call site passes as request.extensions["timeout"].
CHAT_NODE_TIMEOUT = httpx.Timeout(120).as_dict()
SUMMARY_NODE_TIMEOUT = httpx.Timeout(10).as_dict()


def chat_body(conversation_id, **overrides):
    body = {"conversation_id": conversation_id, "prompt": "hello", "node_id": "node-test", "model": MODEL_ID}
    body.update(overrides)
    return body


def test_chat_sends_native_options_and_no_openai_keys(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("native"))
        response = client.post("/chat", headers=AUTH, json=chat_body("native", max_tokens=64, temperature=0.2))

    assert response.status_code == 200
    assert set(response.json()) == {"response", "node", "conversation_id", "message_count", "model"}
    assert response.json()["node"] == "Test Ollama"
    assert response.json()["response"] == "native"
    payload = json.loads(route.calls.last.request.content)
    # top_p 1.0 keeps /v1 sampling parity on the native endpoint.
    assert payload["options"] == {"top_p": 1.0, "num_predict": 64, "temperature": 0.2}
    assert payload["stream"] is False
    for absent in OPENAI_ONLY_KEYS:
        assert absent not in payload
    assert route.calls.last.request.extensions["timeout"] == CHAT_NODE_TIMEOUT
    assert router.MODEL_HEALTH == {}


def test_chat_stream_sends_native_options_and_no_openai_keys(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "native"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
            )
        )
        response = client.post(
            "/chat/stream", headers=AUTH, json=chat_body("native-stream", max_tokens=48, temperature=0.4)
        )

    assert response.status_code == 200
    assert 'data: {"token": "native", "done": false}\n\n' in response.text
    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == MODEL_ID
    assert payload["options"] == {"top_p": 1.0, "num_predict": 48, "temperature": 0.4}
    assert payload["stream"] is True
    for absent in OPENAI_ONLY_KEYS:
        assert absent not in payload
    assert route.calls.last.request.extensions["timeout"] == CHAT_NODE_TIMEOUT
    assert router.MODEL_HEALTH == {}


def test_null_temperature_keeps_v1_default_and_null_max_tokens_omits_num_predict(router_factory):
    """ChatRequest fields are Optional: `/v1` turned a null temperature into 1.0 and dropped
    num_predict for a null max_tokens, so the native payload must do the same (DL-TRANSPORT-01a)."""
    router, client, _ = router_factory()
    assert router.chat_temperature(None) == 1.0
    assert router.chat_temperature(0.0) == 0.0
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("native"))
        response = client.post(
            "/chat", headers=AUTH, json=chat_body("null-temp", max_tokens=None, temperature=None)
        )
        assert response.status_code == 200
        payload = json.loads(route.calls.last.request.content)
        assert payload["options"] == {"top_p": 1.0, "temperature": 1.0}

        route.mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "native"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
            )
        )
        response = client.post(
            "/chat/stream", headers=AUTH, json=chat_body("null-temp-stream", max_tokens=None, temperature=None)
        )
        assert response.status_code == 200
        payload = json.loads(route.calls.last.request.content)
        assert payload["options"] == {"top_p": 1.0, "temperature": 1.0}

        # A request that omits both fields still gets the ChatRequest defaults.
        response = client.post("/chat/stream", headers=AUTH, json=chat_body("default-temp-stream"))
        assert response.status_code == 200
        payload = json.loads(route.calls.last.request.content)
        assert payload["options"] == {"top_p": 1.0, "num_predict": 2048, "temperature": 0.7}


def test_policy_hooks_reach_the_node_payload_on_every_plain_chat_path(router_factory, monkeypatch):
    """chat_node_options / chat_keep_alive are the point of the migration: /v1 dropped both silently."""
    router, client, _ = router_factory()
    seen = []

    def fake_options(node, model_id):
        seen.append(("options", node.id, model_id))
        return {"num_ctx": 4096, "ignored": None}

    def fake_keep_alive(conversation):
        seen.append(("keep_alive", conversation is None))
        return "5m"

    monkeypatch.setattr(router, "chat_node_options", fake_options)
    monkeypatch.setattr(router, "chat_keep_alive", fake_keep_alive)

    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("chat"))

        chat = client.post("/chat", headers=AUTH, json=chat_body("hooks-chat", max_tokens=64, temperature=0.2))
        assert chat.status_code == 200
        payload = json.loads(route.calls.last.request.content)
        assert payload["options"] == {"top_p": 1.0, "num_predict": 64, "temperature": 0.2, "num_ctx": 4096}
        assert payload["keep_alive"] == "5m"
        assert payload["stream"] is False

        route.mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "stream"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
            )
        )
        stream = client.post(
            "/chat/stream", headers=AUTH, json=chat_body("hooks-stream", max_tokens=32, temperature=0.9)
        )
        assert stream.status_code == 200
        assert 'data: {"token": "stream", "done": false}\n\n' in stream.text
        payload = json.loads(route.calls.last.request.content)
        assert payload["options"] == {"top_p": 1.0, "num_predict": 32, "temperature": 0.9, "num_ctx": 4096}
        assert payload["keep_alive"] == "5m"
        assert payload["stream"] is True

        route.mock(return_value=ollama_chat_json("sum"))
        router.MODEL_INVENTORY["node-test"] = {MODEL_ID}
        assert router.generate_conversation_summary([{"role": "user", "content": "a"}] * 3) == "sum"
        payload = json.loads(route.calls.last.request.content)
        assert payload["options"] == {"top_p": 1.0, "num_predict": 150, "temperature": 0.3, "num_ctx": 4096}
        assert payload["keep_alive"] == "5m"
        assert payload["stream"] is False

    assert route.calls.call_count == 3
    assert [entry[0] for entry in seen] == ["options", "keep_alive"] * 3
    assert all(entry[1:] == ("node-test", MODEL_ID) for entry in seen if entry[0] == "options")
    assert [entry[1] for entry in seen if entry[0] == "keep_alive"] == [False, False, True]  # summary: no conversation


def test_images_become_per_message_base64_on_chat_and_stream(router_factory):
    router, client, _ = router_factory()
    # Plain data URL, data URL with a media-type parameter, and raw base64.
    images = ["data:image/png;base64,QUJD", "data:image/png;charset=utf-8;base64,QUJF", "QUJC"]
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("seen"))

        chat = client.post("/chat", headers=AUTH, json=chat_body("img-chat", prompt="look", images=images))
        assert chat.status_code == 200
        payload = json.loads(route.calls.last.request.content)
        assert payload["messages"][-1] == {"role": "user", "content": "look", "images": ["QUJD", "QUJF", "QUJC"]}
        assert "images" not in payload

        route.mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "seen"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
            )
        )
        stream = client.post("/chat/stream", headers=AUTH, json=chat_body("img-stream", prompt="look", images=images))
        assert stream.status_code == 200
        assert '"token": "seen"' in stream.text
        payload = json.loads(route.calls.last.request.content)
        assert payload["messages"][-1] == {"role": "user", "content": "look", "images": ["QUJD", "QUJF", "QUJC"]}
        assert "images" not in payload

        route.mock(return_value=ollama_chat_json("only image"))
        image_only = client.post("/chat", headers=AUTH, json=chat_body("img-only", prompt="", images=[images[0]]))
        assert image_only.status_code == 200
        payload = json.loads(route.calls.last.request.content)
        assert payload["messages"][-1] == {"role": "user", "content": "", "images": ["QUJD"]}
        assert router.CONVERSATIONS["img-only"]["messages"][0] == {"role": "user", "content": "[image]"}

        oversized = client.post(
            "/chat", headers=AUTH, json=chat_body("img-big", prompt="", images=["A" * (router.MAX_IMAGE_SIZE + 1)])
        )
        assert oversized.status_code == 400
        assert oversized.json()["detail"] == "Image too large (max 5MB base64)"
    assert route.calls.call_count == 3


def test_stream_keeps_thinking_private_and_done_line_content(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "", "thinking": "secret plan"}, "done": False},
                {"message": {"role": "assistant", "content": "Hel"}, "done": False},
                {"message": {"role": "assistant", "content": "lo"}, "done": True, "done_reason": "stop", "eval_count": 2},
            )
        )
        response = client.post("/chat/stream", headers=AUTH, json=chat_body("think"))

    assert response.status_code == 200
    assert 'data: {"token": "Hel", "done": false}\n\n' in response.text
    assert 'data: {"token": "lo", "done": false}\n\n' in response.text
    assert "secret plan" not in response.text
    assert "thinking" not in response.text
    assert '"error"' not in response.text
    assert response.text.rstrip().endswith('data: {"token": "", "done": true, "message_count": 2}')
    assert router.CONVERSATIONS["think"]["messages"][1] == {"role": "assistant", "content": "Hello"}


# Mechanism behind the parity claim: Ollama's openai.go ChatWriter.writeResponse unmarshals the
# in-band {"error": ...} NDJSON line into an empty api.ChatResponse, so /v1 emitted a chunk with
# delta.content == "" and then EOF; the old parser read an empty delta and ended the stream normally.
def test_stream_mid_stream_error_line_keeps_partial_reply_like_v1(router_factory, capsys):
    """DL-TRANSPORT-01a parity: /v1 turned an in-band error line into an empty delta, so the
    stream ended normally and the partial text was kept. No new SSE error event until DL-UX-01;
    the only new trace is a `stream_node_error` entry for /monitoring/health."""
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "partial"}, "done": False},
                {"error": "runner crashed"},
            )
        )
        response = client.post("/chat/stream", headers=AUTH, json=chat_body("crash"))

    assert response.status_code == 200
    assert 'data: {"token": "partial", "done": false}\n\n' in response.text
    assert '"error"' not in response.text
    assert "runner crashed" not in response.text
    assert response.text.rstrip().endswith('data: {"token": "", "done": true, "message_count": 2}')
    assert router.CONVERSATIONS["crash"]["messages"][1] == {"role": "assistant", "content": "partial"}
    assert router.COST_LOG.exists()
    assert router.MODEL_HEALTH == {}
    assert [(e["event"], e["detail"]) for e in router.RECENT_ERRORS] == [("stream_node_error", "runner crashed")]
    assert "Node stream error after 7 chars: runner crashed" in capsys.readouterr().out


def test_stream_eof_without_done_line_keeps_partial_reply(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_ndjson({"message": {"role": "assistant", "content": "partial"}, "done": False})
        )
        response = client.post("/chat/stream", headers=AUTH, json=chat_body("eof"))

    assert response.status_code == 200
    assert 'data: {"token": "partial", "done": false}\n\n' in response.text
    assert '"error"' not in response.text
    assert response.text.rstrip().endswith('data: {"token": "", "done": true, "message_count": 2}')
    assert router.CONVERSATIONS["eof"]["messages"][1] == {"role": "assistant", "content": "partial"}


def test_chat_node_failures_keep_existing_strings(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat")

        route.mock(return_value=httpx.Response(500, text='{"error":"model not found"}'))
        http_error = client.post("/chat", headers=AUTH, json=chat_body("http-error"))
        assert http_error.status_code == 500
        assert http_error.json()["detail"] == 'Node error: {"error":"model not found"}'
        assert router.MODEL_HEALTH[MODEL_ID]["last_error"] == "http_error"

        route.mock(side_effect=httpx.ReadTimeout("slow"))
        timeout = client.post("/chat", headers=AUTH, json=chat_body("timeout"))
        assert timeout.status_code == 504
        assert timeout.json()["detail"] == "Node 'Test Ollama' timed out"
        assert router.MODEL_HEALTH[MODEL_ID]["last_error"] == "timeout"

        route.mock(side_effect=httpx.ConnectError("refused"))
        connect = client.post("/chat", headers=AUTH, json=chat_body("connect"))
        assert connect.status_code == 503
        assert connect.json()["detail"] == f"Cannot connect to node 'Test Ollama' at {TEST_NODE_URL}"
        assert router.MODEL_HEALTH[MODEL_ID] == {"failures": 3, "last_error": "connect_error"}

        long_body = "x" * 300
        route.mock(return_value=httpx.Response(503, text=long_body))
        stream = client.post("/chat/stream", headers=AUTH, json=chat_body("stream-503"))
        assert stream.status_code == 200
        assert f'"error": "Node error 503: {"x" * 200}"' in stream.text
        assert "x" * 201 not in stream.text

        route.mock(side_effect=httpx.ReadTimeout("slow"))
        assert '"error": "Node timed out"' in client.post("/chat/stream", headers=AUTH, json=chat_body("s-t")).text
        route.mock(side_effect=httpx.ConnectError("refused"))
        assert '"error": "Cannot connect to node"' in client.post("/chat/stream", headers=AUTH, json=chat_body("s-c")).text
    assert router.MODEL_HEALTH[MODEL_ID]["failures"] == 3  # the stream path never tracks failures


def test_chat_rejects_openai_shaped_body(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
        )
        response = client.post("/chat", headers=AUTH, json=chat_body("openai"))

    assert response.status_code == 500
    assert response.json()["detail"].startswith("Invalid response from node:")
    assert [m["role"] for m in router.CONVERSATIONS["openai"]["messages"]] == ["user"]
    assert not router.COST_LOG.exists()
    assert router.MODEL_HEALTH == {}  # a malformed body is not a node failure


def test_summary_uses_native_chat_and_falls_back(router_factory):
    router, _, _ = router_factory()
    router.MODEL_INVENTORY["node-test"] = {MODEL_ID}
    older = [{"role": "user", "content": "a"}] * 3
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("sum"))
        assert router.generate_conversation_summary(older) == "sum"
        payload = json.loads(route.calls.last.request.content)
        assert payload["model"] == MODEL_ID
        assert payload["stream"] is False
        assert payload["options"] == {"top_p": 1.0, "num_predict": 150, "temperature": 0.3}
        assert "keep_alive" not in payload
        assert route.calls.last.request.extensions["timeout"] == SUMMARY_NODE_TIMEOUT
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        assert payload["messages"][1]["content"].startswith("Summarize the earlier conversation")

        route.mock(return_value=httpx.Response(503, text="node offline"))
        assert router.generate_conversation_summary(older) == "[Earlier conversation summary over 3 messages]"
    assert router.generate_conversation_summary([]) == ""
