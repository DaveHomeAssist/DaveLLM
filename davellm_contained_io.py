"""Contained final I/O for the qualified file.read, file.write and file.append tools (DLA-03).

The handlers admit a path with ``resolve_tool_path``: it is fully resolved, so it
holds no symlink, and it lies inside a tool root (and the run's project folder,
when one is bound). This module keeps that admission true through the I/O
itself, the way ``file.edit`` already does:

- The file's folder is reached from "/" one component at a time, each opened
  relative to its parent's descriptor with O_NOFOLLOW. A folder, or the tool
  root itself, swapped for a symlink after admission makes the call fail
  instead of redirecting it outside the root.
- ``file.write`` and ``file.append`` create missing parent folders only below
  the admitted tool root, one at a time with ``mkdir`` relative to the parent's
  descriptor, and open each new folder with O_NOFOLLOW.
- The file is opened relative to its folder's descriptor with O_NOFOLLOW (and
  O_CREAT for writes, never O_TRUNC). It must be a regular file with exactly
  one link: a hard link can alias a file outside the root, so it is refused for
  reads too, as ``file.edit`` refuses it. O_NONBLOCK keeps a FIFO from blocking
  the open. Only after these checks is a write truncated and written.

Without descriptor-relative open, mkdir and O_NOFOLLOW (Windows), the admitted
path is opened directly and checked before and after the open: no symlink left
in the path, the same file, regular, one link. That narrows the window but
cannot close it, as with ``davellm_files.open_admitted_file``.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Sequence

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

DESCRIPTOR_IO = (
    bool(_O_NOFOLLOW and _O_DIRECTORY)
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
)
# ELOOP (Linux, macOS), EMLINK (FreeBSD) and ENOTDIR mean a component is a symlink, or not a folder.
_LINK_ERRNOS = frozenset(
    code for code in (getattr(errno, "ELOOP", None), getattr(errno, "EMLINK", None),
                      getattr(errno, "ENOTDIR", None)) if code is not None
)

OUTSIDE_ROOTS = "Access denied: path is outside DAVE_TOOL_ROOTS"
SYMLINK_IN_PATH = "Access denied: a component of the path is a symbolic link or not a folder"
MULTIPLE_LINKS = "Access denied: file has more than one hard link"
NOT_REGULAR = "Not a regular file"


class ContainedFileTooLarge(ValueError):
    """The opened file is over the read limit."""


def _not_found(path: Path) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), str(path))


def _root_for(path: Path, roots: Sequence[Path]) -> Path:
    """The deepest root holding ``path``; the path must be absolute and resolved."""
    holding = [root for root in roots if path == root or path.is_relative_to(root)]
    if not path.is_absolute() or not holding or path in holding:
        raise PermissionError(OUTSIDE_ROOTS)
    return max(holding, key=lambda root: len(root.parts))


def _open_folder(name: str, parent: int) -> int:
    return os.open(name, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=parent)


def _open_folder_below(root: Path, folder: Path, create: bool) -> int:
    """A descriptor for ``folder`` reached from "/" without following a symlink, creating below ``root``."""
    descriptor = os.open("/", os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
    try:
        for index, part in enumerate(folder.parts[1:], start=1):
            try:
                child = _open_folder(part, descriptor)
            except FileNotFoundError:
                if not create or index < len(root.parts):
                    raise
                try:
                    os.mkdir(part, 0o777, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = _open_folder(part, descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _check_opened(descriptor: int) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(NOT_REGULAR)
    if info.st_nlink > 1:
        raise PermissionError(MULTIPLE_LINKS)
    return info


def _open_with_descriptors(path: Path, root: Path, flags: int, create: bool) -> int:
    folder = _open_folder_below(root, path.parent, create)
    try:
        return os.open(path.name, flags | _O_NOFOLLOW | _O_CLOEXEC | _O_NONBLOCK, 0o666, dir_fd=folder)
    finally:
        os.close(folder)


def _open_and_verify(path: Path, flags: int, create: bool) -> int:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    if Path(os.path.realpath(path)) != path:
        raise PermissionError(SYMLINK_IN_PATH)
    descriptor = os.open(path, flags | _O_CLOEXEC | _O_NOFOLLOW, 0o666)
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino) \
                or Path(os.path.realpath(path)) != path:
            raise PermissionError(SYMLINK_IN_PATH)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def open_contained(path: Path, roots: Sequence[Path], flags: int) -> int:
    """Open an admitted path inside ``roots`` with ``flags``; see the module docstring for the checks."""
    root = _root_for(path, roots)
    create = bool(flags & os.O_CREAT)
    try:
        if DESCRIPTOR_IO:
            descriptor = _open_with_descriptors(path, root, flags, create)
        else:
            descriptor = _open_and_verify(path, flags, create)
    except FileNotFoundError:
        raise _not_found(path) from None
    except OSError as exc:
        if exc.errno in _LINK_ERRNOS:
            raise PermissionError(SYMLINK_IN_PATH) from None
        if exc.errno == errno.ENXIO:  # a FIFO with no reader, opened for writing without blocking
            raise ValueError(NOT_REGULAR) from None
        raise
    try:
        _check_opened(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def contained_read_text(path: Path, roots: Sequence[Path], limit: int) -> str:
    """Read an admitted UTF-8 text file of at most ``limit`` bytes, newlines as in text mode."""
    descriptor = open_contained(path, roots, os.O_RDONLY)
    try:
        if os.fstat(descriptor).st_size > limit:
            raise ContainedFileTooLarge(limit)
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
        raise ContainedFileTooLarge(limit)
    return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def contained_write_text(path: Path, roots: Sequence[Path], content: str, *, append: bool) -> None:
    """Write or append UTF-8 ``content`` to an admitted path, truncating only after the checks."""
    data = content.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else 0)
    descriptor = open_contained(path, roots, flags)
    try:
        if not append:
            os.ftruncate(descriptor, 0)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
    finally:
        os.close(descriptor)
