"""Hardened read-only Git tools: git.status, git.diff, git.log, and git.show.

A repository's own configuration can name programs that Git runs during
ordinary reads: fsmonitor hooks, clean and smudge filters, textconv and diff
drivers, external diff commands, pagers, credential helpers, signature
verifiers, and lazy fetches from promisor remotes. These tools must stay read
only even for a hostile repository, so every Git process goes through
``run_git``, the one runner in this module:

- Git runs directly from an argument list, never through a shell, with stdin
  closed, in its own process group, under a deadline, and with capped output.
- The environment is built from scratch. System and global configuration are
  ignored, prompts are disabled, optional locks are off (so status never
  writes the index), and every network protocol is refused.
- Fixed ``GIT_CONFIG_*`` overrides disable fsmonitor, hooks, the pager,
  credential helpers, attribute files, signature checks, submodule recursion,
  and implicit bare repositories. Filter drivers found in the repository's
  configuration are overridden with empty commands.
- Diff-producing commands also pass ``--no-ext-diff`` and ``--no-textconv``.

Repositories are admitted only when the requested folder passes the extended
path boundary, Git's working tree, Git directory, common directory, and object
store all resolve inside a tool root, and no alternate object store is used.
Revisions follow a strict grammar and are resolved to a commit hash with
``--end-of-options`` before any other command sees them. File paths are
repository relative, follow the secret denylist, and are passed after ``--``
as literal pathspecs. Protected paths are also left out of status and patches.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from davellm_files import (
    FILE_NOT_FOUND, NOT_TEXT, OUTPUT_BUDGET_BYTES, READ_MAX_LINE_CHARS, READ_MAX_LINES, SECRET_DIR_NAMES,
    SECRET_NAME_PATTERNS, FileToolError, PathNotAllowed, _fitting, _option, decode_text,
    deepest_root, encode_result, has_secret_component, read_admitted_bytes, relative_display, split_lines,
)


GIT_TIMEOUT_SECONDS = 8.0  # per tool call, below DaveHarness's 10-second tool timeout
GIT_CAPTURE_BYTES = 1_048_576  # stdout kept per Git process before it is stopped
GIT_STDERR_BYTES = 65_536
PATCH_MAX_BYTES = 40_000
STATUS_MAX_ENTRIES = 100  # per list
LOG_DEFAULT_COMMITS = 20
LOG_MAX_COMMITS = 50
REVISION_MAX_CHARS = 128
FILE_MAX_CHARS = 1_024
TEXT_FIELD_CHARS = 300
SHOW_BODY_CHARS = 2_000
ALTERNATES_MAX_BYTES = 65_536  # a real alternates file is a few lines
GIT_DIR_MAX_ENTRIES = 100_000  # entries checked for links in the Git directory before it is refused
MIN_GIT_VERSION = (2, 32)

NOT_A_WORK_TREE = "Not a Git working tree"
UNTRUSTED_REPOSITORY = "Repository is owned by another user"
UNSUPPORTED_REPOSITORY = "Repository layout is not supported"
INVALID_REVISION = "Invalid revision"
REVISION_NOT_FOUND = "Revision not found"
INVALID_FILE = "Use a file path relative to the repository"
FILE_NOT_AT_REVISION = "File not found at that revision"
NOT_A_FILE_AT_REVISION = "Path is not a file at that revision"
MODE_CONFLICT = "Use staged or revisions, not both"
TO_WITHOUT_FROM = "to_revision needs from_revision"
GIT_TIMED_OUT = "Git timed out"
GIT_FAILED = "Git command failed"
GIT_UNAVAILABLE = "Git 2.32 or newer is not available"

# Options placed before every Git subcommand.
GLOBAL_OPTIONS = ("--no-pager", "--no-optional-locks", "--no-replace-objects")
# Configuration forced on every Git process, over anything the repository sets.
FIXED_CONFIG = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", os.devnull),
    ("core.pager", "cat"),
    ("core.askPass", ""),
    ("credential.helper", ""),
    ("core.attributesFile", os.devnull),
    ("core.excludesFile", os.devnull),
    ("core.untrackedCache", "false"),
    ("core.quotePath", "false"),
    ("log.showSignature", "false"),
    ("log.mailmap", "false"),
    ("status.submoduleSummary", "false"),
    ("diff.submodule", "short"),
    ("submodule.recurse", "false"),
    ("safe.bareRepository", "explicit"),
    ("protocol.allow", "never"),
    ("color.ui", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)
FILTER_COMMANDS = ("clean", "smudge", "process")
# Diff-producing commands never run external diff programs or text converters.
DIFF_SAFETY = ("--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all")

_REVISION = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*(?:~[0-9]{0,4}|\^[0-9]?)*"
)
_OBJECT_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_CHANGES = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied",
            "T": "type_changed", "U": "unmerged"}


# The runner -----------------------------------------------------------------------------------

_git_cache: dict[str, str] = {}


def git_executable() -> str:
    """The absolute path of a Git that is new enough, checked once per process."""
    if "path" not in _git_cache:
        found = shutil.which("git")
        if found is None:
            raise FileToolError(GIT_UNAVAILABLE)
        path = os.path.realpath(found)
        result = run_git(("--version",), cwd=Path(os.sep), executable=path,
                         deadline=time.monotonic() + GIT_TIMEOUT_SECONDS)
        numbers = re.findall(r"\d+", result.stdout.decode("ascii", "replace"))
        if result.returncode != 0 or tuple(int(item) for item in numbers[:2]) < MIN_GIT_VERSION:
            raise FileToolError(GIT_UNAVAILABLE)
        _git_cache["path"] = path
    return _git_cache["path"]


def git_environment(executable: str, extra: Mapping[str, str] = {},
                    overrides: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
    """A from-scratch environment: nothing is inherited from the DaveLLM process."""
    env = {
        "PATH": os.pathsep.join([os.path.dirname(executable), "/usr/bin", "/bin"]),
        "HOME": os.devnull,
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        # Behave as if protocol.allow=never, overriding any per-protocol repository setting.
        "GIT_ALLOW_PROTOCOL": "davellm-no-network",
        **extra,
    }
    config = [*FIXED_CONFIG, *overrides]
    env["GIT_CONFIG_COUNT"] = str(len(config))
    for index, (key, value) in enumerate(config):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool


def _stop(process: subprocess.Popen[bytes]) -> None:
    """Kill Git and anything it started: each run has its own process group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    process.wait()


