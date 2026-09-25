"""PR-01: the secret-path denylist and root containment every extended tool inherits.

Every test runs against the disposable hostile tree from tests/hostile_fs.py,
never against real project files.
"""

import json
import os
from pathlib import Path

import pytest

import davellm_files
from daveharness import ToolDefinition, ToolRegistry
from davellm_files import (
    PATH_NOT_ALLOWED, SECRET_DIR_NAMES, SECRET_NAME_PATTERNS, PathNotAllowed,
    display_path, has_secret_component, is_secret_name, looks_binary, walk_tree,
)
from hostile_fs import (
    BINARY_FILE, CHAIN_LINK, IGNORED_FILES, INTERNAL_LINK, LOOP_LINK, NORMAL_FILES,
    OUTSIDE_DIR_LINK, OUTSIDE_LINK, OVERSIZED_BYTES, OVERSIZED_FILE, SECRET_DIR_LINK,
    SECRET_DIRS, SECRET_FILE_LINK, SECRET_FILES, sentinel,
)
from tool_contract import (
    PathToolContract, assert_path_contract, contract_failures, run_path_contract,
)


BASELINE = json.loads(
    (Path(__file__).parent / "fixtures" / "davellm" / "tool_catalog.json").read_text()
)
SECRET_FILE_NAMES = (
    ".env", ".env.local", ".env.production", "server.pem", "server.key", "id_rsa",
    "id_rsa.pub", "id_rsa_backup", "client.p12",
    ".ENV", ".Env.Local", "SERVER.PEM", "Server.Key", "ID_RSA", "CLIENT.P12",
)
NEAR_MISSES = (
    "env", "env.txt", ".envrc", "environment.md", "pem.txt", "keys", "key.txt",
    "monkey", "rsa_id", "p12.md", "ssh", ".sshd", "aws", "gnupg",
)


@pytest.fixture
def extended(router_factory, monkeypatch, hostile_tree):
    """Load app with tools and extended tools on, rooted at the hostile tree by default."""
    def load(*roots):
        monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", "true")
        router, _, _ = router_factory(
            tools=True, tool_roots=[str(item) for item in roots or [hostile_tree.root]],
        )
        return router

    return load


def refused(router, path):
    with pytest.raises(PathNotAllowed) as caught:
        router.resolve_extended_tool_path(str(path))
    return caught.value


def admitted(router, path):
    return router.resolve_extended_tool_path(str(path))


def outcome(router, path):
    try:
        return str(admitted(router, path))
    except PathNotAllowed as exc:
        return str(exc)


def test_hostile_tree_contains_every_planted_hazard(hostile_tree):
    tree = hostile_tree
    for relative in [*NORMAL_FILES, *IGNORED_FILES, *SECRET_FILES, BINARY_FILE, OVERSIZED_FILE]:
        assert tree.at(relative).is_file(), relative
    for relative in SECRET_DIRS:
        assert tree.at(relative).is_dir(), relative
    for relative in (INTERNAL_LINK, OUTSIDE_LINK, OUTSIDE_DIR_LINK, CHAIN_LINK,
                     SECRET_FILE_LINK, SECRET_DIR_LINK, LOOP_LINK):
        assert tree.at(relative).is_symlink(), relative
    assert tree.at(OUTSIDE_LINK).resolve().parent == tree.outside
    assert tree.at(CHAIN_LINK).resolve() == tree.outside / "notes.txt"
    assert tree.at(OVERSIZED_FILE).stat().st_size == OVERSIZED_BYTES
    assert looks_binary(tree.at(BINARY_FILE).read_bytes())
    assert len(tree.sentinels) == len(SECRET_FILES) + 2
    assert tree.leaked(tree.at(".env").read_text()) == {sentinel(".env")}


def test_the_denylist_is_exactly_the_documented_policy():
    assert SECRET_NAME_PATTERNS == (".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_rsa*", "*.p12")
    assert SECRET_DIR_NAMES == {".ssh", ".aws", ".gnupg"}


@pytest.mark.parametrize("name", SECRET_FILE_NAMES)
def test_every_denylisted_file_pattern_is_blocked(extended, hostile_tree, name):
    router = extended()
    assert is_secret_name(name)
    for parent in ("", "src/", "docs/nested/deep/"):
        refused(router, hostile_tree.at(parent + name))


