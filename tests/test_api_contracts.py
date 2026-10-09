import asyncio
import hmac
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from conftest import TEST_API_KEY, TEST_NODE, TEST_NODE_URL, ollama_chat_json, ollama_chat_ndjson


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
    assert health.json() == {"status": "ok", "version": PRODUCT_VERSION}
    assert health.json()["version"] == router.PRODUCT_VERSION == PRODUCT_VERSION
    assert router.app.version == router.PRODUCT_VERSION
    assert client.get("/nodes").status_code == 401
    assert client.get("/nodes", headers={"X-API-Key": "wrong"}).status_code == 401
    response = client.get("/nodes", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == [TEST_NODE]

    _, unconfigured_client, _ = router_factory(api_key=None)
    response = unconfigured_client.get("/nodes")
    assert response.status_code == 503
    assert "DAVE_API_KEY" in response.json()["detail"]


def test_public_health_never_discloses_operational_state(router_factory):
    for configured_key in (TEST_API_KEY, None):
        router, client, _ = router_factory(api_key=configured_key)
        router.CONVERSATIONS["private-session"] = {"user_id": "private-user"}
        for headers in ({}, {"X-API-Key": "wrong"}, AUTH):
            response = client.get("/health", headers=headers)
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "version": PRODUCT_VERSION}


def test_api_keys_use_constant_time_byte_comparison(router_factory, monkeypatch):
    router, client, _ = router_factory()
    comparisons = []

    def compare(candidate, expected):
        comparisons.append((candidate, expected))
        return hmac.compare_digest(candidate, expected)

    monkeypatch.setattr(router, "compare_digest", compare)
    assert client.get("/nodes").status_code == 401
    assert client.get("/nodes", headers={"X-API-Key": ""}).status_code == 401
    assert comparisons == []
    candidates = ("wrong", TEST_API_KEY[:-1] + "X", TEST_API_KEY + " ", TEST_API_KEY)
    for candidate in candidates:
        response = client.get("/nodes", headers={"X-API-Key": candidate})
        assert response.status_code == (200 if candidate == TEST_API_KEY else 401)
    assert comparisons == [(candidate.encode("utf-8"), TEST_API_KEY.encode("utf-8"))
                           for candidate in candidates]


def test_constant_time_comparison_accepts_non_ascii_key_values(router_factory):
    router, _, _ = router_factory(api_key="test-only-clé")
    assert router.require_api_key("test-only-clé") == "default"
    with pytest.raises(router.HTTPException) as caught:
        router.require_api_key("test-only-clè")
    assert caught.value.status_code == 401


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


def test_static_bundle_revalidates_and_api_headers_are_unchanged(router_factory):
    _, client, _ = router_factory()
    for path in (
        "/",
        "/index.html",
        "/app.js",
        "/style.css",
        "/console.js",
        "/prompt-contract.js",
        "/anticipation.js",
        "/vendor/gsap/gsap.min.js",
        "/monitoring.html",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache", path
        assert response.headers["etag"], path
        assert response.headers["last-modified"], path

        revalidated = client.get(path, headers={"If-None-Match": response.headers["etag"]})
        assert revalidated.status_code == 304, path
        assert revalidated.headers["cache-control"] == "no-cache", path
        assert revalidated.content == b"", path

    assert "cache-control" not in client.get("/health").headers
    assert "cache-control" not in client.get("/nodes", headers=AUTH).headers
    assert "cache-control" not in client.get("/nodes").headers
    assert "cache-control" not in client.get("/app.py").headers


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
        assert summary_payloads[0]["options"] == {"top_p": 1.0, "num_predict": 150, "temperature": 0.3, "num_ctx": 16384}
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


OPENAI_ONLY_KEYS = ("max_tokens", "temperature", "think", "tools", "images")
CHAT_KEEP_ALIVE_DEFAULT = "30m"  # DL-KEEP-01: sent for chats that belong to a conversation
# DL-TIME-01 node deadlines; httpx records the value each call site passes as request.extensions["timeout"].
# Non-streaming chat: the whole reply within the total budget, connect fails fast.
CHAT_NODE_TIMEOUT = httpx.Timeout(600, connect=10).as_dict()
# Streaming chat: httpx bounds only the connect; first-chunk and idle are enforced per line.
STREAM_NODE_TIMEOUT = httpx.Timeout(None, connect=10).as_dict()
SUMMARY_NODE_TIMEOUT = httpx.Timeout(10).as_dict()


def contract_events(text):
    """The SSE text minus DL-UX-01's additive status/stats events, which these contracts predate."""
    return "".join(
        block + "\n\n" for block in text.split("\n\n")
        if block and not block.startswith(('data: {"status"', 'data: {"stats"'))
    )


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
    assert payload["options"] == {"top_p": 1.0, "num_predict": 64, "temperature": 0.2, "num_ctx": 16384}
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
    assert payload["options"] == {"top_p": 1.0, "num_predict": 48, "temperature": 0.4, "num_ctx": 16384}
    assert payload["stream"] is True
    for absent in OPENAI_ONLY_KEYS:
        assert absent not in payload
    assert route.calls.last.request.extensions["timeout"] == STREAM_NODE_TIMEOUT
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
        assert payload["options"] == {"top_p": 1.0, "temperature": 1.0, "num_ctx": 16384}

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
        assert payload["options"] == {"top_p": 1.0, "temperature": 1.0, "num_ctx": 16384}

        # A request that omits both fields still gets the ChatRequest defaults.
        response = client.post("/chat/stream", headers=AUTH, json=chat_body("default-temp-stream"))
        assert response.status_code == 200
        payload = json.loads(route.calls.last.request.content)
        assert payload["options"] == {"top_p": 1.0, "num_predict": 2048, "temperature": 0.7, "num_ctx": 16384}


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
    # Reasoning text stays private; DL-UX-01 only announces that the model is thinking.
    assert response.text.count("thinking") == 1
    assert 'data: {"status": "thinking", "done": false}\n\n' in response.text
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


@pytest.mark.asyncio
async def test_summary_uses_native_chat_and_falls_back(router_factory):
    # Match background_summarizer: this synchronous helper is called on an active event loop.
    router, _, _ = router_factory()
    router.MODEL_INVENTORY["node-test"] = {MODEL_ID}
    older = [{"role": "user", "content": "a"}] * 3
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("sum"))
        assert router.generate_conversation_summary(older) == "sum"
        payload = json.loads(route.calls.last.request.content)
        assert payload["model"] == MODEL_ID
        assert payload["stream"] is False
        assert payload["options"] == {"top_p": 1.0, "num_predict": 150, "temperature": 0.3, "num_ctx": 16384}
        assert "keep_alive" not in payload
        assert route.calls.last.request.extensions["timeout"] == SUMMARY_NODE_TIMEOUT
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        assert payload["messages"][1]["content"].startswith("Summarize the earlier conversation")

        route.mock(return_value=httpx.Response(503, text="node offline"))
        assert router.generate_conversation_summary(older) == "[Earlier conversation summary over 3 messages]"
    assert router.generate_conversation_summary([]) == ""


