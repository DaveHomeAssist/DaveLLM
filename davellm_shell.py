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
_PROC = Path("/proc")
_EXITED_STATES = frozenset("ZX")


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


def _stat_fields(path: Path) -> list[str]:
    """Return /proc stat fields after the parenthesized command name."""
    text = path.read_text(errors="replace")
    return text[text.rindex(")") + 1:].split()


def _proc_shows_every_process() -> bool:
    """Trust /proc only when it is this PID namespace's and hides no process."""
    try:
        if os.readlink(_PROC / "self") != str(os.getpid()):
            return False
        mounts = (_PROC / "self" / "mounts").read_text().splitlines()
    except OSError:
        return False
    restricted: bool | None = None
    for line in mounts:
        fields = line.split()
        if len(fields) > 3 and fields[1] == "/proc" and fields[2] == "proc":
            restricted = any(
                option.startswith("hidepid=") and option not in {"hidepid=0", "hidepid=off"}
                for option in fields[3].split(",")
            )
    return restricted is False


def _only_zombies_remain(pgid: int) -> bool:
    """Return whether every thread still in the process group has exited.

    Signal 0 succeeds for zombies, which can never run again but keep their
    group alive until their parent reaps them. A killed orphan waits for PID 1,
    and some container init processes reap slowly. Linux /proc shows each
    thread's state; anything other than zombie or dead, including
    uninterruptible sleep, still counts as running. Without a complete /proc,
    as on macOS, answer False so signal 0 stays authoritative.
    """
    if not _proc_shows_every_process():
        return False
    states: set[str] = set()
    try:
        for name in os.listdir(_PROC):
            if not name.isdecimal():
                continue
            member = _PROC / name
            try:
                fields = _stat_fields(member / "stat")
                if fields[2] != str(pgid):
                    continue
                states.add(fields[0])
                tids = os.listdir(member / "task")
            except (FileNotFoundError, ProcessLookupError):
                continue  # reaped during the scan
            for tid in tids:
                try:
                    states.add(_stat_fields(member / "task" / tid / "stat")[0])
                except (FileNotFoundError, ProcessLookupError):
                    continue  # thread exited during the scan
    except (OSError, ValueError, IndexError):
        return False
    return bool(states) and states <= _EXITED_STATES


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
            if await asyncio.to_thread(_only_zombies_remain, process.pid):
                break
            await asyncio.sleep(0.02)
        else:
            return False
        self._active.pop(key, None)
        return process.returncode is not None
