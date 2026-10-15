"""DL-TOOLUX-01: run context, error budget, continue nudge, run recording and path matches."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import respx

from conftest import TEST_NODE_URL
from davellm_files import search_files
from davellm_run_context import (
    CONTINUE_NUDGE, RUN_CONTEXT_MAX_ENTRIES, folder_map, needs_continue_nudge, tool_run_context,
    with_run_context,
)
from hostile_fs import SECRET_FILES
from test_agent_lifecycle_api import AUTH, inventory, new_run, settled
from test_notion_tools import _persistent_loop

EDT = timezone(timedelta(hours=-4), "EDT")
NOW = datetime(2026, 10, 12, 19, 50, tzinfo=EDT)


def reply(content="", tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"message": message, "done": True})


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def conversation(client):
    return client.post("/conversations/from_template", headers=AUTH, json={
        "template_name": "general",
    }).json()["conversation_id"]


# A4: context ---------------------------------------------------------------------------------

def test_context_names_date_zone_nodes_and_rules_for_the_selected_tools(tmp_path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    text = tool_run_context(now=NOW, roots=[tmp_path], node_ids=["dominic", "walter"],
                            selected_tools=["cluster.status"])
    assert text.startswith("Tool run context from DaveLLM, not from the user.")
    assert "Now: Monday 2026-10-12 19:50 EDT (UTC-04:00)." in text
    assert "Cluster node ids: dominic, walter." in text
    assert "Files:" not in text and "Look before you guess" in text
    files_only = tool_run_context(now=NOW, roots=[tmp_path], node_ids=["dominic"], selected_tools=["file.search"])
    assert "notes.md" in files_only and "Cluster node ids" not in files_only
    everything = tool_run_context(now=NOW, roots=[tmp_path], node_ids=["dominic"], selected_tools=[])
    assert "notes.md" in everything and "Cluster node ids: dominic." in everything
    several = tool_run_context(now=NOW, roots=[tmp_path, tmp_path / "b"], node_ids=[], selected_tools=[])
    assert "give absolute paths inside one of" in several and "notes.md" not in several


def test_folder_map_lists_only_admitted_root_relative_names(hostile_tree):
    text = folder_map(hostile_tree.root)
    assert "README.md" in text and "docs/ (guide.md, nested/)" in text and "src/ (app.py)" in text
    for hidden in (".env", ".ssh", ".aws", ".gnupg", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "server.pem",
                   "server.key", "client.p12", "credentials", "known_hosts", "pubring", "node_modules", ".git",
                   "venv", "outside", str(hostile_tree.base)):
        assert hidden not in text, hidden


def test_folder_map_is_bounded(tmp_path):
    for index in range(RUN_CONTEXT_MAX_ENTRIES + 30):
        (tmp_path / f"file{index:03}.txt").write_text("x", encoding="utf-8")
    lines = folder_map(tmp_path).splitlines()
    assert lines[-1] == "(more entries not shown)"
    assert len(lines) <= RUN_CONTEXT_MAX_ENTRIES + 1


def test_context_goes_after_the_leading_system_messages():
    messages = [{"role": "system", "content": "primary"}, {"role": "user", "content": "hi"}]
    assert with_run_context(messages, "ctx") == [
        {"role": "system", "content": "primary"}, {"role": "system", "content": "ctx"},
        {"role": "user", "content": "hi"},
    ]
    assert with_run_context([{"role": "user", "content": "hi"}], "ctx")[0] == {"role": "system", "content": "ctx"}


def test_nudge_only_after_a_silent_step_that_followed_tool_results():
    after_tool = [{"role": "user", "content": "q"}, {"role": "tool", "content": "{}"}]
    assert needs_continue_nudge(after_tool, {"message": {"role": "assistant", "content": "  "}})
    assert not needs_continue_nudge(after_tool, {"message": {"role": "assistant", "content": "Answer"}})
    assert not needs_continue_nudge(after_tool, {"message": {"content": "", "tool_calls": [{"id": "x"}]}})
    assert not needs_continue_nudge([{"role": "user", "content": "q"}], {"message": {"content": ""}})


def test_run_sends_context_after_primary_and_defaults_error_budget_to_four(router_factory, tmp_path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    _, client, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    conversation_id = conversation(client)
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=reply("Done."))
        created = new_run(client, conversation_id=conversation_id, selected_tools=["file.read"])
        assert created.status_code == 200, created.text
        final = settled(client, created.json()["run_id"])
    sent = json.loads(route.calls.last.request.content)["messages"]
    assert sent[0]["role"] == "system" and "Tool run context" not in sent[0]["content"]
    assert sent[1]["role"] == "system" and sent[1]["content"].startswith("Tool run context from DaveLLM")
    assert "notes.md" in sent[1]["content"] and str(tmp_path) not in sent[1]["content"]
    assert sent[-1] == {"role": "user", "content": "Say done"}
    assert final["snapshot"]["budget"]["errors"] == 4


# A7: continue nudge ---------------------------------------------------------------------------

def test_continue_nudge_asks_once_after_a_silent_step(router_factory):
    _, client, _ = router_factory(tools=True)
    with respx.mock(assert_all_called=True) as mock, _persistent_loop(client):
        inventory(client, mock)
        route = mock.post(f"{TEST_NODE_URL}/api/chat").mock(side_effect=[
            reply(tool_calls=[call("call_info", "system.info", {})]), reply(""), reply("All set."),
        ])
        created = new_run(client, selected_tools=["system.info"])
        final = settled(client, created.json()["run_id"])
    assert final["status"] == "completed" and final["snapshot"]["last_content"] == "All set."
    assert len(route.calls) == 3 and final["snapshot"]["steps"] == 2
    nudged = json.loads(route.calls[2].request.content)["messages"]
    assert nudged[-1] == {"role": "system", "content": CONTINUE_NUDGE}
    assert CONTINUE_NUDGE not in json.dumps(final["snapshot"]["transcript"])


# B1: recording a run in its conversation ------------------------------------------------------

def test_completed_run_records_question_and_answer_once(router_factory):
    router, client, _ = router_factory(tools=True)
    conversation_id = conversation(client)
    with respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        mock.post(f"{TEST_NODE_URL}/api/chat").mock(return_value=reply("Done."))
        run_id = new_run(client, conversation_id=conversation_id).json()["run_id"]
        settled(client, run_id)
    recorded = client.post(f"/tools/agent/runs/{run_id}/record", headers=AUTH)
    assert recorded.status_code == 200 and recorded.json()["messages"] == 2
    assert router.CONVERSATIONS[conversation_id]["messages"][-2:] == [
        {"role": "user", "content": "Say done"}, {"role": "assistant", "content": "Done."},
    ]
    again = client.post(f"/tools/agent/runs/{run_id}/record", headers=AUTH)
    assert again.json() == {"recorded": False, "reason": "already_recorded"}
    assert len(router.CONVERSATIONS[conversation_id]["messages"]) == 2
    assert client.post(f"/tools/agent/runs/{run_id}/record").status_code == 401
    assert client.post("/tools/agent/runs/run_missing/record", headers=AUTH).status_code == 404


def test_record_refuses_unfinished_or_unattached_runs_and_failed_runs_keep_only_the_question(
    router_factory, tmp_path,
):
    router, client, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    conversation_id = conversation(client)
    missing = str(tmp_path / "missing.txt")
    with respx.mock(assert_all_called=True) as mock, _persistent_loop(client):
        inventory(client, mock)
        route = mock.post(f"{TEST_NODE_URL}/api/chat")
        route.mock(return_value=reply("Done."))
        unattached = new_run(client).json()["run_id"]
        settled(client, unattached)
        route.mock(return_value=reply(tool_calls=[call("call_write", "file.write", {"path": missing, "content": "x"})]))
        paused = new_run(client, conversation_id=conversation_id, selected_tools=["file.write"]).json()["run_id"]
        assert settled(client, paused)["status"] == "approval_required"
        route.mock(return_value=reply("I will look first.", [
            call("call_a", "file.read", {"path": missing}), call("call_b", "file.read", {"path": missing}),
        ]))
        failed = new_run(client, conversation_id=conversation_id, selected_tools=["file.read"],
                         error_budget=2).json()["run_id"]
        assert settled(client, failed)["status"] == "error_budget"
    assert client.post(f"/tools/agent/runs/{unattached}/record", headers=AUTH).status_code == 409
    assert client.post(f"/tools/agent/runs/{paused}/record", headers=AUTH).status_code == 409
    assert client.post(f"/tools/agent/runs/{failed}/record", headers=AUTH).json()["messages"] == 1
    assert router.CONVERSATIONS[conversation_id]["messages"] == [{"role": "user", "content": "Say done"}]


# A5: path matches ------------------------------------------------------------------------------

def test_file_search_returns_matching_paths_and_never_protected_names(hostile_tree):
    root = hostile_tree.root
    (root / "docs" / "TOOLS100_SETUP.md").write_text("# Switching on tools\n", encoding="utf-8")

    def resolve(path):
        return (root / path).resolve()

    found = search_files({"query": "setup"}, resolve=resolve, roots=[root])
    assert found["path_matches"] == ["docs/TOOLS100_SETUP.md"]
    assert [match["path"] for match in found["matches"]] == ["docs/guide.md"]
    for query in ("env", "id_", "ssh", "pem"):
        shown = search_files({"query": query}, resolve=resolve, roots=[root])["path_matches"]
        assert not any(secret in shown for secret in SECRET_FILES), (query, shown)
