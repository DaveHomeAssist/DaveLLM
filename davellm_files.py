"""DaveLLM-owned helpers and implementations for the extended file tools.

Everything here works over caller-supplied roots and resolvers. It reads no
environment and registers nothing; app.py wires ``list_files``,
``search_files``, and ``read_lines`` to the registry. The policies owned here
are the secret-path denylist (``admit_path`` around the existing tool-root
check, and ``walk_tree`` never listing or entering secrets) and the
symlink-safe open used for every file read (``read_admitted_bytes``).
"""

from __future__ import annotations

import codecs
import errno
import fnmatch
import hashlib
import hmac
import json
import os
import stat
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
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


def deepest_root(path: Path, roots: Sequence[Path]) -> Path:
    """The deepest tool root containing an already resolved ``path``."""
    containing = [
        root for root in (Path(item).resolve() for item in roots)
        if path == root or path.is_relative_to(root)
    ]
    if not containing:
        raise ValueError("Path is outside the tool roots")
    return max(containing, key=lambda root: len(root.parts))


def display_path(path: Path, roots: Sequence[Path]) -> str:
    """Show a path relative to the deepest tool root containing it, never absolute.

    The path is resolved first, matching the existing tool containment check.
    """
    resolved = Path(path).expanduser().resolve()
    return relative_display(resolved, deepest_root(resolved, roots))


AMBIGUOUS_RELATIVE_PATH = "Use an absolute path when several tool roots are configured"


class AmbiguousRelativePath(ValueError):
    def __init__(self) -> None:
        super().__init__(AMBIGUOUS_RELATIVE_PATH)


def anchor_path(requested: str, roots: Sequence[Path]) -> str:
    """Anchor a relative extended-tool path at the tool root, so displayed paths work as input.

    Absolute and ``~`` paths pass through unchanged. A relative path is joined to
    the single configured root; with several roots it is ambiguous and refused.
    The result still goes through ``admit_path``, which owns containment.
    """
    if not isinstance(requested, str) or not requested:
        return requested
    if os.path.isabs(os.path.expanduser(requested)) or not roots:
        return requested
    if len(roots) > 1:
        raise AmbiguousRelativePath()
    return str(Path(roots[0]) / requested)


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
    modified: float | None = None


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
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:  # removed while walking
                continue
            size = info.st_size if kind == "file" else None
            entries.append(WalkEntry(
                path, relative_display(path, root), kind, child_depth, size, info.st_mtime,
            ))
    return WalkResult(tuple(entries), truncated, depth_limited, ignored, unreadable)


# Extended file tools ---------------------------------------------------------
#
# The functions below implement file.list, file.search, and file.read_lines.
# Callers pass ``resolve`` (app.resolve_extended_tool_path: anchoring, the
# secret denylist, and tool-root containment) and the configured roots.
# Every refusal raised here carries a fixed message with no path or OS detail.

FILE_NOT_FOUND = "File not found"
PATH_NOT_FOUND = "Path not found"
NOT_A_REGULAR_FILE = "Path is not a regular file"
NOT_A_FILE_OR_DIRECTORY = "Path is not a file or directory"
NOT_TEXT = "File is not UTF-8 text"
UNREADABLE = "File could not be read"
MULTILINE_QUERY = "Query must be a single line"

# Stays under DaveHarness's 64 KiB per-result lifecycle budget.
OUTPUT_BUDGET_BYTES = 48 * 1024
_ENVELOPE_SLACK = 512

LIST_MAX_DEPTH = 3
LIST_DEFAULT_DEPTH = 1
LIST_MAX_ENTRIES = 500
LIST_DEFAULT_ENTRIES = 100
LIST_SCAN_LIMIT = 10_000

SEARCH_MAX_MATCHES = 200
SEARCH_DEFAULT_MATCHES = 50
SEARCH_MAX_FILES = 2_000
SEARCH_SCAN_LIMIT = 20_000  # walked entries, directories included
SEARCH_MAX_DEPTH = 32
SEARCH_MAX_FILE_BYTES = 1_048_576
SEARCH_MAX_QUERY_CHARS = 200
SEARCH_LINE_CHARS = 300
_SEARCH_CONTEXT_CHARS = 100

