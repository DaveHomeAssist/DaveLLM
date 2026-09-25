"""Shared security contract for extended path tools.

A tool under test is described by a ``PathToolContract``: how to build its real
arguments for a path, plus its own oversized and schema-invalid arguments when
it has them. ``run_path_contract`` drives every case through DaveHarness's
validated dispatcher against the hostile tree, and ``assert_path_contract``
checks the expectations every extended path tool shares:

- allowed paths succeed;
- root escapes, symlink escapes, symlink loops, and (when the tool enforces
  it) every denylisted path fail, all with the same error when ``refusal`` is set;
- oversized and schema-invalid arguments fail;
- no planted sentinel appears in any result or error;
- nothing under the hostile base changes, except the target of an allowed
  mutation and any parent folders it needed.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from daveharness import ToolRegistry, run_tool
from davellm_files import PATH_NOT_ALLOWED
from hostile_fs import (
    CHAIN_LINK, INTERNAL_LINK, LOOP_LINK, OUTSIDE_DIR_LINK, OUTSIDE_LINK,
    SECRET_DIR_LINK, SECRET_DIRS, SECRET_FILE_LINK, SECRET_FILES, HostileTree,
)


ALLOWED_PATHS = ("README.md", "docs/guide.md", "docs/nested/deep/note.txt", INTERNAL_LINK)


@dataclass(frozen=True)
class PathToolContract:
    tool: str
    arguments: Callable[[str], dict[str, Any]]
    allowed: tuple[str, ...] = ALLOWED_PATHS
    enforces_denylist: bool = True
    refusal: str | None = PATH_NOT_ALLOWED  # None accepts any failure for refused paths
    mutates: bool = False
    oversized: Callable[[HostileTree], dict[str, Any]] | None = None
    invalid: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Case:
    name: str
    arguments: dict[str, Any]
    expect: str  # "allow", "refuse" (a refused path), or "fail" (any failure)
    target: str | None = None  # base-relative entry an allowed mutation may change


@dataclass(frozen=True)
class Outcome:
    case: Case
    status: str
    result: str
    error: str | None
    changed: frozenset[str]


def escape_paths(tree: HostileTree) -> dict[str, str]:
    return {
        "dotdot escape": f"{tree.root}/docs/../../outside/secret.txt",
        "absolute outside path": str(tree.outside / "secret.txt"),
        "symlink escape": str(tree.at(OUTSIDE_LINK)),
        "symlink directory escape": str(tree.at(OUTSIDE_DIR_LINK) / "notes.txt"),
        "symlink chain escape": str(tree.at(CHAIN_LINK)),
        "symlink loop": str(tree.at(LOOP_LINK)),
    }


def denylisted_paths(tree: HostileTree) -> dict[str, str]:
    paths = {f"secret file {item}": str(tree.at(item)) for item in SECRET_FILES}
    paths.update({f"secret directory {item}": str(tree.at(item)) for item in SECRET_DIRS})
    paths.update({
        "missing secret": str(tree.at(".ssh/does-not-exist")),
        "normalized secret directory": f"{tree.root}/docs/../.ssh/config",
        "normalized secret file": f"{tree.root}/./src/../.env",
        "symlink to secret file": str(tree.at(SECRET_FILE_LINK)),
        "symlink into secret directory": str(tree.at(SECRET_DIR_LINK) / "config"),
    })
    return paths


def contract_cases(contract: PathToolContract, tree: HostileTree) -> list[Case]:
    cases = [
        Case(
            f"allowed {item}", contract.arguments(str(tree.at(item))), "allow",
            target=tree.at(item).resolve().relative_to(tree.base).as_posix(),
        )
        for item in contract.allowed
    ]
    refused = escape_paths(tree)
    if contract.enforces_denylist:
        refused.update(denylisted_paths(tree))
    cases += [Case(name, contract.arguments(path), "refuse") for name, path in refused.items()]
    if contract.oversized is not None:
        cases.append(Case("oversized", contract.oversized(tree), "fail"))
    cases += [Case(f"schema rejects {args!r}", args, "fail") for args in contract.invalid]
    return cases


def run_path_contract(
    registry: ToolRegistry, contract: PathToolContract, tree: HostileTree,
) -> list[Outcome]:
    outcomes = []
    for case in contract_cases(contract, tree):
        before = tree.snapshot()
        execution = asyncio.run(run_tool(contract.tool, case.arguments, registry=registry))
        after = tree.snapshot()
        changed = frozenset(
            key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
        )
        outcomes.append(Outcome(case, execution.status, execution.result, execution.error, changed))
    return outcomes


def contract_failures(
    contract: PathToolContract, tree: HostileTree, outcomes: list[Outcome],
) -> list[str]:
    failures = []
    for outcome in outcomes:
        case = outcome.case
        if case.expect == "allow" and outcome.status != "success":
            failures.append(f"{case.name}: expected success, got {outcome.status} {outcome.error!r}")
        if case.expect != "allow" and outcome.status == "success":
            failures.append(f"{case.name}: expected a failure, got success")
        if (
            case.expect == "refuse" and contract.refusal is not None
            and outcome.status != "success" and outcome.error != contract.refusal
        ):
            failures.append(f"{case.name}: expected {contract.refusal!r}, got {outcome.error!r}")
        leaked = tree.leaked(outcome.result, outcome.error)
        if leaked:
            failures.append(f"{case.name}: leaked {sorted(leaked)}")
        permitted = set()
        if case.expect == "allow" and contract.mutates and case.target is not None:
            permitted = {
                key for key in outcome.changed
                if key == case.target or case.target.startswith(key + "/")
            }
        unexpected = outcome.changed - permitted
        if unexpected:
            failures.append(f"{case.name}: changed {sorted(unexpected)}")
    return failures


def assert_path_contract(
    contract: PathToolContract, tree: HostileTree, outcomes: list[Outcome],
) -> None:
    failures = contract_failures(contract, tree, outcomes)
    assert not failures, "\n".join(failures)