@pytest.mark.parametrize("name", NEAR_MISSES)
def test_near_miss_names_are_not_blocked(extended, hostile_tree, name):
    assert not is_secret_name(name)
    assert admitted(extended(), hostile_tree.at(name)) == hostile_tree.at(name)


@pytest.mark.parametrize("directory", [".ssh", ".aws", ".gnupg", ".SSH", ".Aws", ".GnuPG"])
def test_every_denylisted_directory_is_blocked(extended, hostile_tree, directory):
    router = extended()
    for relative in (directory, f"{directory}/", f"{directory}/config", f"{directory}/sub/deep.txt",
                     f"docs/{directory}/known_hosts", f"src/{directory}"):
        refused(router, f"{hostile_tree.root}/{relative}")


def test_walks_omit_secrets_and_never_enter_secret_directories(hostile_tree, monkeypatch):
    scanned = []
    real_scandir = os.scandir

    def spy(path):
        scanned.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(davellm_files.os, "scandir", spy)
    result = walk_tree(hostile_tree.root, hostile_tree.root, max_depth=10, max_entries=1000)
    monkeypatch.undo()

    assert {entry.display for entry in result.entries} == {
        "README.md", "certs", "config", "data", "docs", "keys", "links", "src",
        BINARY_FILE, OVERSIZED_FILE, "docs/guide.md", "docs/nested", "docs/nested/deep",
        "docs/nested/deep/note.txt", "keys/README.txt", INTERNAL_LINK, "src/app.py",
    }
    assert scanned and not [path for path in scanned if has_secret_component(path)]
    assert (result.truncated, result.depth_limited, result.unreadable_dirs) == (False, False, 0)
    assert result.ignored_dirs == len(IGNORED_FILES)


def test_secrets_cannot_be_inferred_from_walk_counters_or_starts(tmp_path):
    root = tmp_path.resolve()
    (root / "a").mkdir()
    (root / "a" / ".env").write_text("x")
    (root / "b.txt").write_text("x")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "config").write_text("x")
    (root / "id_rsa").write_text("x")
    (root / "to_ssh").symlink_to(root / ".ssh", target_is_directory=True)

    for ignored in (davellm_files.DEFAULT_IGNORED_DIRS, {".ssh"}):
        result = walk_tree(root, root, max_depth=1, max_entries=2, ignored_dirs=ignored)
        assert [entry.display for entry in result.entries] == ["a", "b.txt"]
        assert (result.truncated, result.depth_limited, result.ignored_dirs) == (False, False, 0)
    for start in (root / ".ssh", root / "a" / ".." / ".ssh", root / "to_ssh"):
        with pytest.raises(PathNotAllowed):
            walk_tree(start, root, max_depth=1, max_entries=10)


def test_dotdot_escape_is_rejected(extended, hostile_tree):
    router = extended()
    root = hostile_tree.root
    for path in (f"{root}/../outside/secret.txt", f"{root}/docs/../../outside/notes.txt",
                 f"{root}/docs/nested/deep/../../../../outside", f"{root}/..", "../outside/secret.txt"):
        refused(router, path)


def test_absolute_outside_path_is_rejected(extended, hostile_tree):
    router = extended()
    for path in (hostile_tree.outside / "secret.txt", hostile_tree.outside, hostile_tree.base,
                 Path("/"), Path("/etc/passwd"), "~/.bashrc"):
        refused(router, path)


def test_direct_symlink_escape_is_rejected(extended, hostile_tree):
    router = extended()
    hostile_tree.at("links/filesystem_root").symlink_to("/", target_is_directory=True)
    for relative in (OUTSIDE_LINK, OUTSIDE_DIR_LINK, f"{OUTSIDE_DIR_LINK}/secret.txt",
                     f"{OUTSIDE_DIR_LINK}/missing.txt", "links/filesystem_root/etc/passwd"):
        refused(router, hostile_tree.at(relative))