def run_git(args: Sequence[str], *, cwd: Path, deadline: float, env: Mapping[str, str] | None = None,
            executable: str | None = None, limit: int = GIT_CAPTURE_BYTES) -> GitResult:
    """Run one Git command. This is the only place a Git process is started."""
    git = executable or git_executable()
    argv = [git, *GLOBAL_OPTIONS, *args]
    if time.monotonic() >= deadline:
        raise FileToolError(GIT_TIMED_OUT)
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=dict(env) if env is not None else git_environment(git),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, close_fds=True, start_new_session=True,
        )
    except OSError:
        raise FileToolError(GIT_UNAVAILABLE) from None
    out, err, truncated = bytearray(), bytearray(), False
    assert process.stdout is not None and process.stderr is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, out)
        selector.register(process.stderr, selectors.EVENT_READ, err)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop(process)
                raise FileToolError(GIT_TIMED_OUT)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = key.data
                room = (limit if buffer is out else GIT_STDERR_BYTES) - len(buffer)
                buffer += chunk[:max(0, room)]
                if buffer is out and len(chunk) > room:
                    truncated = True
                    break
            if truncated:
                _stop(process)
                break
    try:
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _stop(process)
        raise FileToolError(GIT_TIMED_OUT) from None
    finally:
        process.stdout.close()
        process.stderr.close()
    return GitResult(returncode, bytes(out), bytes(err), truncated)


# Repository admission -------------------------------------------------------------------------

@dataclass(frozen=True)
class Repository:
    top: Path
    display: str
    env: dict[str, str]
    deadline: float

    def git(self, *args: str, limit: int = GIT_CAPTURE_BYTES) -> GitResult:
        return run_git(args, cwd=self.top, env=self.env, deadline=self.deadline, limit=limit)

    def output(self, *args: str, limit: int = GIT_CAPTURE_BYTES) -> GitResult:
        result = self.git(*args, limit=limit)
        if result.returncode != 0 and not result.truncated:
            raise FileToolError(GIT_FAILED)
        return result


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in (Path(item).resolve() for item in roots))


