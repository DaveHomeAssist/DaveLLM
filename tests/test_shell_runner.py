"""Cancellable shell process proof uses only a disposable pytest root."""

import asyncio
import sys
from pathlib import Path

import pytest

from daveharness import CancellationToken, ExecutionContext
import davellm_shell
from davellm_shell import ShellProcessRunner, prepare_shell_command


def test_shell_command_needs_explicit_root_and_contains_file_operands(tmp_path):
    with pytest.raises(PermissionError, match="DAVE_TOOL_ROOTS"):
        prepare_shell_command("pwd", [])
    with pytest.raises(PermissionError, match="outside"):
        prepare_shell_command("head /etc/passwd", [tmp_path])
    with pytest.raises(ValueError, match="whitelisted"):
        prepare_shell_command("sh -c true", [tmp_path])
    target = tmp_path / "sample.txt"
    target.write_text("safe")
    argv, cwd = prepare_shell_command("head -n 1 sample.txt", [tmp_path])
    assert argv == ["head", "-n", "1", str(target)]
    assert cwd == tmp_path


@pytest.mark.asyncio
async def test_process_group_stop_prevents_post_cancel_file_effect(tmp_path):
    runner = ShellProcessRunner([tmp_path])
    target = tmp_path / "late.txt"
    context = ExecutionContext(
        run_id="run_shell", call_id="call_shell", run_deadline=None,
        model_deadline=None, tool_deadline=None, output_budget_bytes=2000,
        cancellation=CancellationToken(),
    )
    child = "import pathlib,time; time.sleep(.5); pathlib.Path(%r).write_text('late')" % str(target)
    script = "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', %r]); time.sleep(1)" % child
    task = asyncio.create_task(runner._run([sys.executable, "-c", script], Path(tmp_path), context))
    await asyncio.sleep(0.05)
    assert await runner.stop(context) is True
    await task
    await asyncio.sleep(0.55)
    assert not target.exists()


@pytest.mark.asyncio
async def test_stop_during_spawn_waits_for_process_and_prevents_late_effect(tmp_path, monkeypatch):
    runner = ShellProcessRunner([tmp_path])
    context = ExecutionContext(
        run_id="spawn_run", call_id="spawn_call", run_deadline=None,
        model_deadline=None, tool_deadline=None, output_budget_bytes=2000,
        cancellation=CancellationToken(),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec

    async def held_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(davellm_shell.asyncio, "create_subprocess_exec", held_spawn)
    target = tmp_path / "late-spawn.txt"
    script = "import pathlib,time; time.sleep(.2); pathlib.Path(%r).write_text('late')" % str(target)
    run = asyncio.create_task(runner._run([sys.executable, "-c", script], tmp_path, context))
    await started.wait()
    stopped = asyncio.create_task(runner.stop(context))
    await asyncio.sleep(0)
    release.set()
    assert await stopped is True
    assert (await run)["error"] == "Command cancelled"
    await asyncio.sleep(0.25)
    assert not target.exists()


@pytest.mark.asyncio
async def test_cancelled_invocation_owns_process_spawn(tmp_path, monkeypatch):
    runner = ShellProcessRunner([tmp_path])
    context = ExecutionContext(
        run_id="cancel_run", call_id="cancel_call", run_deadline=None,
        model_deadline=None, tool_deadline=None, output_budget_bytes=2000,
        cancellation=CancellationToken(),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec

    async def held_spawn(*args, **kwargs):
        started.set()
        await release.wait()
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(davellm_shell.asyncio, "create_subprocess_exec", held_spawn)
    target = tmp_path / "late-cancel.txt"
    script = "import pathlib,time; time.sleep(.2); pathlib.Path(%r).write_text('late')" % str(target)
    run = asyncio.create_task(runner._run([sys.executable, "-c", script], tmp_path, context))
    await started.wait()
    run.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    await asyncio.sleep(0.25)
    assert not target.exists()