READ_MAX_LINES = 400
READ_DEFAULT_LINES = 200
READ_MAX_START_LINE = 10_000_000
READ_MAX_FILE_BYTES = 10 * 1_048_576
READ_MAX_LINE_CHARS = 2_000

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Linux, macOS and the BSDs can open each path component relative to its
# parent's descriptor without following symlinks.
DESCRIPTOR_WALK = bool(_O_NOFOLLOW and _O_DIRECTORY) and os.open in os.supports_dir_fd
# ELOOP (Linux, macOS), EMLINK (FreeBSD) and ENOTDIR mean a component is a symlink.
_SYMLINK_ERRNOS = frozenset(
    code for code in (getattr(errno, "ELOOP", None), getattr(errno, "EMLINK", None),
                      getattr(errno, "ENOTDIR", None)) if code is not None
)


class FileToolError(ValueError):
    """A refusal whose message is safe to return to the model."""


class FileTooLarge(FileToolError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"File is larger than the {limit // 1_048_576} MiB limit")


def _open_by_descriptor_walk(path: Path) -> int:
    parent = os.open("/", os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(path.name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def _open_and_verify(path: Path) -> int:
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or _still_linked(path):
        raise PathNotAllowed()
    descriptor = os.open(path, os.O_RDONLY | _O_CLOEXEC | _O_NONBLOCK)
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or _still_linked(path):
        os.close(descriptor)
        raise PathNotAllowed()
    return descriptor


def open_admitted_file(path: Path) -> int:
    """Open an admitted absolute path for reading without following any symlink.

    With DESCRIPTOR_WALK, every component is opened relative to its parent's
    descriptor with O_NOFOLLOW, starting at "/", so a file or directory
    replaced by a symlink after admission makes the open fail instead of
    redirecting it. Otherwise the file is opened and then checked to be the same
    inode, reached through a path that still holds no symlink. Either way the
    opened object must be a regular file. Returns an open descriptor.
    """
    if not path.is_absolute():
        raise PathNotAllowed()
    error: str | None = None
    try:
        descriptor = (_open_by_descriptor_walk if DESCRIPTOR_WALK else _open_and_verify)(path)
    except FileNotFoundError:
        error = FILE_NOT_FOUND
    except OSError as exc:
        error = PATH_NOT_ALLOWED if exc.errno in _SYMLINK_ERRNOS or isinstance(exc, PathNotAllowed) else UNREADABLE
    if error == PATH_NOT_ALLOWED:
        raise PathNotAllowed()
    if error is not None:
        raise FileToolError(error)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise FileToolError(NOT_A_REGULAR_FILE)
    return descriptor


def read_admitted_bytes(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes of an admitted regular file through ``open_admitted_file``."""
    descriptor = open_admitted_file(path)
    try:
        if os.fstat(descriptor).st_size > limit:
            raise FileTooLarge(limit)
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > limit:  # grew after the size check
        raise FileTooLarge(limit)
    return data


def decode_text(data: bytes) -> str | None:
    """Strict UTF-8 text, or None for binary data; nothing is replaced or guessed."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def split_lines(text: str) -> list[str]:
    """Lines split on LF (CRLF tolerated); a final newline does not start a new line."""
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def encode_result(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encoded_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _fitting(items: Sequence[Any], envelope: Mapping[str, Any]) -> int:
    """How many leading ``items`` fit in the output budget next to ``envelope``."""
    used = _encoded_size(envelope) + _ENVELOPE_SLACK
    for count, item in enumerate(items):
        used += _encoded_size(item) + 1
        if used > OUTPUT_BUDGET_BYTES:
            return count
    return len(items)


def _option(arguments: Mapping[str, Any], name: str, default: Any) -> Any:
    """An optional argument; an explicit null means the default, as the schemas allow."""
    value = arguments.get(name)
    return default if value is None else value


def _lstat(path: Path) -> os.stat_result:
    error = PATH_NOT_FOUND
    try:
        return os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError:
        error = UNREADABLE
    raise FileToolError(error)


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "symlink" if stat.S_ISLNK(mode) else "other"


def _page(items: Sequence[T], page_size: int, scope: str, cursor: str | None) -> Page[T]:
    try:
        return paginate(items, page_size=page_size, scope=scope, cursor=cursor)
    except ValueError as exc:
        message = str(exc)
    raise FileToolError(message)


_TYPE_NAMES = {"file": "file", "dir": "directory", "symlink": "symlink", "other": "other"}


def _list_entry(display: str, name: str, kind: str, size: int | None, modified: float | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"path": display, "name": name, "type": _TYPE_NAMES[kind]}
    if size is not None:
        entry["size"] = size
    if modified is not None:
        entry["modified"] = datetime.fromtimestamp(modified, timezone.utc).isoformat(timespec="seconds")
    return entry


def list_files(
    arguments: Mapping[str, Any], *, resolve: Callable[[str], Path], roots: Sequence[Path],
) -> dict[str, Any]:
    """file.list: a bounded, name-sorted, breadth-first listing with request-bound paging."""
    depth = _option(arguments, "depth", LIST_DEFAULT_DEPTH)
    page_size = _option(arguments, "max_entries", LIST_DEFAULT_ENTRIES)
    target = resolve(_option(arguments, "path", "."))
    root = deepest_root(target, roots)
    display = relative_display(target, root)
    info = _lstat(target)
    kind = _kind(info.st_mode)
    scope = cursor_scope("file.list", {"path": str(target), "depth": depth, "max_entries": page_size})
    depth_limited = limit_reached = False
    if kind == "dir" and depth > 0:
        walk = walk_tree(target, root, max_depth=depth, max_entries=LIST_SCAN_LIMIT)
        items = [
            _list_entry(entry.display, entry.path.name, entry.kind, entry.size, entry.modified)
            for entry in walk.entries
        ]
        depth_limited, limit_reached = walk.depth_limited, walk.truncated
    else:
        name = "." if display == "." else target.name
        items = [_list_entry(display, name, kind, info.st_size if kind == "file" else None, info.st_mtime)]
    page = _page(items, page_size, scope, arguments.get("cursor"))
    envelope = {
        "path": display, "depth": depth, "entries": [], "truncated": True,
        "next_cursor": encode_cursor(page.offset + len(page.items), scope),
        "depth_limited": depth_limited, "limit_reached": limit_reached,
    }
    kept = _fitting(page.items, envelope)
    next_cursor: str | None = page.next_cursor
    truncated = page.truncated or limit_reached
    if kept < len(page.items):
        truncated, next_cursor = True, encode_cursor(page.offset + kept, scope)
    return {**envelope, "entries": list(page.items[:kept]), "truncated": truncated, "next_cursor": next_cursor}


def _match_text(line: str, column: int) -> tuple[str, bool]:
    if len(line) <= SEARCH_LINE_CHARS:
        return line, False
    start = max(0, min(column - _SEARCH_CONTEXT_CHARS, len(line) - SEARCH_LINE_CHARS))
    return line[start:start + SEARCH_LINE_CHARS], True


def search_files(
    arguments: Mapping[str, Any], *, resolve: Callable[[str], Path], roots: Sequence[Path],
) -> dict[str, Any]:
    """file.search: literal, line-based search over bounded, symlink-safe text reads.

    Files come from ``walk_tree`` (so secrets, ignored folders and symlinks are
    never candidates) in its deterministic order. At most SEARCH_MAX_FILES are
    considered; each is opened with ``read_admitted_bytes``, skipped when larger
    than SEARCH_MAX_FILE_BYTES or not strict UTF-8, and matched line by line.
    """
    query = arguments["query"]
    if "\n" in query or "\r" in query:
        raise FileToolError(MULTILINE_QUERY)
    case_sensitive = _option(arguments, "case_sensitive", False)
    max_matches = _option(arguments, "max_matches", SEARCH_DEFAULT_MATCHES)
    target = resolve(_option(arguments, "path", "."))
    root = deepest_root(target, roots)
    display = relative_display(target, root)
    kind = _kind(_lstat(target).st_mode)
    truncated = False
    if kind == "file":
        candidates = [target]
    elif kind == "dir":
        walk = walk_tree(target, root, max_depth=SEARCH_MAX_DEPTH, max_entries=SEARCH_SCAN_LIMIT)
        candidates = [entry.path for entry in walk.entries if entry.kind == "file"]
        truncated = walk.truncated or walk.depth_limited
    else:
        raise FileToolError(NOT_A_FILE_OR_DIRECTORY)
    if len(candidates) > SEARCH_MAX_FILES:
        candidates, truncated = candidates[:SEARCH_MAX_FILES], True
    needle = query if case_sensitive else query.casefold()
    counts = {"files_scanned": 0, "skipped_large": 0, "skipped_binary": 0, "skipped_unreadable": 0}
    matches: list[dict[str, Any]] = []
    for path in candidates:
        try:
            text = decode_text(read_admitted_bytes(path, SEARCH_MAX_FILE_BYTES))
        except FileTooLarge:
            counts["skipped_large"] += 1
            continue
        except (FileToolError, PathNotAllowed):
            counts["skipped_unreadable"] += 1
            continue
        if text is None:
            counts["skipped_binary"] += 1
            continue
        counts["files_scanned"] += 1
        shown = relative_display(path, root)
        for number, line in enumerate(split_lines(text), 1):
            haystack = line if case_sensitive else line.casefold()
            index = haystack.find(needle)
            if index < 0:
                continue
            if len(matches) >= max_matches:
                truncated = True
                break
            # casefold can change a line's length; then the window starts at the line start.
            text_shown, cut = _match_text(line, index if len(haystack) == len(line) else 0)
            match: dict[str, Any] = {"path": shown, "line": number, "text": text_shown}
            if cut:
                match["text_truncated"] = True
            matches.append(match)
        if truncated and len(matches) >= max_matches:
            break
    envelope = {"path": display, "matches": [], "match_count": len(matches), "truncated": True, **counts}
    kept = _fitting(matches, envelope)
    return {**envelope, "matches": matches[:kept], "match_count": kept,
            "truncated": truncated or kept < len(matches)}


def read_lines(
    arguments: Mapping[str, Any], *, resolve: Callable[[str], Path], roots: Sequence[Path],
) -> dict[str, Any]:
    """file.read_lines: one page of 1-based lines from a strict UTF-8 text file."""
    start = _option(arguments, "start_line", 1)
    count = _option(arguments, "max_lines", READ_DEFAULT_LINES)
    target = resolve(arguments["path"])
    display = relative_display(target, deepest_root(target, roots))
    text = decode_text(read_admitted_bytes(target, READ_MAX_FILE_BYTES))
    if text is None:
        raise FileToolError(NOT_TEXT)
    lines = split_lines(text)
    window = lines[start - 1:start - 1 + count]
    shown = [line[:READ_MAX_LINE_CHARS] for line in window]
    envelope = {"path": display, "start_line": start, "lines": [], "line_count": 0,
                "total_lines": len(lines), "next_start_line": start, "truncated": True,
                "cut_lines": []}
    kept = _fitting(shown, envelope)
    shown = shown[:kept]
    cut = [start + offset for offset, line in enumerate(window[:kept]) if len(line) > READ_MAX_LINE_CHARS]
    end = start - 1 + kept
    result: dict[str, Any] = {
        "path": display, "start_line": start, "lines": shown, "line_count": kept,
        "total_lines": len(lines), "next_start_line": end + 1 if end < len(lines) else None,
        "truncated": kept < len(window) or bool(cut),
    }
    if cut:
        result["cut_lines"] = cut
    return result
