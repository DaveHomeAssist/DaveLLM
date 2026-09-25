"""A hostile filesystem for extended-tool security tests.

``build_hostile_tree`` builds a disposable base directory holding ``root`` (the
only tool root) and ``outside`` (a sibling the root must never reach). The
root holds ordinary files next to planted secrets, ignored folders, binary and
oversized data, and symlinks that stay inside, leave the root, chain out of
it, point at secrets, or loop. Every planted secret and every outside file
contains a unique sentinel, so a test can prove none of them reached a tool
result. ``snapshot`` records the whole base so a test can prove zero effects.

Tests get it through the ``hostile_tree`` fixture in ``conftest.py``.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


SENTINEL_PREFIX = "hostile-sentinel::"
OVERSIZED_BYTES = 1_048_577  # one byte past 1 MiB, the planned per-file scan limit

NORMAL_FILES = {
    "README.md": "# Hostile fixture\n\nOrdinary project text.\n",
    "docs/guide.md": "# Guide\n\n## Setup\n\nRun the thing.\n",
    "docs/nested/deep/note.txt": "A deep but ordinary note.\n",
    "src/app.py": "print('hello')\n",
    "keys/README.txt": "Ordinary file in an ordinarily named folder.\n",
}
IGNORED_FILES = (
    ".git/config",
    "node_modules/pkg/index.js",
    "venv/bin/activate",
    "__pycache__/app.cpython-312.pyc",
    ".trash/old.txt",
)
SECRET_FILES = (
    ".env",
    ".env.local",
    "config/.env.production",
    "certs/server.pem",
    "certs/server.key",
    "certs/client.p12",
    "id_rsa",
    "keys/id_rsa.pub",
    ".ssh/config",
    ".ssh/id_ed25519",
    ".aws/credentials",
    ".gnupg/pubring.kbx",
    "docs/nested/.ssh/known_hosts",
)
SECRET_DIRS = (".ssh", ".aws", ".gnupg", "docs/nested/.ssh")
OUTSIDE_FILES = ("secret.txt", "notes.txt")
BINARY_FILE = "data/blob.bin"
OVERSIZED_FILE = "data/large.log"

INTERNAL_LINK = "links/internal.md"
OUTSIDE_LINK = "links/outside.txt"
OUTSIDE_DIR_LINK = "links/outside_dir"
CHAIN_LINK = "links/chain_a"
SECRET_FILE_LINK = "links/to_env"
SECRET_DIR_LINK = "links/to_ssh"
LOOP_LINK = "links/loop_a"


def sentinel(label: str) -> str:
    return f"{SENTINEL_PREFIX}{label}"


@dataclass(frozen=True)
class HostileTree:
    base: Path
    root: Path
    outside: Path

    def at(self, relative: str) -> Path:
        return self.root / relative

    @property
    def sentinels(self) -> frozenset[str]:
        return frozenset(
            [sentinel(item) for item in SECRET_FILES]
            + [sentinel(f"outside/{item}") for item in OUTSIDE_FILES]
        )

    def leaked(self, *texts: object) -> set[str]:
        """Sentinels that appear in any of ``texts``."""
        joined = "\n".join(str(text) for text in texts if text is not None)
        return {item for item in self.sentinels if item in joined}

    def snapshot(self) -> dict[str, str]:
        """Every entry under ``base`` with its kind and content digest or link target."""
        state = {}
        for directory, dirnames, filenames in os.walk(self.base, followlinks=False):
            for name in dirnames + filenames:
                path = Path(directory, name)
                key = path.relative_to(self.base).as_posix()
                if path.is_symlink():
                    state[key] = f"link:{os.readlink(path)}"
                elif path.is_dir():
                    state[key] = "dir"
                else:
                    state[key] = f"file:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        return state


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def _link(link: Path, target: str | Path, *, directory: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=directory)


def build_hostile_tree(base: Path) -> HostileTree:
    base.mkdir(parents=True)
    base = base.resolve()
    tree = HostileTree(base=base, root=base / "root", outside=base / "outside")

    for relative, text in NORMAL_FILES.items():
        _write(tree.at(relative), text)
    for relative in IGNORED_FILES:
        _write(tree.at(relative), f"ignored: {relative}\n")
    for relative in SECRET_FILES:
        _write(tree.at(relative), sentinel(relative) + "\n")
    for name in OUTSIDE_FILES:
        _write(tree.outside / name, sentinel(f"outside/{name}") + "\n")
    _write(tree.at(BINARY_FILE), b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + bytes(range(256)))
    _write(tree.at(OVERSIZED_FILE), "x" * OVERSIZED_BYTES)

    _link(tree.at(INTERNAL_LINK), "../docs/guide.md")
    _link(tree.at(OUTSIDE_LINK), tree.outside / "secret.txt")
    _link(tree.at(OUTSIDE_DIR_LINK), tree.outside, directory=True)
    _link(tree.at("links/chain_c"), "../../outside/notes.txt")
    _link(tree.at("links/chain_b"), "chain_c")
    _link(tree.at(CHAIN_LINK), "chain_b")
    _link(tree.at(SECRET_FILE_LINK), "../.env")
    _link(tree.at(SECRET_DIR_LINK), "../.ssh", directory=True)
    _link(tree.at("links/loop_b"), "loop_a")
    _link(tree.at(LOOP_LINK), "loop_b")
    return tree
