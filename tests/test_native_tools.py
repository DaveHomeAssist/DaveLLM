"""PR-05: DaveLLM native read tools behind DAVE_ENABLE_EXTENDED_TOOLS.

project.notepad.read, project.brain.read, project.artifacts, chat.search, and
cluster.status take their scope from the run binding in HOST_RUN_CONTEXT,
never from arguments. Tests call them through DaveHarness's validated
dispatcher with a binding in place, and through the /tools/agent/runs route
with a scripted Ollama transport.
"""

import asyncio
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from conftest import TEST_API_KEY, TEST_NODE_URL
from daveharness import run_tool
from davellm_files import OUTPUT_BUDGET_BYTES
from davellm_native_tools import (
    ARTIFACT_NOT_FOUND, BRAIN_MAX_CHARS, BRAIN_UNAVAILABLE, NOTEPAD_MAX_CHARS, PROJECT_RUN_REQUIRED,
    PROJECT_UNAVAILABLE, RUN_REQUIRED, SNIPPET_CHARS,
)
from test_agent_lifecycle_api import AUTH, MODEL, inventory, new_run, settled


NATIVE_TOOLS = {"project.notepad.read", "project.brain.read", "project.artifacts", "chat.search", "cluster.status"}
PROJECT_TOOLS = ("project.notepad.read", "project.brain.read", "project.artifacts")
OTHER_USER = "bob"
IP_NODE_URL = "http://100.64.0.7:11434"
LEAKS = ("user_id", "project_id", "conversation_id", "system_prompt", "snapshot_json", "brain_digest",
         ".db", "dave_", "http://", "https://", "11434", "100.64", "ollama.test", TEST_API_KEY)


@pytest.fixture
def native(router_factory, monkeypatch):
    monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", "true")
    return router_factory(tools=True)


def make_project(router, client, *, user="default", name="Launch plan", notepad=""):
    if user == "default":
        project_id = client.post("/projects", headers=AUTH, json={
            "name": name, "system_prompt": f"Hidden instructions for {name}",
        }).json()["project_id"]
    else:
        project_id = f"proj_{uuid.uuid4().hex}"
        router.PROJECTS[project_id] = {
            "name": name, "system_prompt": f"Hidden instructions for {name}", "user_id": user,
            "notepad": "", "context_budget_tokens": 16_384, "archived": False, "created_at": "2026-01-01",
        }
        router.get_project(project_id, user)
    router.PROJECTS[project_id]["notepad"] = notepad
    router.PROJECTS[project_id]["notepad_updated_at"] = "2026-09-25T10:00:00"
    return project_id


def binding(router, *, user="default", project_id=None, capture=True, **overrides):
    """A run binding as /tools/agent/runs builds it, with the BRAIN revision captured now."""
    revision = digest = None
    if project_id and capture:
        brain = router.PROJECT_CONTEXT.get_brain(project_id)
        revision, digest = brain["revision"], router.PROJECT_CONTEXT.brain_digest(project_id, brain)
    fields = dict(user_id=user, node_url="http://node.invalid", model="scripted", max_tokens=16,
                  temperature=0.0, project_id=project_id, brain_revision=revision, brain_digest=digest,
                  context_budget={})
    fields.update(overrides)
    return router.HostRunBinding(**fields)


def call(router, scope, name, **arguments):
    token = router.HOST_RUN_CONTEXT.set(scope)
    try:
        return asyncio.run(run_tool(name, arguments, registry=router.HARNESS_REGISTRY))
    finally:
        router.HOST_RUN_CONTEXT.reset(token)


def ok(router, scope, name, **arguments):
    execution = call(router, scope, name, **arguments)
    assert execution.status == "success", execution.error
    return json.loads(execution.result)


def refused(router, scope, name, message, **arguments):
    execution = call(router, scope, name, **arguments)
    assert (execution.status, execution.error) == ("error", message), (name, arguments, execution.result)