# --- Tool-call-only replies: plain chat runs no tools, so it must not keep an empty assistant turn ---


TOOL_CALL = {"function": {"name": "browser.run", "arguments": {"query": "weather in Philadelphia"}}}
TOOL_NOTICE = (
    "The model tried to use a tool (browser.run) instead of replying. Plain chat can't run "
    "tools, so no reply was saved. Select a tool from the Tools menu and send again."
)


def record_side_writes(router, monkeypatch):
    """Record every embedding and project-artifact write a chat path attempts."""
    writes = []
    monkeypatch.setattr(router, "store_message_embedding", lambda *a, **k: writes.append(("embedding", a)))
    monkeypatch.setattr(router, "capture_project_artifact", lambda *a, **k: writes.append(("artifact", a)))
    return writes


def test_chat_tool_call_only_reply_returns_notice_and_persists_no_empty_turn(router_factory, monkeypatch):
    router, client, _ = router_factory()
    writes = record_side_writes(router, monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_json("", message={"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]})
        )
        response = client.post("/chat", headers=AUTH, json=chat_body("tool-only", prompt="weather in Philadelphia?"))

        assert response.status_code == 200
        assert response.json() == {
            "response": TOOL_NOTICE,
            "node": "Test Ollama",
            "conversation_id": "tool-only",
            "message_count": 1,
            "model": MODEL_ID,
            "notice": TOOL_NOTICE,
            "reason": "tool_call_only",
            "tools": ["browser.run"],
        }
        convo = router.CONVERSATIONS["tool-only"]
        # The user turn stays, exactly as after a node error; no assistant turn of any kind is kept.
        assert convo["messages"] == [{"role": "user", "content": "weather in Philadelphia?"}]
        assert convo["title"] == router.DEFAULT_CONVO_TITLE
        assert writes == []
        assert not router.COST_LOG.exists()
        assert router.MODEL_HEALTH == {}  # the node answered; this is not a node failure

        # The next turn gives the model no empty assistant turn to imitate, and persists normally.
        route.mock(return_value=ollama_chat_json("Probably mild."))
        follow_up = client.post("/chat", headers=AUTH, json=chat_body("tool-only", prompt="just guess"))

    assert follow_up.status_code == 200
    assert set(follow_up.json()) == {"response", "node", "conversation_id", "message_count", "model"}
    assert follow_up.json()["response"] == "Probably mild."
    sent = json.loads(route.calls.last.request.content)["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "user"]
    saved = json.loads(router.DATA_FILE.read_text())["tool-only"]["messages"]
    assert saved == [
        {"role": "user", "content": "weather in Philadelphia?"},
        {"role": "user", "content": "just guess"},
        {"role": "assistant", "content": "Probably mild."},
    ]


def test_chat_stream_tool_call_only_reply_emits_notice_event_and_persists_no_empty_turn(router_factory, monkeypatch):
    router, client, _ = router_factory()
    writes = record_side_writes(router, monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "", "thinking": "I should browse"}, "done": False},
                {"message": {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "eval_count": 9},
            )
        )
        response = client.post("/chat/stream", headers=AUTH, json=chat_body("tool-stream", prompt="weather?"))

    assert response.status_code == 200
    # Exact bytes: one notice event, then the unchanged terminal event; no token or error events.
    assert contract_events(response.text) == (
        'data: {"notice": "' + TOOL_NOTICE + '", "reason": "tool_call_only", "tools": ["browser.run"], "done": false}\n\n'
        'data: {"token": "", "done": true, "message_count": 1}\n\n'
    )
    assert "I should browse" not in response.text
    convo = router.CONVERSATIONS["tool-stream"]
    assert convo["messages"] == [{"role": "user", "content": "weather?"}]
    assert convo["title"] == router.DEFAULT_CONVO_TITLE
    assert writes == []
    assert not router.COST_LOG.exists()
    assert list(router.RECENT_ERRORS) == []


def test_chat_stream_tool_call_after_blank_tokens_or_error_line_still_keeps_nothing(router_factory, monkeypatch):
    """Whitespace is not a reply, and an in-band error line after the call does not turn it into one."""
    router, client, _ = router_factory()
    writes = record_side_writes(router, monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "\n\n"}, "done": False},
                {"message": {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL, TOOL_CALL]}, "done": False},
                {"error": "runner crashed"},
            )
        )
        response = client.post("/chat/stream", headers=AUTH, json=chat_body("tool-blank"))

    assert contract_events(response.text) == (
        'data: {"token": "\\n\\n", "done": false}\n\n'
        'data: {"notice": "' + TOOL_NOTICE + '", "reason": "tool_call_only", "tools": ["browser.run"], "done": false}\n\n'
        'data: {"token": "", "done": true, "message_count": 1}\n\n'
    )
    assert router.CONVERSATIONS["tool-blank"]["messages"] == [{"role": "user", "content": "hello"}]
    assert writes == []
    assert [(e["event"], e["detail"]) for e in router.RECENT_ERRORS] == [("stream_node_error", "runner crashed")]


