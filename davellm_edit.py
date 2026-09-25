"""file.edit: the one extended tool that writes, and only after exact-call approval.

An edit replaces ``old_text`` with ``new_text`` in an existing UTF-8 text file
inside a tool root. It is refused unless ``old_text`` occurs exactly
``expected_count`` times, so an approval always covers exactly the change it
showed. The path goes through the same admission as every extended tool
(``resolve_extended_tool_path``: anchoring, containment, secret denylist).

The write is atomic and never follows a symlink:

1. The file's directory is opened one component at a time from "/" with
   O_NOFOLLOW, and the file is opened relative to that descriptor, so a file
   or folder swapped for a symlink after admission makes the edit fail.
2. The new content goes to a fresh temporary file in the same directory
   (O_CREAT | O_EXCL | O_NOFOLLOW), with the original permission bits.
3. Just before the rename, the file is opened and read again. If it is no
   longer the same file with the same bytes, the temporary file is removed
   and nothing is written.
4. The temporary file is renamed over the original within the directory
   descriptor, so readers see the old or the new file, never a mix.

Line endings are kept: in a file whose every line ends with CRLF, the LF line
breaks in ``old_text`` and ``new_text`` are matched and written as CRLF. A
byte order mark stays in place. Files with several hard links are refused,
because replacing one link would silently leave the others unchanged.
Refusals are fixed messages; OS error text never reaches the model.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from davellm_files import (
    DESCRIPTOR_WALK, FILE_NOT_FOUND, NOT_A_REGULAR_FILE, NOT_TEXT, READ_MAX_FILE_BYTES, FileTooLarge,
    FileToolError, PathNotAllowed, _O_CLOEXEC, _O_NOFOLLOW, _O_NONBLOCK, _SYMLINK_ERRNOS, _option, decode_text,
    display_path, open_directory_descriptor, read_descriptor,
)


EDIT_MAX_TEXT_CHARS = 20_000
EDIT_MAX_REPLACEMENTS = 100
EDIT_DEFAULT_REPLACEMENTS = 1
TEMP_PREFIX = ".davellm-edit-"

TEXT_NOT_FOUND = "old_text was not found in the file; nothing was written"
COUNT_MISMATCH = "old_text was found {found} times, not the expected {expected}; nothing was written"
NO_CHANGE = "old_text and new_text are the same"
FILE_CHANGED = "The file changed while the edit was being prepared; nothing was written"
MULTIPLE_LINKS = "File has more than one hard link"
EDIT_UNSUPPORTED = "Editing files is not supported on this platform"
EDIT_FAILED = "The edit could not be written"

# Atomic replacement needs descriptor-relative open, rename, and unlink.
REPLACE_SUPPORTED = DESCRIPTOR_WALK and all(
    function in os.supports_dir_fd for function in (os.rename, os.unlink)
)


def line_endings(text: str) -> str:
    """``crlf`` when every line break is CRLF, otherwise ``lf``."""
    breaks = text.count("\n")
    return "crlf" if breaks and text.count("\r\n") == breaks else "lf"


def in_file_style(value: str, endings: str) -> str:
    """``value`` with its line breaks written the way the file writes them."""
    return value.replace("\r\n", "\n").replace("\n", "\r\n") if endings == "crlf" else value


def _open_parent(path: Path) -> int:
    try:
        return open_directory_descriptor(path.parent)
    except FileNotFoundError:
        raise FileToolError(FILE_NOT_FOUND) from None
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise PathNotAllowed() from None
        raise FileToolError(EDIT_FAILED) from None


def _read_file(directory: int, name: str) -> tuple[os.stat_result, bytes]:
    """Open ``name`` in ``directory`` without following a symlink and read all of it."""
    try:
        descriptor = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        raise FileToolError(FILE_NOT_FOUND) from None
    except OSError as exc:
        if exc.errno in _SYMLINK_ERRNOS:
            raise PathNotAllowed() from None
        raise FileToolError(EDIT_FAILED) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FileToolError(NOT_A_REGULAR_FILE)
        if info.st_nlink > 1:
            raise FileToolError(MULTIPLE_LINKS)
        return info, read_descriptor(descriptor, READ_MAX_FILE_BYTES)
    except OSError:
        raise FileToolError(EDIT_FAILED) from None
    finally:
        os.close(descriptor)


def _write_temp(directory: int, data: bytes, mode: int) -> str:
    """A new file in ``directory`` holding ``data`` with permission bits ``mode``; returns its name."""
    name = f"{TEMP_PREFIX}{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=directory,
        )
    except OSError:
        raise FileToolError(EDIT_FAILED) from None
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError:
        os.close(descriptor)
        _discard(directory, name)
        raise FileToolError(EDIT_FAILED) from None
    os.close(descriptor)
    return name


def _discard(directory: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except OSError:
        pass


def _unchanged(directory: int, name: str, before: os.stat_result, data: bytes) -> None:
    """Refuse unless ``name`` is still the same single-link file holding exactly ``data``."""
    try:
        now, current = _read_file(directory, name)
    except (FileToolError, PathNotAllowed):
        raise FileToolError(FILE_CHANGED) from None
    if (now.st_dev, now.st_ino) != (before.st_dev, before.st_ino) or current != data:
        raise FileToolError(FILE_CHANGED)


def edit_file(arguments: Mapping[str, Any], *, resolve: Callable[[str], Path],
              roots: Sequence[Path]) -> dict[str, Any]:
    """file.edit: replace ``old_text`` with ``new_text``, exactly ``expected_count`` times."""
    if not REPLACE_SUPPORTED:
        raise FileToolError(EDIT_UNSUPPORTED)
    path = resolve(str(arguments["path"]))
    old, new = str(arguments["old_text"]), str(arguments["new_text"])
    expected = _option(arguments, "expected_count", EDIT_DEFAULT_REPLACEMENTS)
    if old == new:
        raise FileToolError(NO_CHANGE)
    directory = _open_parent(path)
    try:
        before, data = _read_file(directory, path.name)
        text = decode_text(data)
        if text is None:
            raise FileToolError(NOT_TEXT)
        endings = line_endings(text)
        needle, replacement = in_file_style(old, endings), in_file_style(new, endings)
        found = text.count(needle)
        if found == 0:
            raise FileToolError(TEXT_NOT_FOUND)
        if found != expected:
            raise FileToolError(COUNT_MISMATCH.format(found=found, expected=expected))
        first_line = text.count("\n", 0, text.index(needle)) + 1
        updated = text.replace(needle, replacement).encode("utf-8")
        if len(updated) > READ_MAX_FILE_BYTES:
            raise FileTooLarge(READ_MAX_FILE_BYTES)
        temporary: str | None = _write_temp(directory, updated, stat.S_IMODE(before.st_mode))
        try:
            _unchanged(directory, path.name, before, data)
            try:
                os.rename(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            except OSError:
                raise FileToolError(EDIT_FAILED) from None
            temporary = None
        finally:
            if temporary is not None:
                _discard(directory, temporary)
        try:
            os.fsync(directory)
        except OSError:
            pass  # the rename is done; some file systems cannot sync a directory
    finally:
        os.close(directory)
    return {
        "path": display_path(path, roots),
        "replacements": found,
        "first_changed_line": first_line,
        "line_endings": endings,
        "bytes_before": len(data),
        "bytes_after": len(updated),
    }
