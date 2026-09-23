import json
import sqlite3
from datetime import datetime, timedelta

import httpx
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL


AUTH = {"X-API-Key": TEST_API_KEY}
MODEL_ID = "inventory-model:latest"


def load_inventory(client, mock):
    mock.get(f"{TEST_NODE_URL}/api/tags").mock(
        return_value=httpx.Response(
            200,
            json={"models": [{"name": MODEL_ID, "model": MODEL_ID}]},
        )
    )
    assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200


def create_project(client, **overrides):
    payload = {"name": "Context Project", "system_prompt": "Stay inside the project."}
    payload.update(overrides)
    response = client.post("/projects", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_brain_compaction_preserves_protected_tiers_and_restores_revisions(router_factory):
    router, client, _ = router_factory()
    project = create_project(client)
    project_id = project["project_id"]
    pinned = "Decision: keep this exact byte sequence."
    active = "Goal: ship BRAIN.\nRisk: context overflow."
    recent = "\n".join(
        (
            "Investigate the homepage",
            "Investigate the homepage",
            "[resolved] Old transient failure",
            "[tool] raw call output",
            "Keep the useful recent finding",
        )
    )

    saved = client.put(
        f"/projects/{project_id}/brain",
        headers=AUTH,
        json={
            "pinned_text": pinned,
            "active_text": active,
            "recent_text": recent,
            "compact_threshold": 3072,
            "expected_revision": 1,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    assert saved.json()["compaction_queued"] is False

    compacted = client.post(
        f"/projects/{project_id}/brain/compact",
        headers=AUTH,
    )
    assert compacted.status_code == 200, compacted.text
    body = compacted.json()
    assert body["revision"] == 3
    assert body["pinned_text"] == pinned
    assert body["active_text"] == active
    assert body["recent_text"].count("Investigate the homepage") == 1
    assert "Old transient failure" not in body["recent_text"]
    assert "raw call output" not in body["recent_text"]

    revisions = client.get(
        f"/projects/{project_id}/brain/revisions",
        headers=AUTH,
    ).json()["revisions"]
    assert [item["revision"] for item in revisions] == [3, 2, 1]

    deleted = client.delete(f"/projects/{project_id}/brain", headers=AUTH)
    assert deleted.status_code == 200
    assert deleted.json()["deleted_at"]
    restored = client.post(
        f"/projects/{project_id}/brain/revisions/2/restore",
        headers=AUTH,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["deleted_at"] is None
    assert restored.json()["recent_text"] == recent
    assert restored.json()["pinned_text"] == pinned

    assert client.delete(f"/projects/{project_id}/brain", headers=AUTH).status_code == 200
    expired_at = (datetime.now() - timedelta(days=31)).isoformat()
    with sqlite3.connect(router.PROJECT_CONTEXT_DB) as connection:
        connection.execute(
            "UPDATE brain_states SET deleted_at = ? WHERE project_id = ?",
            (expired_at, project_id),
        )
    assert router.PROJECT_CONTEXT.purge_expired_brains(30) == [project_id]
    expired = client.get(f"/projects/{project_id}/brain", headers=AUTH).json()
    assert expired["revision"] == 1
    assert expired["pinned_text"] == ""
    assert expired["deleted_at"] is None


def test_run_context_captures_one_brain_revision_even_when_brain_changes_during_assembly(router_factory, monkeypatch):
    router, client, _ = router_factory()
    project_id = create_project(client)["project_id"]
    saved = router.PROJECT_CONTEXT.update_brain(
        project_id, pinned_text="Original run decision", expected_revision=1,
    )
    original_assembly = router.PROJECT_CONTEXT.build_context_messages

    def edit_during_assembly(*args, **kwargs):
        router.PROJECT_CONTEXT.update_brain(
            project_id, pinned_text="Later edited decision", expected_revision=2,
        )
        return original_assembly(*args, **kwargs)

    monkeypatch.setattr(router.PROJECT_CONTEXT, "build_context_messages", edit_during_assembly)
    captured = router.PROJECT_CONTEXT.capture_run_context(project_id, query="decision")
    assert captured["brain_revision"] == saved["revision"] == 2
    assert "Original run decision" in captured["messages"][0]["content"]
    assert "Later edited decision" not in captured["messages"][0]["content"]

    monkeypatch.setattr(router.PROJECT_CONTEXT, "build_context_messages", original_assembly)
    current = router.PROJECT_CONTEXT.capture_run_context(project_id, query="decision")
    assert current["brain_revision"] == 3
    assert current["brain_digest"] != captured["brain_digest"]
    assert "Later edited decision" in current["messages"][0]["content"]


def test_project_homepage_files_artifacts_and_context_order(router_factory):
    _, client, data_dir = router_factory()
    project = create_project(client)
    project_id = project["project_id"]

    brain = client.put(
        f"/projects/{project_id}/brain",
        headers=AUTH,
        json={
            "pinned_text": "Use the launch checklist.",
            "active_text": "Goal: verify the context order.",
            "recent_text": "The release candidate is ready.",
            "expected_revision": 1,
        },
    )
    assert brain.status_code == 200, brain.text

    uploaded = client.post(
        f"/projects/{project_id}/files",
        headers=AUTH,
        files={"file": ("launch.md", b"Launch checklist: test, review, deploy.", "text/markdown")},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["status"] == "indexed"
    assert (data_dir / "project_uploads" / project_id).is_dir()
    file_id = uploaded.json()["file_id"]

    detached = client.put(
        f"/projects/{project_id}/files/{file_id}",
        headers=AUTH,
        json={"attached": False},
    )
    assert detached.status_code == 200
    assert detached.json()["attached"] is False
    attached = client.put(
        f"/projects/{project_id}/files/{file_id}",
        headers=AUTH,
        json={"attached": True},
    )
    assert attached.status_code == 200
    reindexed = client.post(
        f"/projects/{project_id}/files/{file_id}/reindex",
        headers=AUTH,
    )
    assert reindexed.status_code == 200
    assert reindexed.json()["status"] == "indexed"

    conversation_id = client.post(
        "/conversations/from_template",
        headers=AUTH,
        json={"template_name": "general", "project_id": project_id},
    ).json()["conversation_id"]

    with respx.mock(assert_all_called=True) as mock:
        load_inventory(client, mock)
        first_chat = mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Verified launch artifact"}}]},
            )
        )
        response = client.post(
            "/chat",
            headers=AUTH,
            json={
                "conversation_id": conversation_id,
                "prompt": "Use the launch checklist",
                "node_id": "node-test",
                "model": MODEL_ID,
                "project_id": project_id,
            },
        )
    assert response.status_code == 200, response.text
    messages = json.loads(first_chat.calls.last.request.content)["messages"]
    assert messages[0]["content"].endswith(
        "PROJECT INSTRUCTIONS\nStay inside the project."
    )
    assert messages[1]["content"].startswith("BRAIN PROJECT CONTEXT")
    assert "Use the launch checklist." in messages[1]["content"]
    assert messages[2]["content"].startswith("PROJECT FILE CONTEXT")
    assert "[File: launch.md#1]" in messages[2]["content"]
    assert messages[-1] == {"role": "user", "content": "Use the launch checklist"}

    homepage = client.get(
        f"/projects/{project_id}/homepage",
        headers=AUTH,
    )
    assert homepage.status_code == 200, homepage.text
    body = homepage.json()
    assert body["context_budget"]["quotas"] == {
        "project_instructions": 4096,
        "brain": 4096,
        "file_context": 4915,
        "artifact_history": 3277,
    }
    artifacts = body["components"]["artifact_history"]
    assert len(artifacts) == 1
    assert artifacts[0]["preview"] == "Verified launch artifact"
    artifact_id = artifacts[0]["artifact_id"]

    preview = client.post(
        f"/projects/{project_id}/context-preview",
        headers=AUTH,
        json={
            "conversation_id": conversation_id,
            "query": "Recall the verified artifact",
            "model": MODEL_ID,
            "max_tokens": 2048,
        },
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    preview_messages = preview_body["messages"]
    assert preview_messages[0]["content"].endswith(
        "PROJECT INSTRUCTIONS\nStay inside the project."
    )
    assert preview_messages[1]["content"].startswith("BRAIN PROJECT CONTEXT")
    assert preview_messages[2]["content"].startswith("PROJECT FILE CONTEXT")
    assert preview_messages[3]["content"].startswith("PROJECT ARTIFACT HISTORY")
    assert preview_messages[-1] == {
        "role": "user",
        "content": "Recall the verified artifact",
    }
    limits = preview_body["budget"]["available_limits"]
    quotas = preview_body["budget"]["quotas"]
    assert limits["brain"] > quotas["brain"]
    assert limits["file_context"] > quotas["file_context"]
    assert limits["artifact_history"] > quotas["artifact_history"]
    assert len(client.get(f"/conversations/{conversation_id}", headers=AUTH).json()["messages"]) == 2

    with respx.mock(assert_all_called=True) as mock:
        load_inventory(client, mock)
        second_chat = mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={"choices": [{"message": {"content": "Second answer"}}]},
            )
        )
        response = client.post(
            "/chat",
            headers=AUTH,
            json={
                "conversation_id": conversation_id,
                "prompt": "Recall the verified artifact",
                "node_id": "node-test",
                "model": MODEL_ID,
                "project_id": project_id,
            },
        )
    assert response.status_code == 200, response.text
    second_messages = json.loads(second_chat.calls.last.request.content)["messages"]
    assert second_messages[1]["content"].startswith("BRAIN PROJECT CONTEXT")
    assert second_messages[2]["content"].startswith("PROJECT FILE CONTEXT")
    assert second_messages[3]["content"].startswith("PROJECT ARTIFACT HISTORY")
    assert "Verified launch artifact" in second_messages[3]["content"]

    opened = client.get(
        f"/projects/{project_id}/artifacts/{artifact_id}",
        headers=AUTH,
    )
    assert opened.status_code == 200
    assert opened.json()["body"] == "Verified launch artifact"
    pinned_artifact = client.put(
        f"/projects/{project_id}/artifacts/{artifact_id}",
        headers=AUTH,
        json={"pinned": True},
    )
    assert pinned_artifact.status_code == 200
    assert pinned_artifact.json()["pinned"] is True
    archived = client.put(
        f"/projects/{project_id}/artifacts/{artifact_id}",
        headers=AUTH,
        json={"archived": True},
    )
    assert archived.status_code == 200
    visible = client.get(f"/projects/{project_id}/artifacts", headers=AUTH).json()["artifacts"]
    assert all(item["artifact_id"] != artifact_id for item in visible)
    all_artifacts = client.get(
        f"/projects/{project_id}/artifacts?include_archived=true",
        headers=AUTH,
    ).json()["artifacts"]
    assert any(item["artifact_id"] == artifact_id for item in all_artifacts)
    assert client.delete(
        f"/projects/{project_id}/artifacts/{artifact_id}",
        headers=AUTH,
    ).status_code == 200
    assert client.delete(
        f"/projects/{project_id}/files/{file_id}",
        headers=AUTH,
    ).status_code == 200
    assert client.get(
        f"/projects/{project_id}/files",
        headers=AUTH,
    ).json()["files"] == []
    assert not any((data_dir / "project_uploads" / project_id).iterdir())


def test_project_attachment_is_explicit_and_records_future_only_event(router_factory):
    _, client, _ = router_factory()
    project = create_project(client)
    conversation_id = client.post(
        "/conversations/from_template",
        headers=AUTH,
        json={"template_name": "general", "project_id": None},
    ).json()["conversation_id"]

    rejected = client.post(
        "/chat",
        headers=AUTH,
        json={
            "conversation_id": conversation_id,
            "prompt": "Do not attach implicitly",
            "node_id": "node-test",
            "model": MODEL_ID,
            "project_id": project["project_id"],
        },
    )
    assert rejected.status_code == 409
    assert "explicit conversation project endpoint" in rejected.json()["detail"]

    attached = client.put(
        f"/conversations/{conversation_id}/project",
        headers=AUTH,
        json={"project_id": project["project_id"]},
    )
    assert attached.status_code == 200, attached.text
    event = attached.json()["context_events"][-1]
    assert event["project_id"] == project["project_id"]
    assert event["applies_to"] == "future_messages_only"

    detached = client.put(
        f"/conversations/{conversation_id}/project",
        headers=AUTH,
        json={"project_id": None},
    )
    assert detached.status_code == 200
    assert detached.json()["project_id"] is None


def test_instruction_and_brain_protected_content_cannot_overflow_allocations(router_factory):
    _, client, _ = router_factory()
    project = create_project(client, context_budget_tokens=1024)
    project_id = project["project_id"]

    instructions = client.put(
        f"/projects/{project_id}",
        headers=AUTH,
        json={"system_prompt": "x" * 1100},
    )
    assert instructions.status_code == 422
    assert "25 percent" in instructions.json()["detail"]

    brain = client.put(
        f"/projects/{project_id}/brain",
        headers=AUTH,
        json={"pinned_text": "p" * 1100, "active_text": "a" * 1100},
    )
    assert brain.status_code == 422
    assert "protected allocation" in brain.json()["detail"]
