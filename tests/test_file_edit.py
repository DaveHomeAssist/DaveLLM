"""PR-06: file.edit, the one extended tool that writes, and only after exact-call approval.

Every test runs against the disposable hostile tree from tests/hostile_fs.py.
Refusals prove zero effects with a snapshot of the whole base directory, and
the lifecycle tests drive real approvals and rejections through
/tools/agent/runs against a mocked Ollama node.
"""

import asyncio
import json
import os
import shutil
import stat
import time

import httpx
import pytest
import respx

import davellm_edit
from conftest import TEST_API_KEY, TEST_NODE_URL
from daveharness import run_tool
from davellm_edit import (
    COUNT_MISMATCH, EDIT_FAILED, FILE_CHANGED, MULTIPLE_LINKS, NO_CHANGE, TEMP_PREFIX, TEXT_NOT_FOUND,
)
from davellm_files import (
    FILE_NOT_FOUND, NOT_A_REGULAR_FILE, NOT_TEXT, PATH_NOT_ALLOWED, READ_MAX_FILE_BYTES, FileTooLarge,
)
from hostile_fs import BINARY_FILE, OUTSIDE_LINK, sentinel
from tool_contract import PathToolContract, assert_path_contract, run_path_contract


AUTH = {"X-API-Key": TEST_API_KEY}
MODEL = "inventory-model:latest"
TOO_LARGE = str(FileTooLarge(READ_MAX_FILE_BYTES))


@pytest.fixture
def editor(router_factory, monkeypatch, hostile_tree):
    """Load app with tools and extended tools on, rooted at the hostile tree."""
    def load(extended="true"):
        monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", extended)
        router, client, _ = router_factory(tools=True, tool_roots=[str(hostile_tree.root)])
        return router, client

    return load


def run(router, **arguments):
    return asyncio.run(run_tool("file.edit", arguments, registry=router.TOOL_REGISTRY))


def ok(router, **arguments):
    execution = run(router, **arguments)
    assert execution.status == "success", execution.error
    return json.loads(execution.result)


def temporaries(tree):
    return [path for path in tree.base.rglob(f"{TEMP_PREFIX}*")]


def refused_without_effects(router, tree, message, **arguments):
    before = tree.snapshot()
    execution = run(router, **arguments)
    assert execution.status == "error", execution.result
    assert execution.error == message
    assert tree.snapshot() == before
    assert not tree.leaked(execution.result, execution.error)
    return execution


# Registration ---------------------------------------------------------------------------------

def test_file_edit_is_an_approval_required_bounded_write_behind_the_extended_flag(editor):
    router, _ = editor()
    for registry in (router.TOOL_REGISTRY, router.HARNESS_REGISTRY):
        definition = registry.get("file.edit")
        assert definition.permission == "write_files"
        assert definition.approval_required is True
        assert definition.cancellation == "bounded"
        assert definition.parameters["required"] == ["path", "old_text", "new_text"]
        assert definition.parameters["additionalProperties"] is False
    for flag in ("false", ""):
        router, _ = editor(extended=flag)
        for registry in (router.TOOL_REGISTRY, router.HARNESS_REGISTRY):
            assert "file.edit" not in registry.public_catalog()


def test_file_edit_is_the_only_extended_tool_that_writes_or_needs_approval(editor):
    router, _ = editor()
    writers = {
        definition.name for definition in router.extended_tool_definitions()
        if definition.approval_required or definition.permission not in {"read", "read_files", "read_system"}
    }
    assert writers == {"file.edit"}


# Edits that succeed ---------------------------------------------------------------------------

def test_edit_replaces_one_exact_occurrence_and_reports_the_change(editor, hostile_tree):
    router, _ = editor()
    target = hostile_tree.at("docs/plan.md")
    target.write_text("# Plan\n\nstatus: draft\nowner: dave\n", encoding="utf-8")
    before = hostile_tree.snapshot()
    result = ok(router, path="docs/plan.md", old_text="status: draft", new_text="status: final")
    assert result == {
        "path": "docs/plan.md", "replacements": 1, "first_changed_line": 3, "line_endings": "lf",
        "bytes_before": 34, "bytes_after": 34,
    }
    assert target.read_text(encoding="utf-8") == "# Plan\n\nstatus: final\nowner: dave\n"
    after = hostile_tree.snapshot()
    assert {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)} == {
        "root/docs/plan.md",
    }
    assert temporaries(hostile_tree) == []