def invalid(router, scope, name, **arguments):
    assert call(router, scope, name, **arguments).status == "validation_error", (name, arguments)


def conversation(router, *, user="default", title="Planning chat", messages=(), system_prompt=""):
    conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
    router.CONVERSATIONS[conversation_id] = {
        "user_id": user, "title": title, "system_prompt": system_prompt,
        "messages": [{"role": role, "content": content} for role, content in messages],
        "created_at": "2026-09-25T09:00:00", "updated_at": "2026-09-25T09:30:00",
    }
    for index, (role, content) in enumerate(messages):
        router.store_message_embedding(conversation_id, index, role, content)
    return conversation_id


def assert_private(text, data_dir):
    for leak in (*LEAKS, str(data_dir)):
        assert leak not in text, leak


# Registration ---------------------------------------------------------------------------------

def test_native_tools_register_only_with_both_flags(router_factory, monkeypatch):
    monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", "true")
    router, _, _ = router_factory(tools=False)
    assert not NATIVE_TOOLS & set(router.TOOL_REGISTRY.public_catalog())
    monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", "false")
    router, _, _ = router_factory(tools=True)
    assert not NATIVE_TOOLS & set(router.TOOL_REGISTRY.public_catalog())
    assert not NATIVE_TOOLS & set(router.HARNESS_REGISTRY.public_catalog())
    monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", "true")
    router, _, _ = router_factory(tools=True)
    for registry in (router.TOOL_REGISTRY, router.HARNESS_REGISTRY):
        assert NATIVE_TOOLS <= set(registry.public_catalog())


def test_native_definitions_have_the_intended_permissions(native):
    router, _, _ = native
    for name in NATIVE_TOOLS:
        definition = router.HARNESS_REGISTRY.get(name)
        expected = "read_system" if name == "cluster.status" else "read"
        assert (definition.permission, definition.approval_required) == (expected, False)
        assert (definition.cancellation, definition.async_handler, definition.context_handler) == (
            "bounded", False, False)
        assert definition.parameters["additionalProperties"] is False
        assert not {"project", "project_id", "user", "user_id", "owner", "node", "url"} & set(
            definition.parameters["properties"])


# Run context ----------------------------------------------------------------------------------

def test_project_scoped_run_reads_its_own_project(native):
    router, client, _ = native
    project_id = make_project(router, client, notepad="Launch on Tuesday.")
    scope = binding(router, project_id=project_id)
    assert ok(router, scope, "project.notepad.read")["notepad"] == "Launch on Tuesday."
    assert ok(router, scope, "project.brain.read")["revision"] == 1
    assert ok(router, scope, "project.artifacts")["artifacts"] == []


def test_project_tools_fail_closed_without_a_project_run(native):
    router, client, _ = native
    make_project(router, client, notepad="Should never be read.")  # the only, most recent project
    for name in PROJECT_TOOLS:
        refused(router, None, name, PROJECT_RUN_REQUIRED)
        refused(router, binding(router), name, PROJECT_RUN_REQUIRED)
    refused(router, None, "chat.search", RUN_REQUIRED, query="anything")


def test_forged_scope_arguments_are_rejected(native):
    router, client, _ = native
    mine = make_project(router, client, name="Mine")
    theirs = make_project(router, client, user=OTHER_USER, name="Theirs", notepad="Bob's secret")
    scope = binding(router, project_id=mine)
    for name in PROJECT_TOOLS:
        for forged in ({"project_id": theirs}, {"project": theirs}, {"user_id": OTHER_USER}, {"owner": OTHER_USER}):
            invalid(router, scope, name, **forged)
    invalid(router, scope, "chat.search", query="secret", user_id=OTHER_USER)
    invalid(router, scope, "cluster.status", node="node-test")
    assert "Bob's secret" not in json.dumps(ok(router, scope, "project.notepad.read"))


