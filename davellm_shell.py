"""DaveLLM-owned, root-contained shell command and process cancellation adapter."""

from __future__ import annotations

import asyncio
import inspect
import os
import shlex
import signal
from pathlib import Path
from typing import Any

from daveharness import CancellableToolRunner, ExecutionContext, ToolDefinition


SAFE_COMMANDS = frozenset({"echo", "date", "pwd", "ls", "wc", "head", "tail"})
SHELL_OUTPUT_LIMIT = 2_000


def prepare_shell_command(command: str, roots: list[Path]) -> tuple[list[str], Path]:
    """Parse one non-shell command, allowing file operands only inside a root."""
    if not command:
        raise ValueError("Missing command")
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise ValueError("Invalid command quoting") from exc
    if not words:
        raise ValueError("Missing command")
    name = words[0]
    if name not in SAFE_COMMANDS:
        raise ValueError(f"Command not whitelisted: {name}")
    if not roots:
        raise PermissionError("Shell commands require DAVE_TOOL_ROOTS")
    cwd = roots[0].resolve()
    if not cwd.is_dir():
        raise PermissionError("Shell root is unavailable")
    if name in {"echo", "date", "pwd"}:
        if name != "echo" and len(words) != 1:
            raise ValueError("Command does not accept arguments")
        return words, cwd

    result = [name]
    number_next = False
    for word in words[1:]:
        if number_next:
            if not word.isdecimal():
                raise ValueError("Invalid count argument")
            result.append(word)
            number_next = False
        elif name in {"head", "tail"} and word in {"-n", "-c"}:
            result.append(word)
            number_next = True
        elif name == "ls" and word in {"-a", "-l", "-h", "-la", "-al", "-lh"}:
            result.append(word)
        elif name == "wc" and word in {"-l", "-w", "-c"}:
            result.append(word)
        elif word.startswith("-"):
            raise ValueError("Unsupported command option")
        else:
            candidate = (Path(word) if Path(word).is_absolute() else cwd / word).expanduser().resolve()
            if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
                raise PermissionError("Access denied: path is outside DAVE_TOOL_ROOTS")
            result.append(str(candidate))
    if number_next:
        raise ValueError("Missing count argument")
    return result, cwd


class ShellProcessRunner(CancellableToolRunner):
    """Acknowledge stop only after the entire process group has exited."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = roots
        self._active: dict[tuple[str, str], asyncio.subprocess.Process] = {}
        self._spawning: dict[tuple[str, str], asyncio.Event] = {}
        self._stop_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def invoke(
        self, definition: ToolDefinition, arguments: dict[str, Any],
        context: ExecutionContext,
    ) -> Any:
        value = definition.handler(arguments, context)
        return await value if inspect.isawaitable(value) else value

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> dict[str, str]:
        try:
            argv, cwd = prepare_shell_command(str(arguments.get("command", "")), self.roots)
            return await self._run(argv, cwd, context)
        except (ValueError, PermissionError) as exc:
            return {"tool": "shell.exec", "status": "error", "result": "", "error": str(exc)}

    async def _run(
        self, argv: list[str], cwd: Path, context: ExecutionContext,
    ) -> dict[str, str]:
        key = (context.run_id, context.call_id)
        spawned = asyncio.Event()
        self._spawning[key] = spawned
        self._stop_locks[key] = asyncio.Lock()
        process: asyncio.subprocess.Process | None = None
        spawn_task: asyncio.Task[asyncio.subprocess.Process] | None = None

        async def read_bounded(stream: asyncio.StreamReader | None) -> bytes:
            if stream is None:
                return b""
            chunks = bytearray()
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                if len(chunks) < SHELL_OUTPUT_LIMIT:
                    chunks.extend(chunk[:SHELL_OUTPUT_LIMIT - len(chunks)])
            return bytes(chunks)

        try:
            if context.cancellation.is_requested:
                return {"tool": "shell.exec", "status": "error", "result": "", "error": "Command cancelled"}
            spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=True,
            ))
            process = await asyncio.shield(spawn_task)
            self._active[key] = process
            spawned.set()
            if context.cancellation.is_requested:
                if not await self.stop(context):
                    raise asyncio.TimeoutError("Shell process stop unacknowledged")
                return {"tool": "shell.exec", "status": "error", "result": "", "error": "Command cancelled"}
            remaining = context.remaining("tool")
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(
                    read_bounded(process.stdout), read_bounded(process.stderr), process.wait(),
                ), timeout=min(5.0, remaining) if remaining is not None else 5.0,
            )
            output = stdout if process.returncode == 0 else stderr
            return {
                "tool": "shell.exec", "status": "success" if process.returncode == 0 else "error",
                "result": output.decode("utf-8", errors="replace")[:SHELL_OUTPUT_LIMIT],
            }
        except asyncio.TimeoutError:
            if not await self.stop(context):
                raise
            return {"tool": "shell.exec", "status": "error", "result": "", "error": "Command timeout"}
        except asyncio.CancelledError:
            context.cancellation.request_cancel()
            if process is None and spawn_task is not None:
                try:
                    process = await asyncio.shield(spawn_task)
                    self._active[key] = process
                    spawned.set()
                except Exception:
                    pass
            await asyncio.shield(self.stop(context))
            raise
        finally:
            spawned.set()
            self._spawning.pop(key, None)
            if process is not None and process.returncode is not None:
                self._active.pop(key, None)
            if key not in self._active:
                self._stop_locks.pop(key, None)

    async def stop(self, context: ExecutionContext) -> bool:
        context.cancellation.request_cancel()
        key = (context.run_id, context.call_id)
        lock = self._stop_locks.get(key)
        if lock is None:
            return key not in self._active
        async with lock:
            return await self._stop_locked(key)

    async def _stop_locked(self, key: tuple[str, str]) -> bool:
        spawned = self._spawning.get(key)
        if spawned is not None and not spawned.is_set():
            try:
                await asyncio.wait_for(spawned.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                return False
        process = self._active.get(key)
        if process is None:
            return True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            return False
        for _ in range(20):
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            return False
        self._active.pop(key, None)
        return process.returncode is not None