def test_expected_count_must_match_before_every_occurrence_is_replaced(editor, hostile_tree):
    router, _ = editor()
    target = hostile_tree.at("src/config.py")
    target.write_text("debug = True\nverbose = True\ncache = True\n", encoding="utf-8")
    refused_without_effects(
        router, hostile_tree, COUNT_MISMATCH.format(found=3, expected=1),
        path=str(target), old_text="True", new_text="False",
    )
    refused_without_effects(
        router, hostile_tree, COUNT_MISMATCH.format(found=3, expected=2),
        path=str(target), old_text="True", new_text="False", expected_count=2,
    )
    result = ok(router, path=str(target), old_text="True", new_text="False", expected_count=3)
    assert result["replacements"] == 3 and result["first_changed_line"] == 1
    assert target.read_text(encoding="utf-8") == "debug = False\nverbose = False\ncache = False\n"
    # An explicit null means the default count, as the schema allows.
    ok(router, path=str(target), old_text="cache = False", new_text="cache = None", expected_count=None)


def test_crlf_files_match_lf_text_and_keep_crlf(editor, hostile_tree):
    router, _ = editor()
    target = hostile_tree.at("docs/windows.txt")
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = ok(router, path=str(target), old_text="one\ntwo", new_text="1\n2\n2.5")
    assert result["line_endings"] == "crlf" and result["first_changed_line"] == 1
    assert target.read_bytes() == b"1\r\n2\r\n2.5\r\nthree\r\n"
    ok(router, path=str(target), old_text="2.5\r\n", new_text="")
    assert target.read_bytes() == b"1\r\n2\r\nthree\r\n"


def test_mixed_line_endings_are_matched_exactly_as_they_are(editor, hostile_tree):
    router, _ = editor()
    target = hostile_tree.at("docs/mixed.txt")
    target.write_bytes(b"a\r\nb\nc\n")
    refused_without_effects(router, hostile_tree, TEXT_NOT_FOUND, path=str(target), old_text="a\nb", new_text="x")
    result = ok(router, path=str(target), old_text="b\nc", new_text="B\nC")
    assert result["line_endings"] == "lf"
    assert target.read_bytes() == b"a\r\nB\nC\n"


def test_byte_order_mark_permission_bits_and_empty_replacement(editor, hostile_tree):
    router, _ = editor()
    target = hostile_tree.at("docs/bom.txt")
    target.write_bytes("﻿keep\nremove me\nkeep too\n".encode("utf-8"))
    target.chmod(0o640)
    result = ok(router, path=str(target), old_text="remove me\n", new_text="")
    assert result["first_changed_line"] == 2
    assert target.read_bytes() == "﻿keep\nkeep too\n".encode("utf-8")
    assert target.stat().st_mode & 0o777 == 0o640


def test_the_original_is_replaced_by_rename_not_rewritten_in_place(editor, hostile_tree):
    router, _ = editor()
    target = hostile_tree.at("docs/guide.md")
    original = target.read_bytes()
    with open(target, "rb") as reader:
        ok(router, path=str(target), old_text="Run the thing.", new_text="Run the other thing.")
        assert reader.read() == original  # an open reader keeps the whole old file
    assert target.read_text(encoding="utf-8").endswith("Run the other thing.\n")


# Refusals write nothing -----------------------------------------------------------------------

