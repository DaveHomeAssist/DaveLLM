"""Hostile Git repositories for the read-only Git tool tests.

``build_hostile_git`` builds a disposable base directory holding ``root`` (the
only tool root), ``outside`` (Git data the root must never reach), ``bin``
(planted programs), and ``markers``. Every planted program only creates a
marker file named after itself, so a test proves that nothing ran by checking
that ``markers`` stays empty. Repositories are built with plain Git first; the
hostile configuration is added afterwards, so building never runs a planted
program.

Repositories under ``root``:

- ``repo``: history with a branch, lightweight and annotated tags, a commit
  carrying a fake signature, a tracked ``.env``, and a working tree with a
  staged change, an unstaged change, untracked files, and protected files.
- ``tracking``: a clean tree one commit ahead of and one behind its upstream.
- ``detached``: HEAD detached at an older commit.
- ``history``: 60 commits made with fast-import; commit 7 holds a unique fact.
- ``big``: output large enough to truncate every tool.
- ``promisor``: a partial clone with a missing blob and an ``ext::`` remote.
- ``linked`` (``.git`` file to an outside Git directory), ``symgit`` (``.git``
  symlink to an outside directory), ``escaped`` (``core.worktree`` outside),
  ``alternates`` (objects borrowed from outside), ``vendor/embedded.git`` (a
  bare repository), ``plain`` (no repository), and ``link-repo`` (a symlink
  to a repository outside the root).
"""

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


PLANTED = (
    "fsmonitor", "pager", "external-diff", "diff-driver", "textconv", "clean", "smudge", "process",
    "credential", "askpass", "ssh", "gpg", "hook", "env-external-diff", "env-pager", "env-askpass",
    "env-ssh", "env-fsmonitor", "lazy-fetch",
)
HOOKS = ("post-index-change", "reference-transaction", "post-checkout", "pre-commit", "post-commit")
SECRET_TEXT = "hostile-git-sentinel::dotenv"
UNIQUE_FACT = "The rollback token is heron-31."
AUTHOR = "Fixture Author"


@dataclass(frozen=True)
class HostileGit:
    base: Path
    root: Path
    outside: Path
    bin: Path
    markers: Path

    def at(self, relative: str) -> Path:
        return self.root / relative

    def planted(self, name: str) -> Path:
        return self.bin / name

    def fired(self) -> list[str]:
        """Names of planted programs that ran."""
        return sorted(path.name for path in self.markers.iterdir())

    def snapshot(self) -> dict[str, str]:
        """Every entry under ``base`` except ``markers``, with content digests and link targets."""
        state = {}
        for directory, dirnames, filenames in os.walk(self.base, followlinks=False):
            for name in dirnames + filenames:
                path = Path(directory, name)
                key = path.relative_to(self.base).as_posix()
                if key == "markers" or key.startswith("markers/"):
                    continue
                if path.is_symlink():
                    state[key] = f"link:{os.readlink(path)}"
                elif path.is_dir():
                    state[key] = "dir"
                else:
                    stat = path.stat()
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    state[key] = f"file:{digest}:{stat.st_mode:o}:{stat.st_mtime_ns}"
        return state