def test_another_users_project_is_refused(native):
    router, client, _ = native
    theirs = make_project(router, client, user=OTHER_USER, name="Theirs", notepad="Bob's secret")
    router.PROJECT_CONTEXT.add_artifact(theirs, title="Bob's plan", body="Bob's artifact body")
    stolen = binding(router, project_id=theirs, capture=True)  # a binding that names someone else's project
    for name in PROJECT_TOOLS:
        refused(router, stolen, name, PROJECT_UNAVAILABLE)
        refused(router, binding(router, project_id="proj_missing", capture=False), name, PROJECT_UNAVAILABLE)
    assert ok(router, binding(router, user=OTHER_USER, project_id=theirs), "project.notepad.read")["notepad"] == (
        "Bob's secret")


def test_one_project_cannot_read_another(native):
    router, client, _ = native
    first = make_project(router, client, name="First", notepad="first notes")
    second = make_project(router, client, name="Second", notepad="second notes")
    foreign = router.PROJECT_CONTEXT.add_artifact(second, title="Second plan", body="second body")
    router.PROJECT_CONTEXT.update_brain(second, pinned_text="second brain", expected_revision=1)
    scope = binding(router, project_id=first)
    assert ok(router, scope, "project.notepad.read")["notepad"] == "first notes"
    assert "second" not in json.dumps(ok(router, scope, "project.brain.read"))
    refused(router, scope, "project.artifacts", ARTIFACT_NOT_FOUND, artifact=foreign["artifact_id"])
    assert ok(router, scope, "project.artifacts")["artifacts"] == []


# project.notepad.read -------------------------------------------------------------------------

def test_notepad_returns_its_text(native):
    router, client, _ = native
    project_id = make_project(router, client, name="Launch plan", notepad="Line one.\nLine two.")
    notepad = ok(router, binding(router, project_id=project_id), "project.notepad.read")
    assert notepad == {"project": "Launch plan", "notepad": "Line one.\nLine two.", "character_count": 19,
                       "updated_at": "2026-09-25T10:00:00", "truncated": False}


def test_empty_notepad_is_a_normal_result(native):
    router, client, _ = native
    project_id = make_project(router, client)
    notepad = ok(router, binding(router, project_id=project_id), "project.notepad.read")
    assert (notepad["notepad"], notepad["character_count"], notepad["truncated"]) == ("", 0, False)


def test_large_notepad_is_truncated(native):
    router, client, _ = native
    project_id = make_project(router, client, notepad="n" * (NOTEPAD_MAX_CHARS + 5_000))
    execution = call(router, binding(router, project_id=project_id), "project.notepad.read")
    notepad = json.loads(execution.result)
    assert len(notepad["notepad"]) == NOTEPAD_MAX_CHARS and notepad["truncated"] is True
    assert notepad["character_count"] == NOTEPAD_MAX_CHARS + 5_000
    escaped = make_project(router, client, notepad="\x01" * NOTEPAD_MAX_CHARS)  # 6 bytes each when encoded
    result = call(router, binding(router, project_id=escaped), "project.notepad.read").result
    assert len(result.encode()) <= OUTPUT_BUDGET_BYTES and json.loads(result)["truncated"] is True


def test_notepad_result_has_no_internal_fields(native):
    router, client, data_dir = native
    project_id = make_project(router, client, notepad="Plain notes.")
    execution = call(router, binding(router, project_id=project_id), "project.notepad.read")
    assert set(json.loads(execution.result)) == {"project", "notepad", "character_count", "updated_at", "truncated"}
    assert_private(execution.result, data_dir)


# project.brain.read ---------------------------------------------------------------------------

def test_brain_read_returns_the_captured_revision(native):
    router, client, _ = native
    project_id = make_project(router, client)
    router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="Pinned A", active_text="Active A",
                                       recent_text="Recent A", expected_revision=1)
    brain = ok(router, binding(router, project_id=project_id), "project.brain.read")
    assert (brain["revision"], brain["pinned"], brain["active"], brain["recent"], brain["deleted"]) == (
        2, "Pinned A", "Active A", "Recent A", False)
    assert brain["character_count"] == {"pinned": 8, "active": 8, "recent": 8}