def test_content_refusals_write_nothing(editor, hostile_tree):
    router, _ = editor()
    readme = str(hostile_tree.at("README.md"))
    refused_without_effects(router, hostile_tree, TEXT_NOT_FOUND, path=readme, old_text="absent", new_text="x")
    refused_without_effects(router, hostile_tree, NO_CHANGE, path=readme, old_text="Ordinary", new_text="Ordinary")
    refused_without_effects(
        router, hostile_tree, NOT_TEXT, path=str(hostile_tree.at(BINARY_FILE)), old_text="PNG", new_text="GIF",
    )
    latin1 = hostile_tree.at("docs/latin1.txt")
    latin1.write_bytes("café\n".encode("latin-1"))
    refused_without_effects(router, hostile_tree, NOT_TEXT, path=str(latin1), old_text="caf", new_text="tea")


def test_non_files_and_missing_files_are_refused_without_effects(editor, hostile_tree):
    router, _ = editor()
    refused_without_effects(
        router, hostile_tree, NOT_A_REGULAR_FILE, path=str(hostile_tree.at("docs")), old_text="a", new_text="b",
    )
    refused_without_effects(
        router, hostile_tree, FILE_NOT_FOUND, path=str(hostile_tree.at("docs/new.md")), old_text="a", new_text="b",
    )
    refused_without_effects(
        router, hostile_tree, FILE_NOT_FOUND, path=str(hostile_tree.at("missing/new.md")), old_text="a",
        new_text="b",
    )
    # A snapshot would block reading the pipe, so this case checks the folder by hand.
    fifo = hostile_tree.at("data/pipe")
    os.mkfifo(fifo)
    listing = sorted(os.listdir(fifo.parent))
    execution = run(router, path=str(fifo), old_text="a", new_text="b")
    assert (execution.status, execution.error) == ("error", NOT_A_REGULAR_FILE)
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode) and sorted(os.listdir(fifo.parent)) == listing


def test_hard_links_are_refused_so_no_other_name_is_left_stale(editor, hostile_tree):
    router, _ = editor()
    target = hostile_tree.at("docs/linked.txt")
    target.write_text("shared text\n", encoding="utf-8")
    os.link(target, hostile_tree.at("docs/other-name.txt"))
    refused_without_effects(router, hostile_tree, MULTIPLE_LINKS, path=str(target), old_text="shared", new_text="x")
    outside_twin = hostile_tree.at("docs/outside-twin.txt")
    os.link(hostile_tree.outside / "notes.txt", outside_twin)
    execution = refused_without_effects(
        router, hostile_tree, MULTIPLE_LINKS, path=str(outside_twin), old_text="hostile", new_text="x",
    )
    assert sentinel("outside/notes.txt") not in execution.error


def test_files_too_large_before_or_after_the_edit_are_refused(editor, hostile_tree):
    router, _ = editor()
    large = hostile_tree.at("data/too-large.txt")
    large.write_bytes(b"a" * READ_MAX_FILE_BYTES + b"x")
    refused_without_effects(router, hostile_tree, TOO_LARGE, path=str(large), old_text="x", new_text="y")
    large.write_bytes(b"a" * (READ_MAX_FILE_BYTES - 1) + b"x")
    refused_without_effects(router, hostile_tree, TOO_LARGE, path=str(large), old_text="x", new_text="yy")
    assert temporaries(hostile_tree) == []


def test_paths_outside_the_root_or_denylisted_are_refused_before_any_read(editor, hostile_tree):
    router, _ = editor()
    for path in (
        str(hostile_tree.outside / "notes.txt"),
        f"{hostile_tree.root}/docs/../../outside/notes.txt",
        "../outside/notes.txt",
        str(hostile_tree.at(OUTSIDE_LINK)),
        str(hostile_tree.at(".env")),
        "id_ed25519",
        ".ssh/config",
        "certs/server.key",
    ):
        refused_without_effects(router, hostile_tree, PATH_NOT_ALLOWED, path=path, old_text="hostile", new_text="x")