class _Builder:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.clock = 1_700_000_000
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(base),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": AUTHOR,
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": AUTHOR,
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
        }

    def git(self, repo: Path, *args: str, stdin: bytes | None = None) -> str:
        self.clock += 60
        stamp = f"{self.clock} +0000"
        env = dict(self.env, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        result = subprocess.run(["git", *args], cwd=repo, env=env, input=stdin, capture_output=True, check=True)
        return result.stdout.decode()

    def init(self, repo: Path, *extra: str) -> Path:
        repo.mkdir(parents=True, exist_ok=True)
        self.git(repo, "init", "-q", "-b", "main", *extra)
        return repo

    def commit(self, repo: Path, message: str, files: dict[str, str]) -> str:
        for relative, text in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git(repo, "add", "--", *files)
        self.git(repo, "commit", "-q", "-m", message)
        return self.git(repo, "rev-parse", "HEAD").strip()


def _plant(base: Path) -> tuple[Path, Path, Path]:
    bin_dir, markers, hooks = base / "bin", base / "markers", base / "hooks"
    for folder in (bin_dir, markers, hooks):
        folder.mkdir()
    for name in PLANTED:
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\ntouch '{markers / name}'\nexit 0\n")
        script.chmod(0o755)
    for hook in HOOKS:
        script = hooks / hook
        script.write_text(f"#!/bin/sh\ntouch '{markers / 'hook'}'\nexit 0\n")
        script.chmod(0o755)
    return bin_dir, markers, hooks


def hostile_config(bin_dir: Path, hooks: Path, trace_target: Path) -> str:
    """Repository configuration that tries to run a planted program on every read path."""
    b = bin_dir
    return f"""[core]
\tfsmonitor = {b / 'fsmonitor'}
\tpager = {b / 'pager'}
\taskPass = {b / 'askpass'}
\tsshCommand = {b / 'ssh'}
\thooksPath = {hooks}
\tuntrackedCache = true
[diff]
\texternal = {b / 'external-diff'}
[diff "evil"]
\ttextconv = {b / 'textconv'}
\tcommand = {b / 'diff-driver'}
[filter "evil"]
\tclean = {b / 'clean'}
\tsmudge = {b / 'smudge'}
\trequired = true
[filter "proc"]
\tprocess = {b / 'process'}
\trequired = true
[credential]
\thelper = {b / 'credential'}
[pager]
\tstatus = {b / 'pager'}
\tdiff = {b / 'pager'}
\tlog = {b / 'pager'}
\tshow = {b / 'pager'}
[log]
\tshowSignature = true
[gpg]
\tprogram = {b / 'gpg'}
[status]
\tsubmoduleSummary = true
[trace2]
\teventTarget = {trace_target}
[protocol "ext"]
\tallow = always
"""


def _harden_repo(repo: Path, config: str, git_dir: Path | None = None) -> None:
    git_dir = git_dir or repo / ".git"
    with open(git_dir / "config", "a") as handle:
        handle.write(config)
    info = git_dir / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text("* filter=proc\n")


def _signed_head(builder: _Builder, repo: Path) -> None:
    """Replace HEAD with a copy of itself carrying a fake GPG signature header."""
    raw = builder.git(repo, "cat-file", "commit", "HEAD")
    header, _, message = raw.partition("\n\n")
    signature = "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEEfake\n -----END PGP SIGNATURE-----"
    signed = f"{header}\n{signature}\n\n{message}"
    oid = builder.git(repo, "hash-object", "-t", "commit", "-w", "--stdin", stdin=signed.encode()).strip()
    builder.git(repo, "update-ref", "refs/heads/main", oid)


def _history(builder: _Builder, repo: Path, count: int) -> None:
    stream = []
    for number in range(1, count + 1):
        stamp = 1_700_100_000 + number * 3_600
        message = f"History entry {number}\n"
        content = f"entry {number}\n" + (UNIQUE_FACT + "\n" if number == 7 else "")
        stream.append(
            f"commit refs/heads/main\nauthor {AUTHOR} <author@example.invalid> {stamp} +0000\n"
            f"committer {AUTHOR} <author@example.invalid> {stamp} +0000\n"
            f"data {len(message.encode())}\n{message}"
            f"M 644 inline log.txt\ndata {len(content.encode())}\n{content}\n"
        )
    builder.git(repo, "fast-import", "--quiet", stdin="".join(stream).encode())
    builder.git(repo, "reset", "-q", "--hard", "main")


def build_hostile_git(base: Path) -> HostileGit:
    base.mkdir(parents=True)
    base = base.resolve()
    root, outside = base / "root", base / "outside"
    root.mkdir()
    outside.mkdir()
    bin_dir, markers, hooks = _plant(base)
    config = hostile_config(bin_dir, hooks, root / "trace2.json")
    builder = _Builder(base)

    repo = builder.init(root / "repo")
    builder.commit(repo, "Initial import", {
        "README.md": "# Widget\n\nFirst line.\n",
        "app.py": "print('widget')\n",
        ".gitattributes": "* diff=evil filter=evil\n",
        ".env": f"TOKEN={SECRET_TEXT}\n",
        "docs/guide.md": "# Guide\n",
    })
    builder.git(repo, "tag", "v1.0")
    builder.commit(repo, "Add config loader", {"config.py": "LOADED = True\n"})
    builder.git(repo, "tag", "-a", "v1.1", "-m", "Release 1.1")
    builder.git(repo, "branch", "feature")
    builder.commit(repo, "Document the widget", {"docs/guide.md": "# Guide\n\nUse the widget.\n"})
    _signed_head(builder, repo)
    (repo / "app.py").write_text("print('widget v2')\n")
    builder.git(repo, "add", "app.py")
    (repo / "README.md").write_text("# Widget\n\nFirst line.\nSecond line.\n")
    (repo / ".env").write_text(f"TOKEN={SECRET_TEXT}-changed\n")
    (repo / "todo.txt").write_text("remember the milk\n")
    (repo / ".ssh").mkdir()
    (repo / ".ssh" / "id_ed25519").write_text(f"{SECRET_TEXT}-key\n")
    _harden_repo(repo, config)

    tracking = builder.init(root / "tracking")
    builder.commit(tracking, "Base", {"notes.txt": "base\n"})
    builder.commit(tracking, "Shared", {"notes.txt": "base\nshared\n"})
    shared = builder.git(tracking, "rev-parse", "HEAD").strip()
    builder.commit(tracking, "Local only", {"local.txt": "local\n"})
    builder.git(tracking, "checkout", "-q", "-b", "upstream-side", shared)
    builder.commit(tracking, "Upstream only", {"remote.txt": "remote\n"})
    builder.git(tracking, "update-ref", "refs/remotes/origin/main", "HEAD")
    builder.git(tracking, "checkout", "-q", "main")
    builder.git(tracking, "branch", "-q", "-D", "upstream-side")
    builder.git(tracking, "config", "remote.origin.url", str(base / "no-such-remote.git"))
    builder.git(tracking, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    builder.git(tracking, "config", "branch.main.remote", "origin")
    builder.git(tracking, "config", "branch.main.merge", "refs/heads/main")
    _harden_repo(tracking, config)

    detached = builder.init(root / "detached")
    builder.commit(detached, "One", {"a.txt": "1\n"})
    builder.commit(detached, "Two", {"a.txt": "2\n"})
    builder.git(detached, "checkout", "-q", "--detach", "HEAD~1")
    _harden_repo(detached, config)

    history = builder.init(root / "history")
    _history(builder, history, 60)
    _harden_repo(history, config)

    big = builder.init(root / "big")
    builder.commit(big, "Big file", {"big.txt": "".join(f"line {n} {'x' * 40}\n" for n in range(1, 1001))})
    (big / "big.txt").write_text("".join(f"changed {n} {'y' * 60}\n" for n in range(1, 5001)))
    for number in range(150):
        (big / f"untracked-{number:03d}.txt").write_text("u\n")
    _harden_repo(big, config)

    promisor = builder.init(root / "promisor")
    builder.commit(promisor, "Has a blob", {"f.txt": "blob that will go missing\n"})
    blob = builder.git(promisor, "rev-parse", "HEAD:f.txt").strip()
    (promisor / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    builder.git(promisor, "config", "core.repositoryformatversion", "1")
    builder.git(promisor, "config", "extensions.partialClone", "origin")
    builder.git(promisor, "config", "remote.origin.url", f"ext::sh -c touch% {markers / 'lazy-fetch'}")
    builder.git(promisor, "config", "remote.origin.promisor", "true")
    _harden_repo(promisor, config)

    builder.init(root / "linked", "--separate-git-dir", str(outside / "linked.git"))
    builder.commit(root / "linked", "Linked", {"a.txt": "linked\n"})

    symgit = root / "symgit"
    builder.init(outside / "symgit-src")
    builder.commit(outside / "symgit-src", "Sym", {"a.txt": "sym\n"})
    symgit.mkdir()
    (symgit / "a.txt").write_text("sym\n")
    (symgit / ".git").symlink_to(outside / "symgit-src" / ".git", target_is_directory=True)

    escaped = builder.init(root / "escaped")
    builder.commit(escaped, "Escaped", {"a.txt": "escaped\n"})
    (outside / "worktree").mkdir()
    builder.git(escaped, "config", "core.worktree", str(outside / "worktree"))

    altsrc = builder.init(outside / "altsrc")
    builder.commit(altsrc, "Borrowed", {"a.txt": "borrowed\n"})
    alternates = builder.init(root / "alternates")
    (alternates / ".git" / "objects" / "info" / "alternates").write_text(f"{altsrc / '.git' / 'objects'}\n")

    embedded = root / "vendor" / "embedded.git"
    embedded.mkdir(parents=True)
    builder.git(embedded, "init", "-q", "--bare")
    _harden_repo(embedded, config, git_dir=embedded)

    (root / "plain").mkdir()
    (root / "plain" / "file.txt").write_text("not a repository\n")
    outside_repo = builder.init(outside / "repo")
    builder.commit(outside_repo, "Outside", {"secret.txt": "outside repository\n"})
    (root / "link-repo").symlink_to(outside_repo, target_is_directory=True)

    fired = sorted(path.name for path in markers.iterdir())
    assert not fired, f"building the fixture ran planted programs: {fired}"
    return HostileGit(base=base, root=root, outside=outside, bin=bin_dir, markers=markers)