def test_chained_symlink_escape_is_rejected(extended, hostile_tree):
    router = extended()
    for relative in (CHAIN_LINK, "links/chain_b", "links/chain_c", LOOP_LINK, "links/loop_b",
                     f"{LOOP_LINK}/child.txt"):
        refused(router, hostile_tree.at(relative))
    # Containment judges the final target, so a chain that leaves and comes back stays inside.
    (hostile_tree.outside / "back").symlink_to(hostile_tree.at("README.md"))
    hostile_tree.at("links/round_trip").symlink_to(hostile_tree.outside / "back")
    assert admitted(router, hostile_tree.at("links/round_trip")) == hostile_tree.at("README.md")


def test_normalization_cannot_bypass_the_denylist(extended, hostile_tree):
    router = extended()
    for relative in ("docs/../.ssh/config", "./.env", "docs/./../.env", "docs//..//.aws/credentials",
                     ".ssh/", ".ssh/.", "src/../.gnupg", "docs/nested/./.ssh/known_hosts",
                     "certs/../certs/server.pem", "links/../.env.local", "keys/./id_rsa.pub",
                     SECRET_FILE_LINK, f"{SECRET_DIR_LINK}/config", f"{SECRET_DIR_LINK}/"):
        refused(router, f"{hostile_tree.root}/{relative}")
    # The name as written counts too: a secret-named link to an ordinary file is refused.
    hostile_tree.at("docs/.env").symlink_to("../README.md")
    refused(router, hostile_tree.at("docs/.env"))


def test_admission_does_not_trust_the_resolver_to_remove_symlinks(hostile_tree):
    # Python 3.13+ can return a symlink loop unresolved; any symlink left after resolution is refused.
    unresolved = lambda path: Path(path).absolute()  # noqa: E731
    for relative in (LOOP_LINK, OUTSIDE_LINK, INTERNAL_LINK):
        with pytest.raises(PathNotAllowed):
            davellm_files.admit_path(str(hostile_tree.at(relative)), unresolved)
    assert davellm_files.admit_path(str(hostile_tree.at("README.md")), unresolved) == hostile_tree.at("README.md")


def test_ordinary_files_remain_accessible(extended, hostile_tree):
    router = extended()
    root = hostile_tree.root
    for relative in [*NORMAL_FILES, BINARY_FILE, OVERSIZED_FILE, "docs", "docs/new-note.md"]:
        assert admitted(router, hostile_tree.at(relative)) == hostile_tree.at(relative)
    assert admitted(router, root) == root
    assert admitted(router, hostile_tree.at(INTERNAL_LINK)) == hostile_tree.at("docs/guide.md")
    assert admitted(router, f"{root}/docs/../README.md") == hostile_tree.at("README.md")


def test_every_refusal_is_identical_and_reveals_nothing(extended, hostile_tree):
    router = extended()
    root = hostile_tree.root
    paths = [
        root / ".ssh/config", root / ".ssh/missing", root / ".env", root / "src/.env",
        hostile_tree.outside / "secret.txt", hostile_tree.outside / "missing.txt",
        hostile_tree.at(OUTSIDE_LINK), hostile_tree.at(LOOP_LINK), "", "README.md\x00.env",
    ]
    errors = [refused(router, path) for path in paths]
    assert {(type(error), error.args) for error in errors} == {(PathNotAllowed, (PATH_NOT_ALLOWED,))}
    assert all(error.__cause__ is None and error.__context__ is None for error in errors)


def test_nested_roots_are_deterministic(extended, hostile_tree):
    root, docs = hostile_tree.root, hostile_tree.at("docs")
    probes = [root / "README.md", docs / "guide.md", docs / "nested/deep/note.txt",
              docs / "nested/.ssh/known_hosts", root / ".env", hostile_tree.outside / "secret.txt",
              hostile_tree.at(INTERNAL_LINK)]
    results = []
    for roots in ((root, docs), (docs, root)):
        router = extended(*roots)
        results.append([outcome(router, path) for path in probes])
        assert display_path(docs / "guide.md", roots) == "guide.md"
    assert results[0] == results[1]
    assert results[0].count(PATH_NOT_ALLOWED) == 3

    for roots in ((root, root / ".ssh"), (root / ".ssh", root)):
        refused(extended(*roots), root / ".ssh" / "config")
    only_docs = extended(docs)
    refused(only_docs, root / "README.md")
    assert admitted(only_docs, docs / "guide.md") == docs / "guide.md"