def test_file_edit_passes_the_shared_hostile_tree_contract(editor, hostile_tree):
    router, _ = editor()
    # ".\n" occurs exactly once in every allowed file, and still once after each edit.
    contract = PathToolContract(
        tool="file.edit",
        arguments=lambda path: {"path": path, "old_text": ".\n", "new_text": ".\n\n"},
        mutates=True,
        oversized=lambda tree: {
            "path": str(tree.at("README.md")), "old_text": "x" * (davellm_edit.EDIT_MAX_TEXT_CHARS + 1),
            "new_text": "",
        },
        invalid=(
            {},
            {"path": "README.md", "old_text": ".\n"},
            {"path": "README.md", "old_text": "", "new_text": "x"},
            {"path": "README.md", "old_text": ".\n", "new_text": "y" * (davellm_edit.EDIT_MAX_TEXT_CHARS + 1)},
            {"path": "README.md", "old_text": ".\n", "new_text": "x", "expected_count": 0},
            {"path": "README.md", "old_text": ".\n", "new_text": "x", "expected_count": 101},
            {"path": "README.md", "old_text": ".\n", "new_text": "x", "expected_count": "1"},
            {"path": "README.md", "old_text": ".\n", "new_text": "x", "create": True},
        ),
    )
    assert_path_contract(contract, hostile_tree, run_path_contract(router.TOOL_REGISTRY, contract, hostile_tree))
    assert temporaries(hostile_tree) == []


# Time of check to time of use -----------------------------------------------------------------

def racing(monkeypatch, change):
    """Run ``change`` right after the new content is staged, before the final check and rename."""
    write_temp = davellm_edit._write_temp

    def staged(directory, data, mode):
        name = write_temp(directory, data, mode)
        change()
        return name

    monkeypatch.setattr(davellm_edit, "_write_temp", staged)


def test_a_file_rewritten_during_the_edit_keeps_the_new_writer_content(editor, hostile_tree, monkeypatch):
    router, _ = editor()
    target = hostile_tree.at("docs/race.txt")
    target.write_text("value = 1\n", encoding="utf-8")
    racing(monkeypatch, lambda: target.write_text("value = 1\nwritten by someone else\n", encoding="utf-8"))
    execution = run(router, path=str(target), old_text="value = 1", new_text="value = 2")
    assert (execution.status, execution.error) == ("error", FILE_CHANGED)
    assert target.read_text(encoding="utf-8") == "value = 1\nwritten by someone else\n"
    assert temporaries(hostile_tree) == []


def test_a_file_replaced_with_identical_bytes_is_still_a_different_file(editor, hostile_tree, monkeypatch):
    router, _ = editor()
    target = hostile_tree.at("docs/race.txt")
    target.write_text("value = 1\n", encoding="utf-8")
    twin = hostile_tree.base / "twin.txt"
    twin.write_text("value = 1\n", encoding="utf-8")
    racing(monkeypatch, lambda: os.replace(twin, target))
    execution = run(router, path=str(target), old_text="value = 1", new_text="value = 2")
    assert (execution.status, execution.error) == ("error", FILE_CHANGED)
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert temporaries(hostile_tree) == []


def test_a_file_swapped_for_a_symlink_during_the_edit_is_left_alone(editor, hostile_tree, monkeypatch):
    router, _ = editor()
    target = hostile_tree.at("docs/race.txt")
    target.write_text("hostile\n", encoding="utf-8")
    outside = hostile_tree.outside / "notes.txt"
    outside_before = outside.read_bytes()

    def swap():
        target.unlink()
        target.symlink_to(outside)

    racing(monkeypatch, swap)
    execution = run(router, path=str(target), old_text="hostile", new_text="edited")
    assert (execution.status, execution.error) == ("error", FILE_CHANGED)
    assert target.is_symlink() and os.readlink(target) == str(outside)
    assert outside.read_bytes() == outside_before
    assert temporaries(hostile_tree) == []


def test_a_folder_swapped_for_a_symlink_after_admission_is_never_entered(editor, hostile_tree, monkeypatch):
    router, _ = editor()
    folder = hostile_tree.at("notes")
    folder.mkdir()
    (folder / "notes.txt").write_text("hostile\n", encoding="utf-8")
    outside_before = (hostile_tree.outside / "notes.txt").read_bytes()
    admit = router.resolve_extended_tool_path

    def admit_then_swap(path):
        admitted = admit(path)
        shutil.rmtree(folder)
        folder.symlink_to(hostile_tree.outside, target_is_directory=True)
        return admitted

    monkeypatch.setattr(router, "resolve_extended_tool_path", admit_then_swap)
    execution = run(router, path="notes/notes.txt", old_text="hostile", new_text="edited")
    assert (execution.status, execution.error) == ("error", PATH_NOT_ALLOWED)
    assert (hostile_tree.outside / "notes.txt").read_bytes() == outside_before
    assert sorted(path.name for path in hostile_tree.outside.iterdir()) == ["notes.txt", "secret.txt"]


