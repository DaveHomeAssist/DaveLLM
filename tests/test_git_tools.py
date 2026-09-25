"""PR-04: read-only git.status, git.diff, git.log, and git.show behind DAVE_ENABLE_EXTENDED_TOOLS.

Most tests run against the hostile repositories from tests/hostile_git.py,
whose configuration tries to run a planted program on every read path. The
``hostile_git`` fixture fails any test after which a planted program ran.
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import davellm_git
from daveharness import run_tool
from daveharness.registry import DEFAULT_TOOL_TIMEOUT_SECONDS
from davellm_files import OUTPUT_BUDGET_BYTES, PATH_NOT_ALLOWED
from davellm_git import (
    FILE_NOT_AT_REVISION, GIT_FAILED, GIT_TIMED_OUT, INVALID_FILE, INVALID_REVISION, MODE_CONFLICT,
    NOT_A_FILE_AT_REVISION, NOT_A_WORK_TREE, REVISION_NOT_FOUND, TO_WITHOUT_FROM, UNSUPPORTED_REPOSITORY,
    UNTRUSTED_REPOSITORY, check_revision,
)
from hostile_git import SECRET_TEXT, UNIQUE_FACT
from tool_contract import PathToolContract, assert_path_contract, run_path_contract


GIT_TOOLS = {"git.status", "git.diff", "git.log", "git.show"}
EARLIER_EXTENDED = {"file.list", "file.search", "file.read_lines", "md.outline", "md.section"}
FIXED_ERRORS = {
    PATH_NOT_ALLOWED, NOT_A_WORK_TREE, UNSUPPORTED_REPOSITORY, UNTRUSTED_REPOSITORY, INVALID_REVISION,
    REVISION_NOT_FOUND, INVALID_FILE, FILE_NOT_AT_REVISION, NOT_A_FILE_AT_REVISION, MODE_CONFLICT,
    TO_WITHOUT_FROM, GIT_TIMED_OUT, GIT_FAILED, "File is not UTF-8 text",
}
INJECTIONS = (
    "-p", "--output=pwned.txt", "--exec=touch pwned", "-O/tmp/pwned", "--upload-pack=touch pwned",
    "HEAD --output=pwned.txt", "HEAD\n--output=pwned.txt", "@{-1}", "HEAD@{0}", "HEAD:.env",
    "HEAD^{tree}", "main..feature", "main...feature", ":/Initial", "$(touch pwned)", "`touch pwned`",
    "HEAD;touch pwned", "a b", "refs/heads/-x", "main/..", "a.lock", "~1", "",
)


@pytest.fixture
def extended(router_factory, monkeypatch, hostile_git):
    def load(*roots, extended="true", tools=True):
        monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", extended)
        router, _, _ = router_factory(
            tools=tools, tool_roots=[str(item) for item in roots or [hostile_git.root]],
        )
        return router

    return load


@pytest.fixture
def router(extended):
    return extended()


def run(router, name, **arguments):
    return asyncio.run(run_tool(name, arguments, registry=router.TOOL_REGISTRY))


def ok(router, name, **arguments):
    execution = run(router, name, **arguments)
    assert execution.status == "success", execution.error
    return json.loads(execution.result)


def error(router, name, **arguments):
    execution = run(router, name, **arguments)
    assert execution.status != "success", execution.result
    return execution.status, execution.error


def refused(router, name, message, **arguments):
    assert error(router, name, **arguments) == ("error", message), arguments


def paths(entries):
    return [item["path"] if isinstance(item, dict) else item for item in entries]


# Registration ---------------------------------------------------------------------------------

def test_git_tools_register_only_with_both_flags(extended):
    assert not GIT_TOOLS & set(extended(extended="true", tools=False).TOOL_REGISTRY.public_catalog())
    assert not GIT_TOOLS & set(extended(extended="false").TOOL_REGISTRY.public_catalog())
    router = extended()
    assert GIT_TOOLS | EARLIER_EXTENDED <= {definition.name for definition in router.extended_tool_definitions()}
    for registry in (router.TOOL_REGISTRY, router.HARNESS_REGISTRY):
        assert GIT_TOOLS <= set(registry.public_catalog())


def test_git_definitions_are_read_only_synchronous_and_bounded(router):
    for name in GIT_TOOLS:
        definition = router.TOOL_REGISTRY.get(name)
        assert (definition.permission, definition.approval_required) == ("read_files", False)
        assert (definition.cancellation, definition.async_handler, definition.context_handler) == (
            "bounded", False, False)
        assert definition.timeout_seconds == DEFAULT_TOOL_TIMEOUT_SECONDS > davellm_git.GIT_TIMEOUT_SECONDS
        assert definition.parameters["additionalProperties"] is False


def test_git_schemas_reject_bad_arguments_and_accept_null_defaults(router):
    bad = {
        "git.status": ({"path": ""}, {"path": 1}, {"path": "repo", "args": ["-p"]}),
        "git.diff": ({"path": "repo", "staged": "yes"}, {"path": "repo", "from_revision": ""},
                     {"path": "repo", "from_revision": "x" * 129}, {"path": "repo", "extra": "--cached"}),
        "git.log": ({"path": "repo", "limit": 0}, {"path": "repo", "limit": 51}, {"path": "repo", "limit": "5"},
                    {"path": "repo", "file": ""}, {"path": "repo", "file": "x" * 1025}),
        "git.show": ({"path": "repo"}, {"path": "repo", "revision": ""}, {"path": "repo", "revision": 5},
                     {"path": "repo", "revision": "HEAD", "format": "%H"}),
    }
    for name, cases in bad.items():
        for arguments in cases:
            assert error(router, name, **arguments)[0] == "validation_error", (name, arguments)
    assert ok(router, "git.diff", path="repo", staged=None, from_revision=None, to_revision=None) == ok(
        router, "git.diff", path="repo")
    assert ok(router, "git.log", path="repo", limit=None, file=None) == ok(router, "git.log", path="repo")


# Repository boundary --------------------------------------------------------------------------

def test_ordinary_repository_is_accepted(router, extended, hostile_git):  # 1
    assert ok(router, "git.status", path="repo")["path"] == "repo"
    assert ok(router, "git.status", path=str(hostile_git.at("repo")))["path"] == "repo"
    assert ok(router, "git.status", path="repo/docs")["path"] == "repo"  # a subfolder names its repository
    single = ok(extended(hostile_git.at("repo")), "git.status")  # the tool root is the repository
    assert single["path"] == "." and single["branch"] == "main"


def test_non_repository_is_rejected(router):  # 2
    for path in ("plain", ".", "plain/file.txt", "vendor/embedded.git", "missing"):
        status, message = error(router, "git.status", path=path)
        assert (status, message) in {("error", NOT_A_WORK_TREE), ("error", PATH_NOT_ALLOWED)}, path
    refused(router, "git.status", NOT_A_WORK_TREE, path="plain")
    refused(router, "git.status", NOT_A_WORK_TREE, path="vendor/embedded.git")


def test_repository_outside_the_root_is_rejected(router, extended, hostile_git):  # 3
    for path in (str(hostile_git.outside / "repo"), "../outside/repo", "repo/../../outside/repo"):
        refused(router, "git.log", PATH_NOT_ALLOWED, path=path)
    nested = extended(hostile_git.at("repo/docs"))  # a tool root inside someone else's repository
    refused(nested, "git.status", NOT_A_WORK_TREE)
    refused(nested, "git.log", NOT_A_WORK_TREE, path=".")


def test_git_directory_outside_the_root_is_rejected(router):  # 4
    for name, extra in (("git.status", {}), ("git.log", {}), ("git.show", {"revision": "HEAD"})):
        refused(router, name, PATH_NOT_ALLOWED, path="symgit", **extra)  # .git is a symlink to outside
        # core.worktree points outside, so the folder inside the root is not the working tree.
        refused(router, name, NOT_A_WORK_TREE, path="escaped", **extra)


def test_dot_git_file_pointing_outside_is_rejected(router, hostile_git):  # 5
    assert (hostile_git.at("linked") / ".git").is_file()
    for name, extra in (("git.status", {}), ("git.diff", {}), ("git.show", {"revision": "HEAD"})):
        refused(router, name, PATH_NOT_ALLOWED, path="linked", **extra)


def test_symlinked_repository_escape_is_rejected(router):  # 6
    for name, extra in (("git.status", {}), ("git.log", {}), ("git.show", {"revision": "HEAD", "file": "secret.txt"})):
        execution = run(router, name, path="link-repo", **extra)
        assert (execution.status, execution.error) == ("error", PATH_NOT_ALLOWED)
        assert "outside repository" not in execution.result


def test_alternate_object_stores_are_refused(router):
    refused(router, "git.log", UNSUPPORTED_REPOSITORY, path="alternates")


def test_results_and_errors_never_show_absolute_paths(router, hostile_git):  # 7
    calls = [
        ("git.status", {"path": str(hostile_git.at("repo"))}), ("git.status", {"path": "big"}),
        ("git.diff", {"path": str(hostile_git.at("repo/docs"))}), ("git.diff", {"path": "repo", "staged": True}),
        ("git.log", {"path": str(hostile_git.at("history")), "limit": 50}),
        ("git.show", {"path": "repo", "revision": "HEAD~2"}),
        ("git.show", {"path": "repo", "revision": "v1.0", "file": "README.md"}),
        ("git.status", {"path": "linked"}), ("git.status", {"path": "plain"}),
        ("git.show", {"path": "repo", "revision": "nope"}), ("git.show", {"path": "promisor", "revision": "HEAD"}),
        ("git.log", {"path": str(hostile_git.outside / "repo")}),
    ]
    for name, arguments in calls:
        execution = run(router, name, **arguments)
        text = f"{execution.result}{execution.error}"
        assert str(hostile_git.base) not in text and "/tmp" not in text, (name, arguments)


# Hardened runner ------------------------------------------------------------------------------

def exercise_all(router, repo="repo"):
    """Every read mode against one repository; returns the executions."""
    calls = [
        ("git.status", {}), ("git.diff", {}), ("git.diff", {"staged": True}),
        ("git.diff", {"from_revision": "v1.0", "to_revision": "HEAD"}), ("git.log", {"limit": 50}),
        ("git.log", {"file": "README.md"}), ("git.show", {"revision": "HEAD"}),
        ("git.show", {"revision": "HEAD~2"}), ("git.show", {"revision": "HEAD", "file": "README.md"}),
    ]
    return [run(router, name, path=repo, **arguments) for name, arguments in calls]


def test_external_diff_programs_never_run(router, hostile_git, monkeypatch):  # 8
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(hostile_git.planted("env-external-diff")))
    executions = exercise_all(router)
    assert all(execution.status == "success" for execution in executions)
    assert "diff --git a/README.md b/README.md" in json.loads(executions[1].result)["diff"]
    assert hostile_git.fired() == []


def test_textconv_never_runs(router, hostile_git):  # 9
    shown = ok(router, "git.show", path="repo", revision="HEAD~1")
    assert "+LOADED = True" in shown["diff"]  # raw text, not a converter's output
    exercise_all(router)
    assert "textconv" not in hostile_git.fired()


def test_fsmonitor_never_runs(router, hostile_git, monkeypatch):  # 10
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hostile_git.planted("env-fsmonitor")))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.fsmonitor'='{hostile_git.planted('env-fsmonitor')}'")
    for repo in ("repo", "tracking", "big"):
        ok(router, "git.status", path=repo)
        ok(router, "git.diff", path=repo)
    assert hostile_git.fired() == []


def test_pager_never_runs(router, hostile_git, monkeypatch):  # 11
    monkeypatch.setenv("PAGER", str(hostile_git.planted("env-pager")))
    monkeypatch.setenv("GIT_PAGER", str(hostile_git.planted("env-pager")))
    exercise_all(router)
    assert hostile_git.fired() == []


def test_no_credential_prompt_or_network_access(router, hostile_git, monkeypatch):  # 12
    for variable, name in (("GIT_ASKPASS", "env-askpass"), ("SSH_ASKPASS", "env-askpass"),
                           ("GIT_SSH_COMMAND", "env-ssh"), ("GIT_SSH", "env-ssh")):
        monkeypatch.setenv(variable, str(hostile_git.planted(name)))
    started = time.monotonic()
    # The promisor repository is missing a blob; Git would lazily fetch it through an ext:: command.
    refused(router, "git.show", GIT_FAILED, path="promisor", revision="HEAD")
    status, message = error(router, "git.show", path="promisor", revision="HEAD", file="f.txt")
    assert status == "error" and message in FIXED_ERRORS
    assert time.monotonic() - started < 5
    environment = davellm_git.git_environment(davellm_git.git_executable())
    assert environment["GIT_TERMINAL_PROMPT"] == "0" and "GIT_ASKPASS" not in environment
    assert environment["GIT_ALLOW_PROTOCOL"] == "davellm-no-network"
    assert hostile_git.fired() == []


def _alive(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    return state != "Z"


def test_timeout_kills_the_whole_process_group(router, hostile_git, monkeypatch, tmp_path):  # 13
    pid_file = tmp_path / "child.pid"
    fake = tmp_path / "slow-git"
    fake.write_text(f"#!/bin/sh\nsleep 30 &\necho $! > '{pid_file}'\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setitem(davellm_git._git_cache, "path", str(fake))
    monkeypatch.setattr(davellm_git, "GIT_TIMEOUT_SECONDS", 1.0)
    started = time.monotonic()
    refused(router, "git.status", GIT_TIMED_OUT, path="repo")
    assert time.monotonic() - started < 4
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child), "the background child of Git survived the timeout"


def test_errors_are_fixed_messages_without_process_detail(router, hostile_git, monkeypatch, tmp_path):  # 14
    failures = [
        ("git.status", {"path": "plain"}), ("git.status", {"path": "linked"}),
        ("git.log", {"path": "alternates"}), ("git.show", {"path": "repo", "revision": "missing-ref"}),
        ("git.show", {"path": "repo", "revision": "HEAD", "file": "nope.txt"}),
        ("git.show", {"path": "repo", "revision": "HEAD", "file": "docs"}),
        ("git.show", {"path": "promisor", "revision": "HEAD"}), ("git.diff", {"path": "repo", "to_revision": "HEAD"}),
        ("git.diff", {"path": "repo", "staged": True, "from_revision": "HEAD"}),
        ("git.log", {"path": "repo", "file": "../escape"}),
    ]
    for name, arguments in failures:
        status, message = error(router, name, **arguments)
        assert status == "error" and message in FIXED_ERRORS, (name, arguments, message)
    noisy = tmp_path / "noisy-git"
    noisy.write_text("#!/bin/sh\necho 'fatal: /home/secret/path: Permission denied (errno 13)' >&2\nexit 128\n")
    noisy.chmod(0o755)
    monkeypatch.setitem(davellm_git._git_cache, "path", str(noisy))
    refused(router, "git.status", NOT_A_WORK_TREE, path="repo")


def test_filters_hooks_signatures_and_trace_never_run(router, hostile_git):
    before = (hostile_git.at("repo/.git/index")).stat().st_mtime_ns
    exercise_all(router)
    for repo in ("tracking", "detached", "history", "big"):
        ok(router, "git.status", path=repo)
        ok(router, "git.log", path=repo)
    assert hostile_git.fired() == []  # clean, smudge, process, hook, gpg, submodule summary
    assert (hostile_git.at("repo/.git/index")).stat().st_mtime_ns == before  # status never wrote the index
    assert not (hostile_git.root / "trace2.json").exists()


def test_git_runs_directly_without_a_shell_and_terminates_options(router, monkeypatch):
    calls = []
    original = subprocess.Popen

    def spy(args, **kwargs):
        calls.append((args, kwargs))
        return original(args, **kwargs)

    monkeypatch.setattr(davellm_git.subprocess, "Popen", spy)
    monkeypatch.setenv("DAVELLM_CANARY", "inherited")
    ok(router, "git.diff", path="repo", from_revision="v1.0", to_revision="feature")
    ok(router, "git.log", path="repo", file="docs/guide.md")
    ok(router, "git.show", path="repo", revision="v1.1", file="config.py")
    ok(router, "git.status", path="repo")
    git = davellm_git.git_executable()
    assert calls
    for args, kwargs in calls:
        assert isinstance(args, list) and args[0] == git and os.path.isabs(git)
        assert kwargs["shell"] is False and kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert "DAVELLM_CANARY" not in kwargs["env"] and kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert args[1:4] == ["--no-pager", "--no-optional-locks", "--no-replace-objects"]
        for value in ("v1.0", "feature", "v1.1"):
            if any(item.startswith(value) for item in args):
                position = next(index for index, item in enumerate(args) if item.startswith(value))
                assert "--end-of-options" in args[:position], args
        for index, item in enumerate(args):
            if item.endswith(("docs/guide.md", "config.py")) and not item.startswith(tuple("0123456789abcdef")):
                assert "--" in args[:index], args


def test_parent_git_environment_is_ignored(router, hostile_git, monkeypatch):
    outside = hostile_git.outside / "repo"
    monkeypatch.setenv("GIT_DIR", str(outside / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))
    monkeypatch.setenv("GIT_INDEX_FILE", str(outside / ".git" / "index"))
    monkeypatch.setenv("GIT_EXEC_PATH", str(hostile_git.bin))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(outside / ".git" / "objects"))
    log = ok(router, "git.log", path="repo")
    assert log["commits"][0]["subject"] == "Document the widget"
    refused(router, "git.show", FILE_NOT_AT_REVISION, path="repo", revision="HEAD", file="secret.txt")


# Revisions ------------------------------------------------------------------------------------

def head(router, repo="repo"):
    return ok(router, "git.show", path=repo, revision="HEAD")


def test_full_and_short_hashes(router, hostile_git):  # 15
    full = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=hostile_git.at("repo"), capture_output=True,
                          text=True, env={"PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"}).stdout.strip()
    for revision in (full, full[:7], full[:12]):
        shown = ok(router, "git.show", path="repo", revision=revision)
        assert shown["commit"] == full[:12] and shown["subject"] == "Add config loader"


def test_branch_revision(router):  # 16
    assert ok(router, "git.show", path="repo", revision="feature")["subject"] == "Add config loader"
    assert ok(router, "git.show", path="repo", revision="main")["subject"] == "Document the widget"
    assert ok(router, "git.show", path="repo", revision="refs/heads/main")["subject"] == "Document the widget"


def test_tag_revisions(router):  # 17
    assert ok(router, "git.show", path="repo", revision="v1.0")["subject"] == "Initial import"
    annotated = ok(router, "git.show", path="repo", revision="v1.1")  # peeled to its commit
    assert annotated["subject"] == "Add config loader" and "Release 1.1" not in json.dumps(annotated)


def test_ancestry_suffixes(router):  # 18
    assert ok(router, "git.show", path="repo", revision="HEAD~1")["subject"] == "Add config loader"
    assert ok(router, "git.show", path="repo", revision="HEAD^")["subject"] == "Add config loader"
    assert ok(router, "git.show", path="repo", revision="main~2")["subject"] == "Initial import"
    assert ok(router, "git.show", path="repo", revision="HEAD^1~1")["subject"] == "Initial import"
    refused(router, "git.show", REVISION_NOT_FOUND, path="repo", revision="HEAD~9")


def test_leading_dash_revisions_are_rejected(router):  # 19
    for value in ("-p", "--all", "-", "--", "-HEAD"):
        refused(router, "git.show", INVALID_REVISION, path="repo", revision=value)
        refused(router, "git.diff", INVALID_REVISION, path="repo", from_revision=value)
        refused(router, "git.diff", INVALID_REVISION, path="repo", from_revision="HEAD~1", to_revision=value)


def test_option_injection_attempts_are_rejected(router, hostile_git):  # 20
    before = hostile_git.snapshot()
    for value in INJECTIONS:
        if value:
            refused(router, "git.show", INVALID_REVISION, path="repo", revision=value)
            refused(router, "git.diff", INVALID_REVISION, path="repo", from_revision=value)
    for value in ("-p", "--output=pwned.txt", "/etc/passwd", "../outside", ":(glob)*", "a\nb", "docs\\x"):
        refused(router, "git.log", INVALID_FILE, path="repo", file=value)
        refused(router, "git.show", INVALID_FILE, path="repo", revision="HEAD", file=value)
    assert hostile_git.snapshot() == before
    assert not list(hostile_git.base.rglob("pwned*")) and not Path("/tmp/pwned").exists()


def test_oversize_revisions_are_rejected(router):  # 21
    long_name = "a" * 129
    assert error(router, "git.show", path="repo", revision=long_name)[0] == "validation_error"
    assert error(router, "git.diff", path="repo", from_revision=long_name)[0] == "validation_error"
    with pytest.raises(davellm_git.FileToolError, match=INVALID_REVISION):
        check_revision(long_name)
    assert check_revision("a" * 128) == "a" * 128


def test_revision_grammar():
    for value in ("HEAD", "main", "v1.0", "feature/login-2", "refs/tags/v1.0", "0123abcd", "HEAD~",
                  "HEAD~12", "HEAD^", "HEAD^2", "main~1^2~3", "release_1.2.3", "_hidden"):
        assert check_revision(value) == value
    for value in INJECTIONS + ("HEAD~12345", "a/", "/a", "a//b", "a/.b", "a.", "HEAD^{}", None, 5):
        with pytest.raises(davellm_git.FileToolError, match=INVALID_REVISION):
            check_revision(value)


# git.status -----------------------------------------------------------------------------------

def test_status_of_a_clean_tree(router):  # 22
    status = ok(router, "git.status", path="tracking")
    assert (status["branch"], status["detached"], status["truncated"]) == ("main", False, False)
    assert [status[name] for name in ("staged", "unstaged", "untracked", "conflicted")] == [[], [], [], []]
    assert status["counts"] == {"staged": 0, "unstaged": 0, "untracked": 0, "conflicted": 0}


def test_status_lists_staged_files(router):  # 23
    assert ok(router, "git.status", path="repo")["staged"] == [{"path": "app.py", "change": "modified"}]


def test_status_lists_unstaged_files(router):  # 24
    assert ok(router, "git.status", path="repo")["unstaged"] == [{"path": "README.md", "change": "modified"}]


def test_status_lists_untracked_files_but_never_protected_ones(router):  # 25
    status = ok(router, "git.status", path="repo")
    assert status["untracked"] == ["todo.txt"]
    text = json.dumps(status)
    assert ".env" not in text and ".ssh" not in text and SECRET_TEXT not in text


def test_status_reports_a_detached_head(router):  # 26
    status = ok(router, "git.status", path="detached")
    assert (status["branch"], status["detached"], status["upstream"]) == (None, True, None)
    assert len(status["commit"]) == 12


def test_status_reports_ahead_and_behind(router):  # 27
    status = ok(router, "git.status", path="tracking")
    assert (status["upstream"], status["ahead"], status["behind"]) == ("origin/main", 1, 1)
    plain = ok(router, "git.status", path="repo")
    assert (plain["upstream"], plain["ahead"], plain["behind"]) == (None, None, None)


# git.log --------------------------------------------------------------------------------------

def test_log_is_newest_first_and_capped(router):  # 28
    default = ok(router, "git.log", path="history")
    assert [commit["subject"] for commit in default["commits"]] == [
        f"History entry {number}" for number in range(60, 40, -1)]
    assert (default["count"], default["truncated"]) == (20, True)
    dates = [commit["date"] for commit in default["commits"]]
    assert dates == sorted(dates, reverse=True)
    most = ok(router, "git.log", path="history", limit=50)
    assert (most["count"], most["commits"][-1]["subject"], most["truncated"]) == (50, "History entry 11", True)
    everything = ok(router, "git.log", path="repo")
    assert [commit["subject"] for commit in everything["commits"]] == [
        "Document the widget", "Add config loader", "Initial import"]
    assert everything["truncated"] is False
    assert set(everything["commits"][0]) == {"commit", "date", "author", "subject"}
    assert "@" not in json.dumps(everything)  # no e-mail addresses


def test_log_file_filter(router):  # 29
    guide = ok(router, "git.log", path="repo", file="docs/guide.md")
    assert guide["file"] == "docs/guide.md"
    assert [commit["subject"] for commit in guide["commits"]] == ["Document the widget", "Initial import"]
    assert [c["subject"] for c in ok(router, "git.log", path="repo", file="./config.py")["commits"]] == [
        "Add config loader"]
    assert ok(router, "git.log", path="repo", file="docs")["count"] == 2  # a folder filters too
    assert ok(router, "git.log", path="repo", file="never-existed.txt")["commits"] == []
    for secret in (".env", "docs/.ENV", ".ssh/id_ed25519", "keys/server.pem"):
        refused(router, "git.log", PATH_NOT_ALLOWED, path="repo", file=secret)
    refused(router, "git.log", INVALID_FILE, path="repo", file="docs/../../x")


# git.diff -------------------------------------------------------------------------------------

def test_working_tree_diff(router):  # 30
    diff = ok(router, "git.diff", path="repo")
    assert diff["mode"] == "working" and diff["truncated"] is False
    assert "+Second line." in diff["diff"] and "app.py" not in diff["diff"]
    assert ".env" not in diff["diff"] and SECRET_TEXT not in diff["diff"]


def test_staged_diff(router):  # 31
    diff = ok(router, "git.diff", path="repo", staged=True)
    assert diff["mode"] == "staged"
    assert "+print('widget v2')" in diff["diff"] and "README.md" not in diff["diff"]


def test_revision_diff(router):  # 32
    diff = ok(router, "git.diff", path="repo", from_revision="v1.0", to_revision="v1.1")
    assert (diff["mode"], diff["from_revision"], diff["to_revision"]) == ("revisions", "v1.0", "v1.1")
    assert "+LOADED = True" in diff["diff"] and "guide" not in diff["diff"]
    to_head = ok(router, "git.diff", path="repo", from_revision="HEAD~1")
    assert to_head["to_revision"] == "HEAD" and "+Use the widget." in to_head["diff"]
    refused(router, "git.diff", TO_WITHOUT_FROM, path="repo", to_revision="HEAD")
    refused(router, "git.diff", MODE_CONFLICT, path="repo", staged=True, from_revision="HEAD~1")
    refused(router, "git.diff", REVISION_NOT_FOUND, path="repo", from_revision="no-such-branch")


# git.show -------------------------------------------------------------------------------------

def test_show_commit(router):  # 33
    shown = ok(router, "git.show", path="repo", revision="v1.1")
    assert (shown["subject"], shown["author"], shown["revision"]) == ("Add config loader", "Fixture Author", "v1.1")
    assert len(shown["parents"]) == 1 and shown["body"] == ""
    assert "diff --git a/config.py b/config.py" in shown["diff"]
    initial = ok(router, "git.show", path="repo", revision="HEAD~2")
    assert "README.md" in initial["diff"] and "docs/guide.md" in initial["diff"]
    assert ".env" not in initial["diff"] and SECRET_TEXT not in json.dumps(initial)
    signed = head(router)
    assert "PGP" not in json.dumps(signed) and signed["subject"] == "Document the widget"


def test_show_file_at_revision(router):  # 34
    old = ok(router, "git.show", path="repo", revision="v1.0", file="docs/guide.md")
    assert (old["file"], old["lines"], old["total_lines"], old["truncated"]) == ("docs/guide.md", ["# Guide"], 1, False)
    new = ok(router, "git.show", path="repo", revision="HEAD", file="docs/guide.md")
    assert new["lines"] == ["# Guide", "", "Use the widget."]
    fact = ok(router, "git.show", path="history", revision="HEAD~53", file="log.txt")
    assert fact["lines"] == ["entry 7", UNIQUE_FACT]
    refused(router, "git.show", PATH_NOT_ALLOWED, path="repo", revision="v1.0", file=".env")
    refused(router, "git.show", FILE_NOT_AT_REVISION, path="repo", revision="v1.0", file="config.py")
    refused(router, "git.show", NOT_A_FILE_AT_REVISION, path="repo", revision="HEAD", file="docs")


# Output bounds and effects --------------------------------------------------------------------

def test_outputs_are_truncated_within_the_budget(router):  # 35
    for name, arguments in (("git.diff", {}), ("git.show", {"revision": "HEAD"}),
                            ("git.show", {"revision": "HEAD", "file": "big.txt"}), ("git.status", {})):
        execution = run(router, name, path="big", **arguments)
        assert execution.status == "success", (name, execution.error)
        assert len(execution.result.encode("utf-8")) <= OUTPUT_BUDGET_BYTES
        assert json.loads(execution.result)["truncated"] is True, name
    diff = ok(router, "git.diff", path="big")
    assert len(diff["diff"].encode("utf-8")) <= davellm_git.PATCH_MAX_BYTES and diff["diff"].endswith("\n")
    status = ok(router, "git.status", path="big")
    assert status["counts"]["untracked"] == 150 and len(status["untracked"]) == davellm_git.STATUS_MAX_ENTRIES
    lines = ok(router, "git.show", path="big", revision="HEAD", file="big.txt")
    assert (lines["line_count"], lines["total_lines"]) == (400, 1000)


def test_every_read_leaves_the_filesystem_unchanged(router, hostile_git):  # 36
    before = hostile_git.snapshot()
    exercise_all(router)
    for repo in ("tracking", "detached", "history", "big", "promisor", "linked", "symgit", "escaped",
                 "alternates", "vendor/embedded.git", "plain", "link-repo"):
        for name, extra in (("git.status", {}), ("git.diff", {}), ("git.log", {}),
                            ("git.show", {"revision": "HEAD"})):
            run(router, name, path=repo, **extra)
    assert hostile_git.snapshot() == before
    assert hostile_git.fired() == []


def test_git_tools_pass_the_security_contract(extended, hostile_tree):
    env = {"PATH": os.environ["PATH"], "HOME": str(hostile_tree.base), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@example.invalid",
           "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@example.invalid"}
    shutil.rmtree(hostile_tree.root / ".git")  # the planted stand-in, so the root can become a real repository
    for args in (("init", "-q", "-b", "main"), ("add", "README.md", "docs", "src"), ("commit", "-qm", "Tree")):
        subprocess.run(["git", *args], cwd=hostile_tree.root, env=env, check=True, capture_output=True)
    router = extended(hostile_tree.root)
    contracts = [
        PathToolContract(tool="git.status", arguments=lambda path: {"path": path}, allowed=("docs", "src"),
                         invalid=({"path": ""}, {"path": "docs", "extra": 1})),
        PathToolContract(tool="git.diff", arguments=lambda path: {"path": path}, allowed=("docs",),
                         invalid=({"path": "docs", "staged": "x"},)),
        PathToolContract(tool="git.log", arguments=lambda path: {"path": path, "limit": 5}, allowed=("docs",),
                         invalid=({"path": "docs", "limit": 51},)),
        PathToolContract(tool="git.show", arguments=lambda path: {"path": path, "revision": "HEAD"},
                         allowed=("docs", "src"), invalid=({"path": "docs"},)),
    ]
    for contract in contracts:
        assert_path_contract(contract, hostile_tree, run_path_contract(router.TOOL_REGISTRY, contract, hostile_tree))


def test_runner_caps_captured_output_and_stops_git(hostile_git):
    result = davellm_git.run_git(
        ("log", "--format=%H"), cwd=hostile_git.at("history"), limit=100,
        deadline=time.monotonic() + davellm_git.GIT_TIMEOUT_SECONDS,
    )
    assert result.truncated is True and len(result.stdout) == 100
    whole = davellm_git.run_git(("log", "--format=%H"), cwd=hostile_git.at("history"),
                                deadline=time.monotonic() + davellm_git.GIT_TIMEOUT_SECONDS)
    assert (whole.truncated, whole.returncode, len(whole.stdout)) == (False, 0, 60 * 41)
