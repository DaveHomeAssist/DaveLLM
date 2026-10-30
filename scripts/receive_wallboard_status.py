#!/usr/bin/env python3
"""Atomic receiver executed over SSH by the wallboard publisher, never a service."""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

LIMIT = 1024 * 1024


def receive(destination: str, digest: str, data: bytes) -> str:
    if not data or len(data) > LIMIT or hashlib.sha256(data).hexdigest() != digest:
        return "invalid"
    try:
        document = json.loads(data)
        if not isinstance(document, dict) or set(document) != {"version", "published_at", "current", "history"} or document["version"] != 1:
            return "invalid"
    except (ValueError, UnicodeError, RecursionError):
        return "invalid"
    path = Path(destination)
    try:
        valid_path = path.is_absolute() and path.resolve() == path and bool(path.name)
    except (OSError, RuntimeError):
        valid_path = False
    if not valid_path:
        return "refused"
    directory = None
    temporary = ".wallboard-" + uuid.uuid4().hex
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        parent = os.fstat(directory)
        if parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
            return "refused"
        try:
            old = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(old.st_mode) or old.st_uid != os.geteuid():
                return "refused"
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            if stream.read(LIMIT + 1) != data or os.fstat(stream.fileno()).st_uid != os.geteuid():
                return "invalid"
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        return "success"
    except OSError:
        return "refused"
    finally:
        if directory is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
            os.close(directory)


if __name__ == "__main__":
    result = receive(sys.argv[1], sys.argv[2], sys.stdin.buffer.read(LIMIT + 1))
    print(result)
    sys.exit(0 if result == "success" else 1)