def test_a_file_swapped_for_a_symlink_after_admission_is_never_read(editor, hostile_tree, monkeypatch):
    router, _ = editor()
    target = hostile_tree.at("docs/swap.txt")
    target.write_text("hostile\n", encoding="utf-8")
    outside = hostile_tree.outside / "notes.txt"
    outside_before = outside.read_bytes()
    admit = router.resolve_extended_tool_path

    def admit_then_swap(path):
        admitted = admit(path)
        target.unlink()
        target.symlink_to(outside)
        return admitted

    monkeypatch.setattr(router, "resolve_extended_tool_path", admit_then_swap)
    execution = run(router, path="docs/swap.txt", old_text="hostile", new_text="edited")
    assert (execution.status, execution.error) == ("error", PATH_NOT_ALLOWED)
    assert target.is_symlink() and outside.read_bytes() == outside_before
    assert temporaries(hostile_tree) == []


def test_a_planted_temporary_name_is_never_followed(editor, hostile_tree, monkeypatch):
    router, _ = editor()
    monkeypatch.setattr(davellm_edit.secrets, "token_hex", lambda _size: "0" * 16)
    outside = hostile_tree.outside / "notes.txt"
    outside_before = outside.read_bytes()
    hostile_tree.at(f"docs/{TEMP_PREFIX}{'0' * 16}.tmp").symlink_to(outside)
    before = hostile_tree.snapshot()
    execution = run(router, path="docs/guide.md", old_text="Run the thing.", new_text="Run it.")
    assert (execution.status, execution.error) == ("error", EDIT_FAILED)
    assert hostile_tree.snapshot() == before and outside.read_bytes() == outside_before


def test_a_failed_rename_reports_a_fixed_message_and_leaves_no_temporary(editor, hostile_tree, monkeypatch):
    router, _ = editor()
    target = hostile_tree.at("docs/guide.md")
    before = hostile_tree.snapshot()

    def failing_rename(*_args, **_kwargs):
        raise OSError(28, f"No space left on device: {hostile_tree.root}")

    monkeypatch.setattr(davellm_edit.os, "rename", failing_rename)
    execution = run(router, path=str(target), old_text="Run the thing.", new_text="Run it.")
    assert (execution.status, execution.error) == ("error", EDIT_FAILED)
    assert str(hostile_tree.root) not in execution.error
    assert hostile_tree.snapshot() == before


# Approval through the lifecycle routes -------------------------------------------------------

def inventory(client, mock):
    mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(
        200, json={"models": [{"name": MODEL, "model": MODEL}]},
    ))
    assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200


def edit_call(arguments):
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": "Editing",
        "tool_calls": [{"id": "call_edit", "type": "function", "function": {
            "name": "file.edit", "arguments": json.dumps(arguments),
        }}],
    }}]})


def paused_edit(client, mock, arguments):
    """Start a run whose model asks for ``arguments``; return the run id, pending call, and model route."""
    inventory(client, mock)
    route = mock.post(f"{TEST_NODE_URL}/v1/chat/completions")
    route.side_effect = [
        edit_call(arguments),
        httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "Finished"}}]}),
    ]
    created = client.post("/tools/agent/runs", headers=AUTH, json={
        "messages": [{"role": "user", "content": "Mark the plan final"}], "node_id": "node-test", "model": MODEL,
    })
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    for _ in range(100):
        state = client.get(f"/tools/agent/runs/{run_id}", headers=AUTH).json()
        if state["status"] not in {"created", "running"}:
            break
        time.sleep(0.01)
    assert state["status"] == "approval_required", state
    return run_id, state["snapshot"]["pending_call"], route