def _real(value: str) -> Path:
    return Path(os.path.realpath(value))


def open_repository(arguments: Mapping[str, Any], resolve: Callable[[str], Path],
                    roots: Sequence[Path]) -> Repository:
    """Admit the repository named by ``path``; every refusal is a fixed message."""
    deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
    target = resolve(_option(arguments, "path", "."))
    if not target.is_dir():
        raise FileToolError(NOT_A_WORK_TREE)
    git = git_executable()
    # Discovery may reach the tool root but never the folder above it.
    ceiling = {"GIT_CEILING_DIRECTORIES": str(deepest_root(target, roots).parent)}
    probe = run_git(
        ("rev-parse", "--path-format=absolute", "--is-inside-work-tree", "--show-toplevel",
         "--absolute-git-dir", "--git-common-dir", "--git-path", "objects"),
        cwd=target, env=git_environment(git, ceiling), deadline=deadline,
    )
    if probe.returncode != 0:
        if b"dubious ownership" in probe.stderr:
            raise FileToolError(UNTRUSTED_REPOSITORY)
        raise FileToolError(NOT_A_WORK_TREE)
    fields = probe.stdout.decode("utf-8", "replace").split("\n")
    if len(fields) < 5 or fields[0] != "true":
        raise FileToolError(NOT_A_WORK_TREE)
    top, git_dir, common_dir, objects = (_real(item) for item in fields[1:5])
    for location in (top, git_dir, common_dir, objects):
        # The same answer as a folder that is not a repository, so a .git file or
        # symlink never reveals whether something outside is a Git directory.
        if not _inside(location, roots) or has_secret_component(location):
            raise FileToolError(NOT_A_WORK_TREE)
    if not _free_of_links(git_dir, common_dir, objects) or _names_alternates(objects):
        raise FileToolError(UNSUPPORTED_REPOSITORY)
    # From here on Git uses exactly the admitted locations and never discovers others.
    located = {"GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(top)}
    listing = run_git(("config", "--list", "-z"), cwd=top, env=git_environment(git, located),
                      deadline=deadline)
    if listing.returncode != 0 or listing.truncated:
        raise FileToolError(GIT_FAILED)
    env = git_environment(git, located, _filter_overrides(listing.stdout))
    display = relative_display(top, deepest_root(top, roots))
    return Repository(top, display, env, deadline)


def _names_alternates(objects: Path) -> bool:
    """Whether the object store borrows objects from elsewhere.

    ``info/alternates`` is read like any extended-tool file: without following a
    symlink, non-blocking, and bounded. Anything other than a missing file or a
    small regular file counts as borrowing, so a symlink, a special file, or an
    oversized file is refused with the same message whatever it points at.
    """
    info = objects / "info"
    try:
        # Checked without following first, so no platform's fallback read can tell
        # a dangling symlink from a missing file.
        if info.is_symlink() or (info / "alternates").is_symlink():
            return True
        data = read_admitted_bytes(info / "alternates", ALTERNATES_MAX_BYTES)
    except FileToolError as exc:
        return str(exc) != FILE_NOT_FOUND
    except OSError:  # includes PathNotAllowed
        return True
    # Git splits on LF only and skips just empty and comment lines; a line of
    # spaces or a carriage return is a relative path to Git, so it counts here too.
    return any(line and not line.startswith(b"#") for line in data.split(b"\n"))


def _free_of_links(*directories: Path) -> bool:
    """Whether the Git directories hold only plain folders and regular files.

    Git follows symlinks inside its own directory (refs, packed-refs, packs,
    loose objects, the index), so a link planted there could read another
    repository from outside the roots. The walk never follows a link, skips
    ``hooks`` (hooks never run), and gives up after GIT_DIR_MAX_ENTRIES entries.
    """
    tops = {folder for folder in directories
            if not any(folder != other and folder.is_relative_to(other) for other in directories)}
    pending, seen = sorted(tops), 0
    try:
        while pending:
            folder = pending.pop()
            with os.scandir(folder) as entries:
                for entry in entries:
                    seen += 1
                    if seen > GIT_DIR_MAX_ENTRIES or entry.is_symlink():
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        if not (folder in tops and entry.name == "hooks"):
                            pending.append(Path(entry.path))
                    elif not entry.is_file(follow_symlinks=False):
                        return False
    except OSError:
        return False
    return True


def _filter_overrides(listing: bytes) -> list[tuple[str, str]]:
    """Empty commands for every filter driver the repository configures."""
    drivers = set()
    for entry in listing.split(b"\0"):
        key = entry.split(b"\n", 1)[0].decode("utf-8", "surrogateescape")
        section, _, rest = key.partition(".")
        driver, _, variable = rest.rpartition(".")
        if section == "filter" and driver and variable in FILTER_COMMANDS:
            drivers.add(driver)
    overrides = []
    for driver in sorted(drivers):
        overrides += [(f"filter.{driver}.{command}", "") for command in FILTER_COMMANDS]
        overrides.append((f"filter.{driver}.required", "false"))
    return overrides


# Revisions and paths --------------------------------------------------------------------------

def check_revision(value: Any) -> str:
    """A ref name, commit hash, or HEAD, optionally followed by ``~N`` or ``^N``.

    Ranges, reflog and upstream forms (``@{...}``), peeling (``^{...}``),
    ``rev:path``, searches, whitespace, and anything starting with ``-`` are
    refused before Git sees them.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= REVISION_MAX_CHARS:
        raise FileToolError(INVALID_REVISION)
    if not _REVISION.fullmatch(value) or ".." in value:
        raise FileToolError(INVALID_REVISION)
    base = re.split(r"[~^]", value, maxsplit=1)[0]
    if any(part.endswith((".", ".lock")) for part in base.split("/")):
        raise FileToolError(INVALID_REVISION)
    return value


def resolve_commit(repository: Repository, revision: str) -> str:
    """The full hash of the commit a checked revision names."""
    result = repository.git("rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}")
    oid = result.stdout.decode("ascii", "replace").strip()
    if result.returncode != 0 or not _OBJECT_ID.fullmatch(oid):
        raise FileToolError(REVISION_NOT_FOUND)
    return oid


def check_file(value: Any) -> str:
    """A normalized repository-relative path that obeys the secret denylist."""
    if not isinstance(value, str) or not 1 <= len(value) <= FILE_MAX_CHARS:
        raise FileToolError(INVALID_FILE)
    if value[0] in "/-:" or "\\" in value or any(ord(char) < 32 or char == "\x7f" for char in value):
        raise FileToolError(INVALID_FILE)
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise FileToolError(INVALID_FILE)
    normalized = "/".join(parts)
    if has_secret_component(normalized):
        raise PathNotAllowed()
    return normalized


def _protected_pathspecs() -> list[str]:
    """Pathspecs that keep protected paths out of status lists and patches."""
    names = [*SECRET_NAME_PATTERNS, *sorted(SECRET_DIR_NAMES)]
    return [".", *(f":(exclude,glob,icase)**/{name}{tail}" for name in names for tail in ("", "/**"))]


def _text(value: bytes | str, limit: int = TEXT_FIELD_CHARS) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    return text[:limit]


def _fit_patch(patch: str, envelope: dict[str, Any]) -> tuple[str, bool]:
    """Whole lines of ``patch`` that fit the byte cap and the result budget."""
    raw = patch.encode("utf-8")
    cut = len(raw) > PATCH_MAX_BYTES
    if cut:
        patch = raw[:PATCH_MAX_BYTES].decode("utf-8", "ignore")
        patch = patch[:patch.rfind("\n") + 1]
    lines = patch.split("\n")
    kept = _fitting(lines, {**envelope, "diff": "", "truncated": True})
    if kept < len(lines):
        cut = True
        lines = lines[:kept]
        if lines and lines[-1]:
            lines.append("")
    return "\n".join(lines), cut


# git.status -----------------------------------------------------------------------------------

def git_status(arguments: Mapping[str, Any], *, resolve: Callable[[str], Path],
               roots: Sequence[Path]) -> dict[str, Any]:
    repository = open_repository(arguments, resolve, roots)
    result = repository.output(
        "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all",
        "--ignore-submodules=all", "--no-renames", "--", *_protected_pathspecs(),
    )
    fields = result.stdout.split(b"\0")
    if result.truncated:
        fields = fields[:-1]
    head: dict[str, Any] = {"branch": None, "detached": False, "commit": None, "upstream": None,
                            "ahead": None, "behind": None}
    lists: dict[str, list[dict[str, str] | str]] = {
        "staged": [], "unstaged": [], "untracked": [], "conflicted": []}
    for field in fields:
        record = field.decode("utf-8", "replace")
        if record.startswith("# "):
            key, _, value = record[2:].partition(" ")
            if key == "branch.oid" and value != "(initial)":
                head["commit"] = value[:12]
            elif key == "branch.head":
                head["detached"] = value == "(detached)"
                head["branch"] = None if head["detached"] else _text(value)
            elif key == "branch.upstream":
                head["upstream"] = _text(value)
            elif key == "branch.ab":
                ahead, _, behind = value.partition(" ")
                head["ahead"], head["behind"] = int(ahead.lstrip("+")), int(behind.lstrip("-"))
        elif record.startswith("1 "):
            parts = record.split(" ", 8)
            if len(parts) == 9:
                staged, unstaged = parts[1][0], parts[1][1]
                if staged != ".":
                    lists["staged"].append({"path": parts[8], "change": _CHANGES.get(staged, staged)})
                if unstaged != ".":
                    lists["unstaged"].append({"path": parts[8], "change": _CHANGES.get(unstaged, unstaged)})
        elif record.startswith("u "):
            parts = record.split(" ", 10)
            if len(parts) == 11:
                lists["conflicted"].append({"path": parts[10], "change": parts[1]})
        elif record.startswith("? "):
            lists["untracked"].append(record[2:])
    counts = {name: len(items) for name, items in lists.items()}
    payload: dict[str, Any] = {"path": repository.display, **head, "counts": counts,
                               **{name: items[:STATUS_MAX_ENTRIES] for name, items in lists.items()},
                               "truncated": result.truncated or any(
                                   count > STATUS_MAX_ENTRIES for count in counts.values())}
    while len(encode_result(payload).encode("utf-8")) > OUTPUT_BUDGET_BYTES - 512:
        longest = max(lists, key=lambda name: len(payload[name]))
        payload[longest] = payload[longest][:-1]
        payload["truncated"] = True
    return payload


# git.log --------------------------------------------------------------------------------------

def git_log(arguments: Mapping[str, Any], *, resolve: Callable[[str], Path],
            roots: Sequence[Path]) -> dict[str, Any]:
    limit = _option(arguments, "limit", LOG_DEFAULT_COMMITS)
    file = _option(arguments, "file", None)
    pathspec = [] if file is None else [f":(literal){check_file(file)}"]
    repository = open_repository(arguments, resolve, roots)
    payload: dict[str, Any] = {"path": repository.display, "file": None if file is None else check_file(file),
                               "commits": [], "count": 0, "truncated": False}
    if repository.git("rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        return payload  # no commits yet
    result = repository.output(
        "log", "--no-color", "--no-show-signature", "--no-notes", "-z", f"--max-count={limit + 1}",
        "--format=%h%x1f%aI%x1f%an%x1f%s", "--end-of-options", "HEAD", "--", *pathspec,
    )
    records = [record for record in result.stdout.split(b"\0") if record]
    if result.truncated:
        records = records[:-1]
    commits = []
    for record in records:
        parts = record.split(b"\x1f", 3)
        if len(parts) == 4:
            commits.append({"commit": _text(parts[0]), "date": _text(parts[1]),
                            "author": _text(parts[2]), "subject": _text(parts[3])})
    payload["truncated"] = len(commits) > limit or result.truncated
    payload["commits"] = commits[:limit]
    payload["count"] = len(payload["commits"])
    return payload


# git.diff -------------------------------------------------------------------------------------

def git_diff(arguments: Mapping[str, Any], *, resolve: Callable[[str], Path],
             roots: Sequence[Path]) -> dict[str, Any]:
    staged = bool(_option(arguments, "staged", False))
    from_revision = _option(arguments, "from_revision", None)
    to_revision = _option(arguments, "to_revision", None)
    if to_revision is not None and from_revision is None:
        raise FileToolError(TO_WITHOUT_FROM)
    if staged and from_revision is not None:
        raise FileToolError(MODE_CONFLICT)
    if from_revision is not None:
        check_revision(from_revision)
        check_revision(to_revision if to_revision is not None else "HEAD")
    repository = open_repository(arguments, resolve, roots)
    envelope: dict[str, Any] = {"path": repository.display}
    args = ["diff", *DIFF_SAFETY]
    if from_revision is not None:
        to_revision = to_revision if to_revision is not None else "HEAD"
        first, second = resolve_commit(repository, from_revision), resolve_commit(repository, to_revision)
        envelope.update(mode="revisions", from_revision=from_revision, to_revision=to_revision,
                        from_commit=first[:12], to_commit=second[:12])
        args += ["--end-of-options", first, second]
    elif staged:
        envelope["mode"] = "staged"
        args.append("--cached")
    else:
        envelope["mode"] = "working"
    result = repository.output(*args, "--", *_protected_pathspecs(), limit=PATCH_MAX_BYTES + 1)
    patch, cut = _fit_patch(result.stdout.decode("utf-8", "replace"), envelope)
    return {**envelope, "diff": patch, "truncated": cut or result.truncated}


# git.show -------------------------------------------------------------------------------------

def git_show(arguments: Mapping[str, Any], *, resolve: Callable[[str], Path],
             roots: Sequence[Path]) -> dict[str, Any]:
    revision = check_revision(arguments["revision"])
    file = _option(arguments, "file", None)
    normalized = None if file is None else check_file(file)
    repository = open_repository(arguments, resolve, roots)
    oid = resolve_commit(repository, revision)
    envelope: dict[str, Any] = {"path": repository.display, "revision": revision, "commit": oid[:12]}
    if normalized is not None:
        return _show_file(repository, oid, normalized, envelope)
    meta = repository.output("log", "-1", "--no-color", "--no-show-signature", "--no-notes",
                             "--format=%aI%x1f%an%x1f%p%x1f%s%x1f%b", "--end-of-options", oid)
    parts = meta.stdout.decode("utf-8", "replace").split("\x1f", 4)
    if len(parts) != 5:
        raise FileToolError(GIT_FAILED)
    body = parts[4].strip()
    envelope.update(date=parts[0], author=_text(parts[1]), parents=parts[2].split(),
                    subject=_text(parts[3]), body=body[:SHOW_BODY_CHARS])
    if len(body) > SHOW_BODY_CHARS:
        envelope["body_truncated"] = True
    patch = repository.output("show", "--format=", "--patch", "--no-show-signature", "--no-notes",
                              *DIFF_SAFETY, "--end-of-options", oid, "--", *_protected_pathspecs(),
                              limit=PATCH_MAX_BYTES + 1)
    text, cut = _fit_patch(patch.stdout.decode("utf-8", "replace"), envelope)
    return {**envelope, "diff": text, "truncated": cut or patch.truncated}


def _show_file(repository: Repository, oid: str, file: str, envelope: dict[str, Any]) -> dict[str, Any]:
    spec = f"{oid}:{file}"
    kind = repository.git("cat-file", "-t", spec)
    if kind.returncode != 0:
        raise FileToolError(FILE_NOT_AT_REVISION)
    if kind.stdout.strip() != b"blob":
        raise FileToolError(NOT_A_FILE_AT_REVISION)
    content = repository.output("cat-file", "blob", spec)
    data = content.stdout
    if content.truncated:
        data = data[:data.rfind(b"\n") + 1]
    text = decode_text(data)
    if text is None:
        raise FileToolError(NOT_TEXT)
    lines = split_lines(text)
    window = lines[:READ_MAX_LINES]
    shown = [line[:READ_MAX_LINE_CHARS] for line in window]
    base = {**envelope, "file": file, "lines": [], "line_count": 0,
            "total_lines": None if content.truncated else len(lines), "truncated": True, "cut_lines": []}
    kept = _fitting(shown, base)
    cut = [offset + 1 for offset, line in enumerate(window[:kept]) if len(line) > READ_MAX_LINE_CHARS]
    result = {**base, "lines": shown[:kept], "line_count": kept,
              "truncated": content.truncated or kept < len(lines) or bool(cut)}
    del result["cut_lines"]
    if cut:
        result["cut_lines"] = cut
    return result
