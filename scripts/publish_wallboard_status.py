#!/usr/bin/env python3
"""Publish only a validated wallboard projection through a bounded SSH command."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import time

from export_wallboard_status import project
from record_monitor_status import MAX_FILE_BYTES


def projection(source: Path) -> bytes:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not regular")
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("oversized source")
    data = json.dumps(project(json.loads(raw)), allow_nan=False).encode()
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("oversized projection")
    return data


def publish(source: Path, host: str, destination: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", host) or not destination.startswith("/") or "\x00" in destination:
        return "refused"
    try:
        data = projection(source)
        receiver = Path(__file__).with_name("receive_wallboard_status.py").read_text()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return "invalid"
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1", host,
               shlex.join(["python3", "-c", receiver, destination, hashlib.sha256(data).hexdigest()])]
    for attempt in range(2):
        try:
            result = subprocess.run(command, input=data, capture_output=True, timeout=15, check=False)
            response = result.stdout.strip()
            if result.returncode == 0 and response == b"success":
                return "success"
            if result.returncode == 1 and response in {b"invalid", b"refused"}:
                return response.decode()
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt == 0:
            time.sleep(0.5)
    return "unavailable"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--host", required=True, help="Existing strict-host-key SSH alias")
    parser.add_argument("--destination", required=True, help="Absolute path in an existing private directory owned by the remote user")
    args = parser.parse_args()
    result = publish(args.source, args.host, args.destination)
    print(result)
    raise SystemExit(0 if result == "success" else 1)


if __name__ == "__main__":
    main()