PROBE_LIMIT_BYTES = 1_048_576


def probe_registry(resolve, *, side_effect=None):
    """A test double for a future read tool, built on a caller-chosen resolver."""
    def read_text(args):
        if side_effect is not None:
            side_effect()
        try:
            path = resolve(args["path"])
        except PathNotAllowed as exc:
            return {"status": "error", "error": str(exc)}
        if path.stat().st_size > PROBE_LIMIT_BYTES:
            return {"status": "error", "error": "File exceeds the probe limit"}
        return path.read_text(encoding="utf-8")

    registry = ToolRegistry()
    registry.register(ToolDefinition(
        "probe.read", "Test double that reads text through a path resolver.",
        {"type": "object", "properties": {"path": {"type": "string", "minLength": 1}},
         "required": ["path"], "additionalProperties": False},
        read_text, permission="read_files", handler_version="test-probe",
    ))
    return registry


PROBE = PathToolContract(
    tool="probe.read",
    arguments=lambda path: {"path": path},
    oversized=lambda tree: {"path": str(tree.at(OVERSIZED_FILE))},
    invalid=({}, {"path": ""}, {"path": 1}, {"path": "README.md", "extra": True}),
)


def test_contract_passes_a_tool_behind_the_extended_boundary(extended, hostile_tree):
    router = extended()
    outcomes = run_path_contract(probe_registry(router.resolve_extended_tool_path), PROBE, hostile_tree)
    assert_path_contract(PROBE, hostile_tree, outcomes)
    expected = {case.case.expect for case in outcomes}
    assert expected == {"allow", "refuse", "fail"} and len(outcomes) > 30


def test_contract_catches_tools_that_skip_the_boundary(extended, hostile_tree):
    router = extended()
    contained_only = contract_failures(PROBE, hostile_tree, run_path_contract(
        probe_registry(router.resolve_tool_path), PROBE, hostile_tree))
    assert any(line.startswith("secret file .env: expected a failure") for line in contained_only)
    assert any("leaked" in line for line in contained_only)

    unbounded = contract_failures(PROBE, hostile_tree, run_path_contract(
        probe_registry(lambda path: Path(path).resolve()), PROBE, hostile_tree))
    assert any(line.startswith("symlink chain escape: expected a failure") for line in unbounded)
    assert any(line.startswith("absolute outside path: leaked") for line in unbounded)

    marker = hostile_tree.at("side-effect.txt")
    touching = contract_failures(PROBE, hostile_tree, run_path_contract(
        probe_registry(router.resolve_extended_tool_path, side_effect=marker.touch),
        PROBE, hostile_tree))
    assert any(line.startswith("allowed README.md: changed") for line in touching)


def test_existing_file_read_still_passes_the_containment_contract(extended, hostile_tree):
    router = extended()
    contract = PathToolContract(
        tool="file.read",
        arguments=lambda path: {"path": path},
        enforces_denylist=False,
        refusal=None,
        invalid=({}, {"path": ""}, {"path": "README.md", "extra": True}),
    )
    assert_path_contract(contract, hostile_tree, run_path_contract(router.TOOL_REGISTRY, contract, hostile_tree))


def test_existing_file_read_is_not_covered_by_the_denylist_yet(extended, hostile_tree):
    """The qualified file.read is deliberately unchanged; applying the denylist is a separate decision."""
    router = extended()
    result = router.tool_file_read({"path": str(hostile_tree.at(".env"))})
    assert result.status == "success" and sentinel(".env") in result.result
    refused(router, hostile_tree.at(".env"))


def test_extended_flag_adds_only_the_extended_tools(extended):
    router = extended()
    extended_tools = {"file.list", "file.search", "file.read_lines", "md.outline", "md.section",
                      "git.status", "git.diff", "git.log", "git.show"}
    assert router.EXTENDED_TOOLS_ENABLED is True
    assert {definition.name for definition in router.extended_tool_definitions()} == extended_tools
    for registry in (router.TOOL_REGISTRY, router.HARNESS_REGISTRY):
        assert sorted(set(registry.public_catalog()) - extended_tools) == sorted(BASELINE["default_tools"])