def test_replies_with_content_keep_the_existing_contract_even_with_a_tool_call(router_factory):
    """Byte-identical SSE and /chat JSON for any reply that has visible content."""
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat")
        route.mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "Let me"}, "done": False},
                {"message": {"role": "assistant", "content": " check.", "tool_calls": [TOOL_CALL]}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
            )
        )
        stream = client.post("/chat/stream", headers=AUTH, json=chat_body("mixed-stream"))
        route.mock(return_value=ollama_chat_ndjson(
            {"message": {"role": "assistant", "content": "Plain"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        ))
        plain = client.post("/chat/stream", headers=AUTH, json=chat_body("plain-stream"))
        route.mock(
            return_value=ollama_chat_json("", message={"role": "assistant", "content": "Let me check.", "tool_calls": [TOOL_CALL]})
        )
        chat = client.post("/chat", headers=AUTH, json=chat_body("mixed-chat"))

    assert contract_events(stream.text) == (
        'data: {"token": "Let me", "done": false}\n\n'
        'data: {"token": " check.", "done": false}\n\n'
        'data: {"token": "", "done": true, "message_count": 2}\n\n'
    )
    assert contract_events(plain.text) == (
        'data: {"token": "Plain", "done": false}\n\n'
        'data: {"token": "", "done": true, "message_count": 2}\n\n'
    )
    assert router.CONVERSATIONS["mixed-stream"]["messages"][1] == {"role": "assistant", "content": "Let me check."}
    assert chat.json() == {
        "response": "Let me check.",
        "node": "Test Ollama",
        "conversation_id": "mixed-chat",
        "message_count": 2,
        "model": MODEL_ID,
    }


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("existing", [False, True])
def test_tool_call_only_notice_retains_user_turn_after_disk_reload(router_factory, monkeypatch, endpoint, existing):
    router, client, _ = router_factory()
    conversation_id = "durable-notice"
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat")
        if existing:
            route.mock(return_value=ollama_chat_json("Earlier answer"))
            assert client.post("/chat", headers=AUTH, json=chat_body(conversation_id, prompt="Earlier question")).status_code == 200
        writes = record_side_writes(router, monkeypatch)
        cost_before = router.COST_LOG.read_bytes() if router.COST_LOG.exists() else None
        message = {"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]}
        route.mock(return_value=(ollama_chat_json("", message=message) if endpoint == "/chat"
                                 else ollama_chat_ndjson({"message": message, "done": True})))
        response = client.post(endpoint, headers=AUTH, json=chat_body(conversation_id, prompt="Retain this question"))
    assert response.status_code == 200
    assert "tool_call_only" in response.text
    memory = router.CONVERSATIONS[conversation_id]
    # Use the startup loader before any later reply or unrelated save can mask the defect.
    reloaded = router.load_conversations()[conversation_id]
    assert reloaded == memory
    expected = ([{"role": "user", "content": "Earlier question"},
                 {"role": "assistant", "content": "Earlier answer"}] if existing else [])
    assert reloaded["messages"] == expected + [{"role": "user", "content": "Retain this question"}]
    assert writes == []
    assert (router.COST_LOG.read_bytes() if router.COST_LOG.exists() else None) == cost_before


def test_tool_call_only_notice_echoes_only_plain_tool_names(router_factory):
    router, _, _ = router_factory()
    notice = router.tool_call_only_notice
    assert notice("Hi", [TOOL_CALL]) is None
    assert notice("", []) is None
    assert notice(" \n", ()) is None

    hostile = [
        {"function": {"name": "<img src=x onerror=alert(1)>"}},
        {"function": {"name": "x" * 65}},
        {"function": {"name": "browser.run"}},
        {"function": {"name": "browser.run"}},
        {"function": {"name": "a"}}, {"function": {"name": "b"}}, {"function": {"name": "c"}}, {"function": {"name": "d"}},
    ]
    result = notice("", hostile)
    assert result["tools"] == ["browser.run", "a", "b", "c"]
    assert result["notice"].startswith("The model tried to use a tool (browser.run, a, b, c) instead of replying.")
    assert "<img" not in result["notice"]

    unnamed = notice("", [{"function": {"arguments": {}}}])
    assert unnamed["tools"] == []
    assert unnamed["notice"].startswith("The model tried to use a tool instead of replying.")
    assert unnamed["notice"].endswith("Select a tool from the Tools menu and send again.")


# DL-CTX-01 / DL-KEEP-01 --------------------------------------------------------------------------

def test_chat_and_stream_send_router_context_and_keep_alive(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("ok"))
        assert client.post("/chat", headers=AUTH, json=chat_body("ctx-chat")).status_code == 200
        chat_payload = json.loads(route.calls.last.request.content)
        route.mock(return_value=ollama_chat_ndjson(
            {"message": {"role": "assistant", "content": "ok"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        ))
        assert client.post("/chat/stream", headers=AUTH, json=chat_body("ctx-stream")).status_code == 200
        stream_payload = json.loads(route.calls.last.request.content)
    for payload in (chat_payload, stream_payload):
        # The router, not the node's own default (131072 on the Windows app), decides the window.
        assert payload["options"]["num_ctx"] == router.CHAT_NUM_CTX == 16384
        assert payload["keep_alive"] == CHAT_KEEP_ALIVE_DEFAULT


def test_chat_context_is_the_smaller_of_model_window_and_cap(router_factory, monkeypatch):
    monkeypatch.setenv("DAVE_MODEL_CONTEXT_WINDOWS", json.dumps({"small:1b": 4096, "big:70b": 65536}))
    router, _, _ = router_factory()
    assert router.chat_num_ctx("small:1b") == 4096
    assert router.chat_num_ctx("big:70b") == 16384
    assert router.chat_num_ctx("unlisted:7b") == 16384  # default window 32768, capped

    monkeypatch.setenv("DAVE_CHAT_NUM_CTX", "32768")
    router, _, _ = router_factory()
    assert router.chat_num_ctx("big:70b") == 32768
    assert router.chat_num_ctx("small:1b") == 4096

    monkeypatch.setenv("DAVE_CHAT_NUM_CTX", "not-a-number")
    router, _, _ = router_factory()
    assert router.CHAT_NUM_CTX == 16384

    # Below the floor there is no room for project context after the output and safety reserves.
    monkeypatch.setenv("DAVE_CHAT_NUM_CTX", "512")
    router, _, _ = router_factory()
    assert router.CHAT_NUM_CTX == router.CHAT_NUM_CTX_FLOOR == 8192


def test_keep_alive_setting_and_its_empty_value(router_factory, monkeypatch):
    monkeypatch.setenv("DAVE_CHAT_KEEP_ALIVE", "1h")
    router, _, _ = router_factory()
    assert router.chat_keep_alive({"id": "c"}) == "1h"
    assert router.chat_keep_alive(None) is None  # the summary never pins a model

    monkeypatch.setenv("DAVE_CHAT_KEEP_ALIVE", "  ")
    router, _, _ = router_factory()
    assert router.chat_keep_alive({"id": "c"}) is None  # empty: server default, nothing sent


def test_project_budget_uses_chat_window_for_plain_chat_and_model_window_for_agents(router_factory, monkeypatch):
    monkeypatch.setenv("DAVE_MODEL_CONTEXT_WINDOWS", json.dumps({MODEL_ID: 65536}))
    router, _, _ = router_factory()
    monkeypatch.setattr(
        router.PROJECT_CONTEXT, "build_context_messages",
        lambda project_id, *, query, available_tokens: {"messages": [], "budget": {}},
    )
    common = dict(project_id="p1", project={}, model_id=MODEL_ID, output_reserve=2048,
                  query="q", system_prompt="s", history=[])
    _, chat_budget = router.build_project_messages_for_node(**common, window=router.chat_num_ctx(MODEL_ID))
    _, agent_budget = router.build_project_messages_for_node(**common)
    # Plain chat plans for the num_ctx it sends; agent runs (/v1, no num_ctx) keep the model window.
    assert chat_budget["model_context_window"] == 16384
    assert agent_budget["model_context_window"] == 65536


def test_plain_chat_call_sites_pass_the_chat_window(router_factory, monkeypatch):
    router, client, _ = router_factory()
    windows = []
    real = router.build_project_messages_for_node

    def spy(**kwargs):
        windows.append(kwargs.get("window"))
        return real(**kwargs)

    monkeypatch.setattr(router, "build_project_messages_for_node", spy)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_json("ok"))
        client.post("/chat", headers=AUTH, json=chat_body("win-chat"))
        route.mock(return_value=ollama_chat_ndjson(
            {"message": {"role": "assistant", "content": "ok"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        ))
        client.post("/chat/stream", headers=AUTH, json=chat_body("win-stream"))
    assert windows == [router.chat_num_ctx(MODEL_ID)] * 2


# DL-TIME-01 ------------------------------------------------------------------------------------------

class _SlowNDJSON(httpx.AsyncByteStream):
    """An Ollama stream that waits before each line: (delay_seconds, json_line) pairs."""

    def __init__(self, *steps):
        self.steps = steps

    async def __aiter__(self):
        for delay, line in self.steps:
            await asyncio.sleep(delay)
            yield (json.dumps(line) + "\n").encode()


def _stream_with(router_factory, monkeypatch, *steps, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=httpx.Response(200, stream=_SlowNDJSON(*steps)))
        return router, client.post("/chat/stream", headers=AUTH, json=chat_body("slow")).text


def test_stream_times_out_when_no_first_line_arrives(router_factory, monkeypatch):
    router, text = _stream_with(
        router_factory, monkeypatch,
        (2.0, {"message": {"role": "assistant", "content": "late"}, "done": True}),
        DAVE_NODE_FIRST_CHUNK_TIMEOUT="0.2",
    )
    assert '"error": "Node timed out"' in text
    assert '"token": "late"' not in text
    assert not [m for m in router.CONVERSATIONS["slow"]["messages"] if m["role"] == "assistant"]


def test_stream_times_out_when_the_node_stalls_after_the_first_line(router_factory, monkeypatch):
    _, text = _stream_with(
        router_factory, monkeypatch,
        (0.0, {"message": {"role": "assistant", "content": "partial"}, "done": False}),
        (2.0, {"message": {"role": "assistant", "content": " never"}, "done": True}),
        DAVE_NODE_FIRST_CHUNK_TIMEOUT="5", DAVE_NODE_IDLE_TIMEOUT="0.2",
    )
    assert '"token": "partial"' in text
    assert '"error": "Node timed out"' in text
    assert "never" not in text


def test_slow_first_line_within_the_deadline_is_not_a_timeout(router_factory, monkeypatch):
    # The old flat read timeout would have cut this off; a first-chunk budget lets it through.
    _, text = _stream_with(
        router_factory, monkeypatch,
        (0.4, {"message": {"role": "assistant", "content": "ok"}, "done": False}),
        (0.0, {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"}),
        DAVE_NODE_FIRST_CHUNK_TIMEOUT="2", DAVE_NODE_IDLE_TIMEOUT="0.3",
    )
    assert '"token": "ok"' in text
    assert "Node timed out" not in text


def test_node_deadline_settings_fall_back_on_bad_values(router_factory, monkeypatch):
    for value in ("abc", "0", "-5", ""):
        monkeypatch.setenv("DAVE_NODE_IDLE_TIMEOUT", value)
        router, _, _ = router_factory()
        assert router.NODE_IDLE_TIMEOUT == 120.0
    monkeypatch.setenv("DAVE_NODE_CONNECT_TIMEOUT", "3.5")
    monkeypatch.setenv("DAVE_NODE_TOTAL_TIMEOUT", "900")
    router, _, _ = router_factory()
    assert router.node_complete_timeout().as_dict() == httpx.Timeout(900, connect=3.5).as_dict()
    assert router.node_stream_timeout().as_dict() == httpx.Timeout(None, connect=3.5).as_dict()
    assert router.HARNESS.model_timeout_seconds == 900  # the tool loop's per-step wall clock
    monkeypatch.setenv("DAVE_NODE_TOTAL_TIMEOUT", "inf")
    router, _, _ = router_factory()
    assert router.NODE_TOTAL_TIMEOUT == 600.0


def test_plain_chat_and_tool_loop_get_the_wall_clock_total(router_factory, monkeypatch):
    router, client, _ = router_factory(tools=True)
    assert router.HARNESS.model_timeout_seconds == router.NODE_TOTAL_TIMEOUT == 600.0
    seen = {}

    def fake_chat(*args, **kwargs):
        seen["chat"] = kwargs
        raise httpx.ReadTimeout("The node did not finish its reply before the total deadline")

    async def fake_loop(*args, **kwargs):
        seen["loop"] = kwargs
        raise RuntimeError("stop here")

    monkeypatch.setattr(router, "ollama_chat", fake_chat)
    monkeypatch.setattr(router, "run_executor_loop", fake_loop)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        response = client.post("/chat", headers=AUTH, json=chat_body("total"))
        assert response.status_code == 504
        assert seen["chat"]["total_timeout"] == 600.0
        with pytest.raises(RuntimeError, match="stop here"):
            client.post("/tools/agent/run", headers=AUTH, json={
                "messages": [{"role": "user", "content": "hi"}], "node_id": "node-test", "model": MODEL_ID,
            })
    assert seen["loop"]["model_timeout_seconds"] == 600.0


# DL-UX-01 ------------------------------------------------------------------------------------------

def test_stream_announces_waiting_thinking_then_reports_stats(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_ndjson(
            {"message": {"role": "assistant", "content": "", "thinking": "plan"}, "done": False},
            {"message": {"role": "assistant", "content": "", "thinking": " more"}, "done": False},
            {"message": {"role": "assistant", "content": "Hi"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
             "load_duration": 1_500_000_000, "prompt_eval_count": 40, "prompt_eval_duration": 500_000_000,
             "eval_count": 30, "eval_duration": 600_000_000, "total_duration": 2_700_000_000},
        ))
        text = client.post("/chat/stream", headers=AUTH, json=chat_body("ux")).text
    events = [json.loads(block[6:]) for block in text.split("\n\n") if block.startswith("data: ")]
    assert events[0] == {"status": "waiting", "done": False}
    assert events[1] == {"status": "thinking", "done": False}  # once, before the first token
    assert [e for e in events if e.get("status") == "thinking"] == [events[1]]
    assert events[2] == {"token": "Hi", "done": False}
    stats = events[3]["stats"]
    assert events[3]["done"] is False
    assert stats["gen_tokens"] == 30 and stats["gen_tps"] == 50.0
    assert stats["prompt_tokens"] == 40 and stats["prompt_tps"] == 80.0
    assert stats["load_s"] == 1.5 and stats["total_s"] == 2.7 and stats["ttft_s"] >= 0
    assert events[4] == {"token": "", "done": True, "message_count": 2}
    assert "plan" not in text and "more" not in text


def test_no_stats_after_an_error_and_no_thinking_status_without_reasoning(router_factory):
    router, client, _ = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        route = mock.post(f"{TEST_NODE_URL}/api/chat")
        route.mock(return_value=httpx.Response(500, text="boom"))
        failed = client.post("/chat/stream", headers=AUTH, json=chat_body("ux-err")).text
        route.mock(return_value=ollama_chat_ndjson(
            {"message": {"role": "assistant", "content": "ok"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop"},
        ))
        plain = client.post("/chat/stream", headers=AUTH, json=chat_body("ux-plain")).text
    assert failed.startswith('data: {"status": "waiting", "done": false}')
    assert '"stats"' not in failed and '"error": "Node error 500' in failed
    assert '"thinking"' not in plain
    # The done line carried no metrics, so only the router's own time to first token is reported.
    stats_events = [json.loads(b[6:]) for b in plain.split("\n\n") if b.startswith('data: {"stats"')]
    assert len(stats_events) == 1 and set(stats_events[0]["stats"]) == {"ttft_s"}


# DL-ROUTE-01/02 ------------------------------------------------------------------------------------

# The limit sits above the default system prompt (~900 tokens), which every prompt carries.
PROFILED_NODE = {**TEST_NODE, "profile": {"compute": "cpu", "prompt_token_limit": 2000}}


def test_nodes_list_their_profile_and_a_bad_profile_keeps_the_node(router_factory):
    _, client, _ = router_factory(nodes=[PROFILED_NODE])
    assert client.get("/nodes", headers=AUTH).json() == [
        {**TEST_NODE, "profile": {"compute": "cpu", "prompt_token_limit": 2000, "model_prompt_token_limits": {}}}
    ]
    _, client, _ = router_factory(nodes=[{**TEST_NODE, "profile": {"compute": "tpu"}}])
    assert client.get("/nodes", headers=AUTH).json() == [TEST_NODE]


def _waiting_event(router_factory, prompt):
    _, client, _ = router_factory(nodes=[PROFILED_NODE])
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_ndjson(
            {"message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop"},
        ))
        text = client.post("/chat/stream", headers=AUTH, json=chat_body("big", prompt=prompt)).text
    return json.loads(text.split("\n\n")[0].removeprefix("data: "))


def test_a_prompt_over_the_node_limit_says_so_in_the_waiting_status(router_factory):
    event = _waiting_event(router_factory, "word " * 3000)
    assert event["status"] == "waiting" and event["done"] is False
    assert event["prompt_token_limit"] == 2000
    assert event["prompt_tokens"] > 3750  # the message alone is 3,750
    assert _waiting_event(router_factory, "hi") == {"status": "waiting", "done": False}


# DL-ROUTE-03/04 ------------------------------------------------------------------------------------

def _stream_first_event(router_factory, *, ps=None, busy=0, chat=None):
    router, client, _ = router_factory()
    node_url = router.NODE_CONFIGS[0].url
    for _ in range(busy):
        router.NODE_ACTIVITY.start(node_url)
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        if ps is not None:
            mock.get(f"{TEST_NODE_URL}/api/ps").mock(return_value=ps)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=chat or ollama_chat_ndjson(
            {"message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop"},
        ))
        text = client.post("/chat/stream", headers=AUTH, json=chat_body("route")).text
    return router, node_url, json.loads(text.split("\n\n")[0].removeprefix("data: ")), text


def test_waiting_status_says_whether_the_model_is_loaded(router_factory):
    loaded = httpx.Response(200, json={"models": [{"name": MODEL_ID, "model": MODEL_ID}]})
    _, _, event, _ = _stream_first_event(router_factory, ps=loaded)
    assert event == {"status": "waiting", "model_loaded": True, "done": False}
    _, _, event, _ = _stream_first_event(router_factory, ps=httpx.Response(200, json={"models": []}))
    assert event == {"status": "waiting", "model_loaded": False, "done": False}
    _, _, event, _ = _stream_first_event(router_factory, ps=httpx.Response(404))
    assert event == {"status": "waiting", "done": False}  # unknown residency adds nothing


def test_waiting_status_counts_replies_ahead_and_the_count_is_released(router_factory):
    router, node_url, event, _ = _stream_first_event(router_factory, busy=2)
    assert event["others_in_flight"] == 2
    assert router.NODE_ACTIVITY.in_flight(node_url) == 2  # this stream finished and left
    router, node_url, _, text = _stream_first_event(router_factory, chat=httpx.Response(500, text="boom"))
    assert '"error": "Node error 500' in text
    assert router.NODE_ACTIVITY.in_flight(node_url) == 0


def test_chat_and_tool_loop_count_as_busy_while_the_node_works(router_factory, monkeypatch):
    router, client, _ = router_factory(tools=True)
    node_url = router.NODE_CONFIGS[0].url
    seen = []

    def fake_chat(*args, **kwargs):
        seen.append(("chat", router.NODE_ACTIVITY.in_flight(node_url)))
        raise httpx.ConnectError("down")

    async def fake_bounded(*args, **kwargs):
        seen.append(("tool", router.NODE_ACTIVITY.in_flight(node_url)))
        return {"message": {"role": "assistant", "content": "done"}, "done": True}

    monkeypatch.setattr(router, "ollama_chat", fake_chat)
    monkeypatch.setattr(router, "ollama_chat_bounded", fake_bounded)
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        assert client.post("/chat", headers=AUTH, json=chat_body("busy")).status_code == 503
        run = client.post("/tools/agent/run", headers=AUTH, json={
            "messages": [{"role": "user", "content": "hi"}], "node_id": "node-test", "model": MODEL_ID,
        })
    assert run.status_code == 200 and run.json()["status"] == "completed"
    assert seen == [("chat", 1), ("tool", 1)]
    assert router.NODE_ACTIVITY.in_flight(node_url) == 0


def test_a_warm_follow_up_counts_only_its_new_message(router_factory):
    router, client, _ = router_factory(nodes=[PROFILED_NODE])
    loaded = httpx.Response(200, json={"models": [{"name": MODEL_ID}]})
    reply = {"message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop"}

    def first_event(mock, prompt, ps):
        mock.get(f"{TEST_NODE_URL}/api/ps").mock(return_value=ps)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_ndjson(reply))
        text = client.post("/chat/stream", headers=AUTH, json=chat_body("warm", prompt=prompt)).text
        return json.loads(text.split("\n\n")[0].removeprefix("data: "))

    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        # First turn: nothing cached yet, so the whole long prompt counts.
        assert first_event(mock, "word " * 3000, loaded)["prompt_token_limit"] == 2000
        # Same conversation, model still loaded: the node reads only the new message.
        assert first_event(mock, "hi", loaded) == {"status": "waiting", "model_loaded": True, "done": False}
        # Model unloaded since: the whole conversation is read again.
        cold = first_event(mock, "hi again", httpx.Response(200, json={"models": []}))
        assert cold["model_loaded"] is False and cold["prompt_tokens"] > 3750
        # Something else used the node in between: its cache no longer holds this chat.
        router.NODE_ACTIVITY.start(router.NODE_CONFIGS[0].url)
        router.NODE_ACTIVITY.finish(router.NODE_CONFIGS[0].url)
        assert "prompt_token_limit" in first_event(mock, "and again", loaded)


def test_a_long_conversation_is_never_treated_as_warm(router_factory):
    # Past 10 messages the history window slides each turn, so the cached prefix no longer matches.
    router, _, _ = router_factory()
    short = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}] * 5
    assert router.chat_prompt_key("m", "c", None, short) == "m\nc"
    assert router.chat_prompt_key("m", "c", None, short + [{"role": "user", "content": "q"}]) is None
    assert router.chat_prompt_key("m", "c", "project-1", short[:2]) is None


# DL-ROUTE-05 ---------------------------------------------------------------------------------------

def _size_check(client, conversation_id, prompt, **overrides):
    body = {"conversation_id": conversation_id, "prompt": prompt, "node_id": "node-test", "model": MODEL_ID}
    body.update(overrides)
    return client.post("/chat/size-check", headers=AUTH, json=body)


def test_size_check_sizes_the_draft_the_stream_would_send_and_changes_nothing(router_factory):
    router, client, _ = router_factory(nodes=[PROFILED_NODE])
    reply = {"message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop"}
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        chat = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_ndjson(reply))
        big = _size_check(client, "fresh", "word " * 3000)
        small = _size_check(client, "fresh", "hi")
        assert big.status_code == small.status_code == 200
        assert not chat.called and "fresh" not in router.CONVERSATIONS
        assert router.NODE_ACTIVITY.in_flight(router.NODE_CONFIGS[0].url) == 0
        streamed = client.post("/chat/stream", headers=AUTH, json=chat_body("fresh", prompt="word " * 3000)).text
    waiting = json.loads(streamed.split("\n\n")[0].removeprefix("data: "))
    assert big.json() == {
        "prompt_tokens": waiting["prompt_tokens"], "prompt_token_limit": 2000, "over_limit": True,
        "reads_new_message_only": False, "node_id": "node-test", "model": MODEL_ID,
    }
    assert small.json()["over_limit"] is False and small.json()["prompt_token_limit"] == 2000
    assert 0 < small.json()["prompt_tokens"] < 2000  # the default system prompt plus "hi"


def test_size_check_counts_project_context_like_the_stream(router_factory):
    router, client, _ = router_factory(nodes=[PROFILED_NODE])
    project = client.post("/projects", headers=AUTH, json={
        "name": "Sized", "system_prompt": "Project rule. " * 400,
    }).json()
    reply = {"message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop"}
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_ndjson(reply))
        checked = _size_check(client, "projected", "hello there", project_id=project["project_id"]).json()
        bare = _size_check(client, "bare", "hello there").json()
        streamed = client.post("/chat/stream", headers=AUTH, json=chat_body(
            "projected", prompt="hello there", project_id=project["project_id"],
        )).text
    waiting = json.loads(streamed.split("\n\n")[0].removeprefix("data: "))
    assert checked["over_limit"] is True and checked["prompt_tokens"] == waiting["prompt_tokens"]
    assert checked["prompt_tokens"] > bare["prompt_tokens"] + 1000  # the project's instructions count
    # Once attached, a different project is refused exactly as the stream refuses it.
    other = client.post("/projects", headers=AUTH, json={"name": "Other"}).json()
    assert _size_check(client, "projected", "hi", project_id=other["project_id"]).status_code == 409


def test_size_check_reports_a_warm_follow_up_and_no_limit_without_a_profile(router_factory):
    router, client, _ = router_factory(nodes=[PROFILED_NODE])
    loaded = httpx.Response(200, json={"models": [{"name": MODEL_ID}]})
    reply = {"message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop"}
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        ps = mock.get(f"{TEST_NODE_URL}/api/ps").mock(return_value=loaded)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_ndjson(reply))
        assert _size_check(client, "warm", "word " * 3000).json()["reads_new_message_only"] is False
        assert not ps.called  # nothing cached yet, so residency is not probed
        client.post("/chat/stream", headers=AUTH, json=chat_body("warm", prompt="word " * 3000))
        follow_up = _size_check(client, "warm", "hi").json()
        assert follow_up["reads_new_message_only"] is True and follow_up["over_limit"] is False
        assert follow_up["prompt_tokens"] == 1  # only "hi" is read
        ps.mock(return_value=httpx.Response(200, json={"models": []}))
        cold = _size_check(client, "warm", "hi").json()
        assert cold["reads_new_message_only"] is False and cold["over_limit"] is True

    _, client, _ = router_factory()
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        plain = _size_check(client, None, "word " * 3000).json()
    assert plain["prompt_token_limit"] is None and plain["over_limit"] is False and plain["prompt_tokens"] > 3750


def test_size_check_never_calls_a_model_for_a_long_conversation(router_factory, monkeypatch):
    router, client, _ = router_factory(nodes=[PROFILED_NODE])
    reply = {"message": {"role": "assistant", "content": "ok"}, "done": True, "done_reason": "stop"}
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        chat = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=ollama_chat_ndjson(reply))
        client.post("/chat/stream", headers=AUTH, json=chat_body("long", prompt="start"))
        stored = router.CONVERSATIONS["long"]["messages"]
        stored.extend([{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}] * 8)
        before = [dict(message) for message in stored]
        calls = chat.call_count

        def no_summary(_older):
            raise AssertionError("the size check must not summarize with a model")

        monkeypatch.setattr(router, "generate_conversation_summary", no_summary)
        response = _size_check(client, "long", "next question")
        assert response.status_code == 200 and response.json()["prompt_tokens"] > 0
        assert chat.call_count == calls and router.CONVERSATIONS["long"]["messages"] == before


def test_size_check_requires_a_loaded_inventory_and_a_known_model(router_factory):
    _, client, _ = router_factory()
    assert _size_check(client, None, "hi").status_code == 409
    with respx.mock(assert_all_called=False) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        assert _size_check(client, None, "hi", model="missing:latest").status_code == 400
    assert client.post("/chat/size-check", json={"prompt": "hi", "node_id": "node-test", "model": MODEL_ID}).status_code == 401