def test_brain_read_stays_on_the_run_revision_after_the_brain_changes(native):
    router, client, _ = native
    project_id = make_project(router, client)
    router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="Decision A", expected_revision=1)
    scope = binding(router, project_id=project_id)  # the run starts on revision 2
    router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="Decision B", expected_revision=2)
    router.PROJECT_CONTEXT.compact_brain(project_id)
    assert router.PROJECT_CONTEXT.get_brain(project_id)["revision"] > 2
    brain = ok(router, scope, "project.brain.read")
    assert (brain["revision"], brain["pinned"]) == (2, "Decision A")


def test_brain_read_inside_a_real_run_uses_the_revision_captured_at_start(native):
    router, client, _ = native
    project_id = make_project(router, client)
    router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="Decision A", expected_revision=1)
    # One event loop for the whole test, so the run keeps going between requests.
    with client, respx.mock(assert_all_called=True) as mock:
        inventory(client, mock)
        route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions")

        def respond(request):
            messages = json.loads(request.content)["messages"]
            if messages[-1]["role"] != "tool":
                # The BRAIN moves on after the run started but before the tool runs.
                router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="Decision B", expected_revision=2)
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "", "tool_calls": [{
                        "id": "call_brain", "type": "function",
                        "function": {"name": "project.brain.read", "arguments": "{}"},
                    }],
                }}]})
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Done"}}]})

        route.side_effect = respond
        created = new_run(client, project_id=project_id)
        assert created.status_code == 200, created.text
        final = settled(client, created.json()["run_id"])
    assert final["status"] == "completed" and final["context"]["brain_revision"] == 2
    tool_message = json.loads(route.calls[1].request.content)["messages"][-1]
    brain = json.loads(json.loads(tool_message["content"])["result"])
    assert (brain["revision"], brain["pinned"]) == (2, "Decision A")
    assert router.PROJECT_CONTEXT.get_brain(project_id)["pinned_text"] == "Decision B"


def test_missing_or_altered_brain_revision_fails_safely(native):
    router, client, _ = native
    project_id = make_project(router, client)
    for scope in (binding(router, project_id=project_id, capture=False),
                  binding(router, project_id=project_id, brain_revision=99, brain_digest="0" * 64),
                  binding(router, project_id=project_id, brain_revision=1, brain_digest="0" * 64)):
        refused(router, scope, "project.brain.read", BRAIN_UNAVAILABLE)
    router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="Kept", expected_revision=1)
    scope = binding(router, project_id=project_id)  # captured revision 2
    router.PROJECT_CONTEXT.delete_project(project_id)  # removes every stored revision; only 1 is recreated
    refused(router, scope, "project.brain.read", BRAIN_UNAVAILABLE)


def test_brain_output_is_capped(native):
    router, client, _ = native
    project_id = make_project(router, client)
    router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="p" * 20_000, active_text="a" * 20_000,
                                       recent_text="r" * 20_000, compact_threshold=262_144, expected_revision=1)
    execution = call(router, binding(router, project_id=project_id), "project.brain.read")
    brain = json.loads(execution.result)
    assert len(execution.result.encode()) <= OUTPUT_BUDGET_BYTES and brain["truncated"] is True
    assert len(brain["pinned"]) + len(brain["active"]) + len(brain["recent"]) <= BRAIN_MAX_CHARS
    assert brain["pinned"] == "p" * 20_000 and brain["character_count"]["recent"] == 20_000


# project.artifacts ----------------------------------------------------------------------------