def decide(client, run_id, pending, decision):
    body = {key: pending[key] for key in ("call_id", "digest", "definition_fingerprint", "permission", "nonce")}
    return client.post(f"/tools/agent/runs/{run_id}/decisions", headers=AUTH, json={**body, "decision": decision})


def tool_messages(route):
    return [message for message in json.loads(route.calls[-1].request.content)["messages"]
            if message.get("role") == "tool"]


def test_an_approved_edit_writes_exactly_the_change_the_card_showed(editor, hostile_tree):
    router, client = editor()
    target = hostile_tree.at("docs/plan.md")
    target.write_text("status: draft\n", encoding="utf-8")
    arguments = {"path": "docs/plan.md", "old_text": "status: draft", "new_text": "status: final"}
    with respx.mock(assert_all_called=True) as mock:
        run_id, pending, route = paused_edit(client, mock, arguments)
        assert pending["tool_name"] == "file.edit"
        assert pending["arguments"] == arguments
        assert pending["permission"] == "write_files"
        assert target.read_text(encoding="utf-8") == "status: draft\n"  # nothing before approval
        approved = decide(client, run_id, pending, "approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "completed"
    assert target.read_text(encoding="utf-8") == "status: final\n"
    assert len(route.calls) == 2
    [result] = tool_messages(route)
    envelope = json.loads(result["content"])
    assert envelope["status"] == "success"
    assert json.loads(envelope["result"]) == {
        "path": "docs/plan.md", "replacements": 1, "first_changed_line": 1, "line_endings": "lf",
        "bytes_before": 14, "bytes_after": 14,
    }
    assert temporaries(hostile_tree) == []


def test_a_rejected_edit_writes_nothing(editor, hostile_tree):
    router, client = editor()
    target = hostile_tree.at("docs/plan.md")
    target.write_text("status: draft\n", encoding="utf-8")
    with respx.mock(assert_all_called=False) as mock:
        run_id, pending, route = paused_edit(
            client, mock, {"path": "docs/plan.md", "old_text": "status: draft", "new_text": "status: final"},
        )
        before = hostile_tree.snapshot()
        rejected = decide(client, run_id, pending, "reject")
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "approval_rejected"
        assert decide(client, run_id, pending, "approve").status_code == 409
    assert hostile_tree.snapshot() == before
    assert len(route.calls) == 1


def test_a_file_changed_after_the_request_is_checked_again_when_approved(editor, hostile_tree):
    router, client = editor()
    target = hostile_tree.at("docs/plan.md")
    target.write_text("status: draft\n", encoding="utf-8")
    with respx.mock(assert_all_called=True) as mock:
        run_id, pending, route = paused_edit(
            client, mock, {"path": "docs/plan.md", "old_text": "status: draft", "new_text": "status: final"},
        )
        target.write_text("status: reviewed by a person\n", encoding="utf-8")
        before = hostile_tree.snapshot()
        approved = decide(client, run_id, pending, "approve")
        assert approved.status_code == 200, approved.text
    assert hostile_tree.snapshot() == before
    assert target.read_text(encoding="utf-8") == "status: reviewed by a person\n"
    [result] = tool_messages(route)
    assert TEXT_NOT_FOUND in result["content"]


def test_an_approved_edit_outside_the_root_still_writes_nothing(editor, hostile_tree):
    router, client = editor()
    outside = hostile_tree.outside / "notes.txt"
    with respx.mock(assert_all_called=True) as mock:
        run_id, pending, route = paused_edit(
            client, mock, {"path": str(outside), "old_text": "hostile", "new_text": "edited"},
        )
        before = hostile_tree.snapshot()
        approved = decide(client, run_id, pending, "approve")
        assert approved.status_code == 200, approved.text
    assert hostile_tree.snapshot() == before
    [result] = tool_messages(route)
    assert PATH_NOT_ALLOWED in result["content"]
    assert not hostile_tree.leaked(json.dumps(approved.json()), result["content"])
