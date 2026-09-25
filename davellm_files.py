"""DaveLLM-owned helpers shared by extended file, Markdown, and repository tools.

These are pure functions over caller-supplied roots. They read no environment
and register no tools. The one policy they own is the secret-path denylist:
extended tools admit every requested path through ``admit_path`` around the
existing tool-root check, and ``walk_tree`` never lists or enters secrets.
"""

from __future__ import annotations

import codecs
import fnmatch
import hashlib
import hmac
import json
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, Iterable, Mapping, Sequence, TypeVar


DEFAULT_IGNORED_DIRS = frozenset({".git", "node_modules", "venv", "__pycache__", ".trash"})
BINARY_SNIFF_BYTES = 8_192
CURSOR_VERSION = "c1"
_CURSOR_OFFSET_DIGITS = 12

# Every pattern applies to every path component, case-insensitively, because
# macOS and Windows volumes usually are. A directory named like a secret file
# is blocked too.
SECRET_NAME_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_rsa*", "*.p12")
SECRET_DIR_NAMES = frozenset({".ssh", ".aws", ".gnupg"})
PATH_NOT_ALLOWED = "Access denied: path is not allowed"

T = TypeVar("T")


class PathNotAllowed(PermissionError):
    """The single refusal for every blocked path, whatever exists on disk."""

    def __init__(self) -> None:
        super().__init__(PATH_NOT_ALLOWED)


def is_secret_name(name: str) -> bool:
    folded = name.casefold()
    return folded in SECRET_DIR_NAMES or any(
        fnmatch.fnmatchcase(folded, pattern) for pattern in SECRET_NAME_PATTERNS
    )


def has_secret_component(path: str | os.PathLike[str]) -> bool:
    return any(is_secret_name(part) for part in Path(path).parts)


def _still_linked(path: Path) -> bool:
    # A fully resolved path has no symlink left in it. One that remains is a
    # loop, which only some Python versions report while resolving.
    return any(os.path.islink(item) for item in (path, *path.parents))


def admit_path(requested: str, resolve: Callable[[str], Path]) -> Path:
    """Apply the secret denylist around an existing containment resolver.

    The path is checked as written (so ``foo/../.ssh`` fails before any
    filesystem access), resolved by ``resolve``, which must enforce the tool
    roots, and checked again after symlinks are followed. Every refusal,
    including a containment or resolution failure, raises the same
    ``PathNotAllowed`` with no cause or context attached.
    """
    if not isinstance(requested, str) or not requested:
        raise PathNotAllowed()
    if has_secret_component(requested) or has_secret_component(os.path.expanduser(requested)):
        raise PathNotAllowed()
    resolved: Path | None
    try:
        resolved = resolve(requested)
    except (OSError, ValueError, RuntimeError):
        resolved = None
    if resolved is None or has_secret_component(resolved) or _still_linked(resolved):
        raise PathNotAllowed()
    return resolved