def test_artifacts_are_listed(native):
    router, client, _ = native
    project_id = make_project(router, client)
    first = router.PROJECT_CONTEXT.add_artifact(project_id, title="Release notes", body="v1 notes", kind="note")
    router.PROJECT_CONTEXT.add_artifact(project_id, title="Pinned plan", body="plan", pinned=True)
    listing = ok(router, binding(router, project_id=project_id), "project.artifacts")
    assert (listing["count"], listing["total"], listing["truncated"]) == (2, 2, False)
    assert [entry["title"] for entry in listing["artifacts"]] == ["Pinned plan", "Release notes"]
    assert set(listing["artifacts"][1]) == {"artifact", "title", "kind", "pinned", "created_at", "updated_at"}
    assert listing["artifacts"][1]["artifact"] == first["artifact_id"] and listing["artifacts"][1]["kind"] == "note"


def test_empty_artifact_list(native):
    router, client, _ = native
    project_id = make_project(router, client)
    listing = ok(router, binding(router, project_id=project_id), "project.artifacts", max_entries=None)
    assert listing == {"project": "Launch plan", "artifacts": [], "count": 0, "total": 0, "truncated": False}


def test_one_artifact_is_read(native):
    router, client, _ = native
    project_id = make_project(router, client)
    record = router.PROJECT_CONTEXT.add_artifact(project_id, title="Runbook", body="Step 1\nStep 2")
    artifact = ok(router, binding(router, project_id=project_id), "project.artifacts", artifact=record["artifact_id"])
    assert (artifact["title"], artifact["text"], artifact["character_count"], artifact["truncated"]) == (
        "Runbook", "Step 1\nStep 2", 13, False)


def test_unknown_artifacts_are_not_found(native):
    router, client, _ = native
    project_id = make_project(router, client)
    scope = binding(router, project_id=project_id)
    for wanted in ("artifact_missing", "../../dave_project_context.db", "' OR 1=1 --", "x" * 100):
        refused(router, scope, "project.artifacts", ARTIFACT_NOT_FOUND, artifact=wanted)
    invalid(router, scope, "project.artifacts", artifact="x" * 101)


def test_artifacts_of_other_projects_and_users_are_refused(native):
    router, client, _ = native
    mine = make_project(router, client)
    theirs = make_project(router, client, user=OTHER_USER, name="Theirs")
    foreign = router.PROJECT_CONTEXT.add_artifact(theirs, title="Bob's plan", body="Bob's artifact body")
    refused(router, binding(router, project_id=mine), "project.artifacts", ARTIFACT_NOT_FOUND,
            artifact=foreign["artifact_id"])


def test_artifact_results_expose_no_storage_details(native):
    router, client, data_dir = native
    project_id = make_project(router, client)
    record = router.PROJECT_CONTEXT.add_artifact(project_id, title="Notes", body="Body text",
                                                 conversation_id="conv_internal", source_message_index=3)
    scope = binding(router, project_id=project_id)
    for execution in (call(router, scope, "project.artifacts"),
                      call(router, scope, "project.artifacts", artifact=record["artifact_id"])):
        assert_private(execution.result, data_dir)
        assert "source_message_index" not in execution.result and "conv_internal" not in execution.result


def test_artifact_results_are_capped(native):
    router, client, _ = native
    project_id = make_project(router, client)
    for number in range(60):
        router.PROJECT_CONTEXT.add_artifact(project_id, title=f"Artifact {number}", body=f"body {number}")
    big = router.PROJECT_CONTEXT.add_artifact(project_id, title="Big", body="b" * 50_000)
    scope = binding(router, project_id=project_id)
    default = ok(router, scope, "project.artifacts")
    assert (default["count"], default["total"], default["truncated"]) == (20, 61, True)
    assert ok(router, scope, "project.artifacts", max_entries=50)["count"] == 50
    invalid(router, scope, "project.artifacts", max_entries=51)
    execution = call(router, scope, "project.artifacts", artifact=big["artifact_id"])
    body = json.loads(execution.result)
    assert body["truncated"] is True and body["character_count"] == 50_000
    assert len(execution.result.encode()) <= OUTPUT_BUDGET_BYTES


