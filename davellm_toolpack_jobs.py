"""Bounded SSH command runner transport, never an agent or caller-supplied command."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
from pathlib import PurePosixPath, PureWindowsPath

from davellm_toolpack import ToolpackError, digest


async def execute_job(name, args, host):
    if host.config.get("jobs_enabled") is not True:
        raise ToolpackError("Tool jobs are disabled; enable DAVE_ENABLE_TOOL_JOBS separately")
    binding = host.binding()
    runner = host.configured("runners", args["runner"])
    if binding.user_id not in runner.get("users", []) or name not in runner.get("tools", []):
        raise ToolpackError("Runner is not approved for this owner and operation")
    alias = runner.get("ssh_alias", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", alias):
        raise ToolpackError("Configured SSH alias is invalid")
    paths = [runner.get("python", ""), runner.get("script", ""), runner.get("config", "")]
    if any(not re.fullmatch(r"[A-Za-z0-9_./\\: -]{1,4096}", path) or not path or path.startswith("-") for path in paths):
        raise ToolpackError("Configured runner command paths are invalid")
    words = [paths[0], paths[1], "--config", paths[2], "--tool", name]
    if runner.get("os") == "windows":
        if not all(PureWindowsPath(path).is_absolute() for path in paths):
            raise ToolpackError("Runner command paths must be absolute")
        command = subprocess.list2cmdline(words)
    elif runner.get("os") == "posix":
        if not all(PurePosixPath(path).is_absolute() for path in paths):
            raise ToolpackError("Runner command paths must be absolute")
        command = shlex.join(words)
    else:
        raise ToolpackError("Runner shell capability is not configured")
    envelope = {"user": binding.user_id, "arguments": args}
    payload = json.dumps(envelope, allow_nan=False).encode()
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C"}
    if os.getenv("SSH_AUTH_SOCK"):
        environment["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
        alias, command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=environment, start_new_session=True)

    async def communicate():
        process.stdin.write(payload)
        await process.stdin.drain()
        process.stdin.close()
        captured = bytearray()
        while chunk := await process.stdout.read(8192):
            captured.extend(chunk)
            if len(captured) > 65536:
                raise ToolpackError("Runner capture exceeded the budget")
        await process.wait()
        response = json.loads(captured)
        if not isinstance(response, dict) or response.get("tool") != name or response.get("host") != runner.get("host"):
            raise ToolpackError("Runner identity or response is invalid")
        if process.returncode and response.get("outcome") != "unknown" or response.get("outcome") == "failed":
            raise ToolpackError("Runner refused or failed the configured operation")
        return response
    try:
        return await asyncio.wait_for(communicate(), 40)
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        # The remote helper has its own absolute 30-second watchdog. Closing SSH
        # alone is not proof that an already-dispatched remote effect was cancelled.