def _positive_int(name: str, value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def relative_display(path: Path, root: Path) -> str:
    """Lexical POSIX path of ``path`` under ``root``; ``.`` for the root itself."""
    relative = Path(path).relative_to(root)
    if ".." in relative.parts:
        raise ValueError("Path is outside the tool root")
    return relative.as_posix()


def display_path(path: Path, roots: Sequence[Path]) -> str:
    """Show a path relative to the deepest tool root containing it, never absolute.

    The path is resolved first, matching the existing tool containment check.
    """
    resolved = Path(path).expanduser().resolve()
    containing = [
        root for root in (Path(item).resolve() for item in roots)
        if resolved == root or resolved.is_relative_to(root)
    ]
    if not containing:
        raise ValueError("Path is outside the tool roots")
    return relative_display(resolved, max(containing, key=lambda root: len(root.parts)))


@dataclass(frozen=True)
class BoundedText:
    text: str
    truncated: bool
    omitted_chars: int


def bound_text(text: str, max_chars: int) -> BoundedText:
    """Keep the first ``max_chars`` characters and report what was cut."""
    _positive_int("max_chars", max_chars)
    if len(text) <= max_chars:
        return BoundedText(text, False, 0)
    return BoundedText(text[:max_chars], True, len(text) - max_chars)


def cursor_scope(tool_name: str, arguments: Mapping[str, Any]) -> str:
    """Canonical request identity a cursor is bound to, ignoring the cursor itself."""
    request = {key: value for key, value in arguments.items() if key != "cursor"}
    return json.dumps(
        {"tool": tool_name, "arguments": request},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _cursor_digest(offset: int, scope: str) -> str:
    material = f"{CURSOR_VERSION}\0{scope}\0{offset}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def encode_cursor(offset: int, scope: str) -> str:
    """Deterministic continuation token for ``offset`` within one request scope.

    The digest binds a cursor to its request so it cannot be replayed against a
    different listing. It is an integrity check, not a secret or a permission.
    """
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    return f"{CURSOR_VERSION}.{offset}.{_cursor_digest(offset, scope)}"


def decode_cursor(cursor: str, scope: str) -> int:
    parts = cursor.split(".") if isinstance(cursor, str) else []
    if (
        len(parts) != 3 or parts[0] != CURSOR_VERSION
        or not (parts[1].isascii() and parts[1].isdigit())
        or len(parts[1]) > _CURSOR_OFFSET_DIGITS
    ):
        raise ValueError("Invalid cursor")
    offset = int(parts[1])
    if not hmac.compare_digest(parts[2], _cursor_digest(offset, scope)):
        raise ValueError("Cursor does not belong to this request")
    return offset


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    offset: int
    total: int
    truncated: bool
    next_cursor: str | None


def paginate(
    items: Sequence[T], *, page_size: int, scope: str, cursor: str | None = None,
) -> Page[T]:
    """Return one page of ``items`` and a cursor for the next page, if any."""
    _positive_int("page_size", page_size)
    offset = 0 if cursor is None else decode_cursor(cursor, scope)
    if offset > len(items):
        raise ValueError("Cursor is past the end of the results")
    end = min(offset + page_size, len(items))
    more = end < len(items)
    return Page(
        tuple(items[offset:end]), offset, len(items), more,
        encode_cursor(end, scope) if more else None,
    )


def looks_binary(sample: bytes) -> bool:
    """Treat NUL bytes or invalid UTF-8 as binary; a cut final character is fine."""
    if b"\x00" in sample:
        return True
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return True
    return False


def is_text_file(path: Path, sample_bytes: int = BINARY_SNIFF_BYTES) -> bool:
    _positive_int("sample_bytes", sample_bytes)
    with open(path, "rb") as handle:
        return not looks_binary(handle.read(sample_bytes))


@dataclass(frozen=True)
class WalkEntry:
    path: Path
    display: str
    kind: str
    depth: int
    size: int | None


@dataclass(frozen=True)
class WalkResult:
    entries: tuple[WalkEntry, ...]
    truncated: bool
    depth_limited: bool
    ignored_dirs: int
    unreadable_dirs: int


def _entry_kind(entry: os.DirEntry[str]) -> str:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir(follow_symlinks=False):
        return "dir"
    if entry.is_file(follow_symlinks=False):
        return "file"
    return "other"


def _has_children(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            return any(not is_secret_name(entry.name) for entry in entries)
    except OSError:
        return False


def _link_stays_inside(path: Path, root: Path) -> bool:
    try:
        target = path.resolve()
    except (OSError, RuntimeError):
        return False
    return (
        (target == root or target.is_relative_to(root))
        and not has_secret_component(target) and not _still_linked(target)
    )


def walk_tree(
    start: Path, root: Path, *, max_depth: int, max_entries: int,
    ignored_dirs: Iterable[str] = DEFAULT_IGNORED_DIRS, include_dirs: bool = True,
) -> WalkResult:
    """Breadth-first, name-sorted walk of ``start`` that never follows symlinks.

    Direct children of ``start`` are depth 1, and directories are only entered
    below ``max_depth``. ``truncated`` means more entries existed past
    ``max_entries``; ``depth_limited`` means a directory at the depth limit still
    had children. Ignored directories are neither listed nor entered.

    Secrets are skipped silently: they are never listed, entered, counted, or
    allowed to set ``truncated`` or ``depth_limited``. A symlink is listed only
    when its target stays inside ``root`` and is not a secret.
    """
    _positive_int("max_depth", max_depth)
    _positive_int("max_entries", max_entries)
    start = Path(start)
    resolved_start: Path | None
    try:
        resolved_start = start.resolve()
    except (OSError, RuntimeError):
        resolved_start = None
    if resolved_start is None or has_secret_component(start) or has_secret_component(resolved_start):
        raise PathNotAllowed()
    relative_display(start, root)
    if start.is_symlink() or not start.is_dir():
        raise ValueError("Walk start must be a directory")
    resolved_root = Path(root).resolve()
    ignore = frozenset(ignored_dirs)
    entries: list[WalkEntry] = []
    truncated = depth_limited = False
    ignored = unreadable = 0
    queue: deque[tuple[Path, int]] = deque([(start, 0)])
    while queue and not truncated:
        directory, depth = queue.popleft()
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda item: item.name)
        except OSError:
            unreadable += 1
            continue
        for child in children:
            if is_secret_name(child.name):
                continue
            kind = _entry_kind(child)
            if kind == "dir" and child.name in ignore:
                ignored += 1
                continue
            path = Path(child.path)
            if kind == "symlink" and not _link_stays_inside(path, resolved_root):
                continue
            child_depth = depth + 1
            if kind == "dir":
                if child_depth < max_depth:
                    queue.append((path, child_depth))
                elif _has_children(path):
                    depth_limited = True
                if not include_dirs:
                    continue
            if len(entries) >= max_entries:
                truncated = True
                break
            size = child.stat(follow_symlinks=False).st_size if kind == "file" else None
            entries.append(WalkEntry(path, relative_display(path, root), kind, child_depth, size))
    return WalkResult(tuple(entries), truncated, depth_limited, ignored, unreadable)