# chat.search ----------------------------------------------------------------------------------

def test_chat_search_finds_owned_conversations(native):
    router, client, _ = native
    wanted = conversation(router, title="Budget chat", messages=(
        ("user", "What is the orchid budget?"), ("assistant", "The orchid budget is 42 dollars.")))
    conversation(router, title="Other chat", messages=(("user", "Unrelated gardening question"),))
    found = ok(router, binding(router), "chat.search", query="orchid budget")
    assert found["count"] >= 1 and {item["conversation"] for item in found["results"]} == {wanted}
    assert any("42 dollars" in item["snippet"] for item in found["results"])
    assert set(found["results"][0]) == {"conversation", "title", "role", "snippet"}
    assert found["results"][0]["title"] == "Budget chat"


def test_chat_search_returns_at_most_ten_results(native):
    router, client, _ = native
    for number in range(12):
        conversation(router, title=f"Kestrel {number}", messages=(("user", f"kestrel report {number}"),))
    scope = binding(router)
    assert ok(router, scope, "chat.search", query="kestrel report")["count"] == 5
    assert ok(router, scope, "chat.search", query="kestrel report", max_results=10)["count"] == 10
    invalid(router, scope, "chat.search", query="kestrel report", max_results=11)
    invalid(router, scope, "chat.search", query="k" * 201)
    invalid(router, scope, "chat.search", query="")


def test_chat_search_never_returns_other_users_conversations(native):
    router, client, _ = native
    conversation(router, user=OTHER_USER, title="Bob's chat", messages=(
        ("user", "The heron vault code is 7781."), ("assistant", "Noted: heron vault code 7781.")))
    mine = conversation(router, title="My chat", messages=(("user", "Where is the heron vault?"),))
    for query in ("heron vault code", "heron vault", "7781"):
        found = ok(router, binding(router), "chat.search", query=query)
        assert "7781" not in json.dumps(found["results"]) and "Bob" not in json.dumps(found["results"])
        assert {item["conversation"] for item in found["results"]} <= {mine}
    assert ok(router, binding(router, user=OTHER_USER), "chat.search", query="7781")["count"] >= 1


def test_chat_search_never_returns_hidden_or_system_messages(native):
    router, client, data_dir = native
    conversation(router, title="Prompted chat", system_prompt="Secret system rule: lynx-55", messages=(
        ("system", "Hidden system prompt says lynx-55"), ("tool", "Tool payload lynx-55"),
        ("user", "hello there"),))
    for query in ("lynx-55", "Hidden system prompt", "Secret system rule", "Tool payload"):
        found = ok(router, binding(router), "chat.search", query=query)
        assert found["results"] == [], query
    assert_private(json.dumps(ok(router, binding(router), "chat.search", query="hello")), data_dir)


def test_chat_search_snippets_are_bounded(native):
    router, client, _ = native
    text = "filler " * 400 + "the marigold answer is 9 " + "filler " * 400
    conversation(router, messages=(("assistant", text),))
    found = ok(router, binding(router), "chat.search", query="marigold answer")
    snippet = found["results"][0]["snippet"]
    assert len(snippet) <= SNIPPET_CHARS and "marigold answer" in snippet


def test_chat_search_with_no_matches(native):
    router, client, _ = native
    conversation(router, messages=(("user", "Nothing relevant here"),))
    assert ok(router, binding(router), "chat.search", query="zeppelin quartz") == {
        "query": "zeppelin quartz", "results": [], "count": 0}


def test_search_route_behaviour_is_unchanged(native):
    router, client, _ = native
    conversation(router, title="Route chat", messages=(("system", "route phrase alpha"), ("user", "route phrase alpha")))
    results = client.get("/search", params={"query": "route phrase alpha"}, headers=AUTH).json()
    assert {item["role"] for item in results} == {"system", "user"}


# cluster.status -------------------------------------------------------------------------------

