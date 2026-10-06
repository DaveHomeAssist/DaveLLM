#!/usr/bin/env python3
"""One SSH job, fixed config/tool argv, bounded JSON stdin and output; no service."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from davellm_toolpack import admitted_arguments, bounded_result
from davellm_toolpack_runner import JobRunner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--tool", required=True)
    args = parser.parse_args()
    # POSIX runners kill the entire isolated helper group, including grandchildren.
    # Windows support requires an operator job-object wrapper; fail closed here.
    if os.name != "posix":
        print(json.dumps({"tool": args.tool, "host": socket.gethostname(), "outcome": "failed", "reason": "POSIX watchdog required"}))
        return 1
    try:
        os.setsid()
    except PermissionError:
        if os.getpgrp() != os.getpid():
            return 1  # Never kill a process group that this helper does not own.
    def expired(_signal, _frame):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGXCPU, expired)
    signal.alarm(30)
    runner = None
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (18, 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
        payload = sys.stdin.buffer.read(65537)
        if len(payload) > 65536:
            raise ValueError
        envelope = json.loads(payload)
        admitted_arguments(args.tool, envelope["arguments"])
        config_path = Path(args.config)
        if config_path.is_symlink() or not config_path.is_absolute() or config_path.stat().st_size > 65536:
            raise ValueError
        from davellm_files import read_admitted_bytes
        if config_path.stat().st_uid != os.geteuid() or config_path.stat().st_mode & 0o022:
            raise ValueError
        config = json.loads(read_admitted_bytes(config_path, 65536))
        runner = JobRunner(config, envelope["user"])
        result = runner.execute(args.tool, envelope["arguments"])
        result.update(tool=args.tool, host=socket.gethostname())
        print(bounded_result(result))
        return 0
    except Exception:
        print(json.dumps({"tool": args.tool, "host": socket.gethostname(), "outcome": "unknown" if runner and runner.effect_started else "failed", "reason": "Configured job refused or failed; inspect any dispatched effect"}))
        return 1
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    raise SystemExit(main())
