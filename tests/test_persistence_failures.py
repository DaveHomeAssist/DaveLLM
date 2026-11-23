"""Backlog: report persistence failures before confirming saved changes.

Every saver once caught the write error, printed a warning and returned, so a
request whose change never reached disk still answered 200 (or streamed a
normal completion). A disk that refuses writes is simulated by pointing a
store file under a regular file, so opening its temporary file fails with
NotADirectoryError, an OSError like ENOSPC or EACCES.
"""

import json

import pytest
import respx

from test_api_contracts import AUTH, MODEL_ID, chat_body, mock_inventory
from conftest import TEST_NODE_URL, ollama_chat_ndjson


def refuse_writes(router, data_dir, monkeypatch, *names):
    blocker = data_dir / "not-a-folder"
    blocker.write_text("")
    for name in names:
        monkeypatch.setattr(router, name, blocker / f"{name.lower()}.json")


@pytest.mark.parametrize("saver, store", [
    ("save_conversations", "DATA_FILE"),
    ("save_projects", "PROJECTS_FILE"),
    ("save_settings", "SETTINGS_FILE"),
])
def test_savers_raise_instead_of_returning_normally(router_factory, monkeypatch, saver, store):
    router, _, data_dir = router_factory()
    refuse_writes(router, data_dir, monkeypatch, store)
    with pytest.raises(router.PersistenceError):
        getattr(router, saver)({"k": "v"})


def test_saver_leaves_no_partial_temporary_file(router_factory, monkeypatch, tmp_path):
    router, _, _ = router_factory()
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(router, "DATA_FILE", store / "conversations.json")
    real_replace = type(store).replace

    def refuse_rename(self, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(type(store), "replace", refuse_rename)
    with pytest.raises(router.PersistenceError):
        router.save_conversations({"k": "v"})
    monkeypatch.setattr(type(store), "replace", real_replace)
    assert sorted(item.name for item in store.iterdir()) == []


def test_settings_change_is_not_acknowledged_when_disk_refuses(router_factory, monkeypatch):
    router, client, data_dir = router_factory()
    refuse_writes(router, data_dir, monkeypatch, "SETTINGS_FILE")
    response = client.delete("/instructions/global", headers=AUTH)
    assert response.status_code == 503
    assert response.json() == {"detail": router.PERSISTENCE_FAILED}


def test_project_and_conversation_changes_are_not_acknowledged_when_disk_refuses(router_factory, monkeypatch):
    router, client, data_dir = router_factory()
    conversation_id = client.post(
        "/conversations/from_template", headers=AUTH, json={"template_name": "general"},
    ).json()["conversation_id"]
    refuse_writes(router, data_dir, monkeypatch, "DATA_FILE", "PROJECTS_FILE")

    cleared = client.post(f"/conversations/{conversation_id}/clear", headers=AUTH)
    assert cleared.status_code == 503
    assert cleared.json() == {"detail": router.PERSISTENCE_FAILED}

    created = client.post("/projects", headers=AUTH, json={"name": "Unsaved"})
    assert created.status_code == 503
    assert created.json() == {"detail": router.PERSISTENCE_FAILED}


def test_stream_reports_a_reply_that_could_not_be_saved(router_factory, monkeypatch):
    router, client, data_dir = router_factory()
    with respx.mock(assert_all_called=True) as mock:
        mock_inventory(mock)
        client.get("/nodes/node-test/models", headers=AUTH)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(
            return_value=ollama_chat_ndjson(
                {"message": {"role": "assistant", "content": "Hello"}, "done": True, "done_reason": "stop"},
            )
        )
        refuse_writes(router, data_dir, monkeypatch, "DATA_FILE")
        response = client.post("/chat/stream", headers=AUTH, json=chat_body("unsaved", model=MODEL_ID))

    assert response.status_code == 200
    events = [json.loads(line[len("data: "):]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert {"error": router.PERSISTENCE_FAILED, "done": True} in events
    assert not any("stats" in event for event in events)
    assert [event["event"] for event in router.RECENT_ERRORS][-1:] == ["stream_finalize"]