def nodes_with_an_ip_node(router):
    router.NODE_CONFIGS.append(router.NodeConfig(id="node-ip", name="Walter", url=IP_NODE_URL))


def test_cluster_status_reports_reachable_and_unreachable_nodes(native):
    router, _, _ = native
    nodes_with_an_ip_node(router)
    router.MODEL_INVENTORY["node-test"] = {"qwen2.5:3b", "llama3:8b"}
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(200, json={"models": []}))
        mock.get(f"{IP_NODE_URL}/api/tags").mock(side_effect=httpx.ConnectError("refused by 100.64.0.7"))
        execution = call(router, None, "cluster.status")
    status = json.loads(execution.result)
    assert status["count"] == 2
    first, second = status["nodes"]
    assert (first["node"], first["name"], first["reachable"]) == ("node-test", "Test Ollama", True)
    assert first["models"] == ["llama3:8b", "qwen2.5:3b"] and isinstance(first["latency_ms"], float)
    assert (second["node"], second["name"], second["reachable"], second["latency_ms"], second["models"]) == (
        "node-ip", "Walter", False, None, None)


def test_cluster_status_never_exposes_addresses_or_credentials(native):
    router, _, data_dir = native
    nodes_with_an_ip_node(router)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(500, text="boom at ollama.test"))
        mock.get(f"{IP_NODE_URL}/api/tags").mock(side_effect=httpx.ConnectTimeout("timeout to 100.64.0.7:11434"))
        execution = call(router, binding(router), "cluster.status")
    assert execution.status == "success"
    assert_private(execution.result, data_dir)
    for node in json.loads(execution.result)["nodes"]:
        assert set(node) == {"node", "name", "reachable", "latency_ms", "models", "models_truncated"}


def test_cluster_status_cannot_target_other_hosts(native):
    router, _, _ = native
    for arguments in ({"node": "node-test"}, {"url": "http://169.254.169.254/"}, {"host": "evil.test"}):
        invalid(router, None, "cluster.status", **arguments)
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(200, json={"models": []}))
        assert call(router, None, "cluster.status").status == "success"  # any other request would fail here


# Global ---------------------------------------------------------------------------------------

def _state(folder):
    """Logical content of every store: SQLite databases dumped, other files hashed.

    SQLite's WAL and shared-memory files change on reads and checkpoints, so
    databases are compared by content rather than by bytes.
    """
    state = {}
    for path in sorted(Path(folder).rglob("*")):
        if not path.is_file() or path.name.endswith(("-wal", "-shm", "-journal")):
            continue
        if path.suffix == ".db":
            with sqlite3.connect(path) as connection:
                state[path.name] = "\n".join(connection.iterdump())
        else:
            state[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


def test_native_reads_are_bounded_private_and_change_nothing(native):
    router, client, data_dir = native
    project_id = make_project(router, client, notepad="notes " * 5_000)
    router.PROJECT_CONTEXT.update_brain(project_id, pinned_text="brain " * 3_000, expected_revision=1)
    record = router.PROJECT_CONTEXT.add_artifact(project_id, title="Doc", body="doc " * 20_000)
    conversation(router, messages=(("user", "plover detail"), ("assistant", "the plover detail is 5")))
    scope = binding(router, project_id=project_id)
    router.save_conversations(router.CONVERSATIONS)
    router.save_projects(router.PROJECTS)
    before = _state(data_dir)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(200, json={"models": []}))
        executions = [
            call(router, scope, "project.notepad.read"), call(router, scope, "project.brain.read"),
            call(router, scope, "project.artifacts"), call(router, scope, "project.artifacts", artifact=record["artifact_id"]),
            call(router, scope, "chat.search", query="plover detail"), call(router, scope, "cluster.status"),
        ]
    for execution in executions:
        assert execution.status == "success", execution.error
        assert len(execution.result.encode()) <= OUTPUT_BUDGET_BYTES
        assert_private(execution.result, data_dir)
    assert _state(data_dir) == before
