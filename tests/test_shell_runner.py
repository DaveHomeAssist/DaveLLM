"""Cancellable shell process proof uses only a disposable pytest root."""

import asyncio
import ctypes
import os
import signal
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


def fake_proc(root, members, mount_options):
    """Write /proc stat files for {pid: (pgid, thread states, leader first)}."""
    if mount_options is not None:
        own = root / str(os.getpid())
        own.mkdir()
        (own / "mounts").write_text(f"proc /proc proc {mount_options} 0 0\n")
        (root / "self").symlink_to(own.name)
    for pid, (pgid, states) in members.items():
        for offset, state in enumerate(states):
            task = root / str(pid) / "task" / str(pid + offset)
            task.mkdir(parents=True)
            (task / "stat").write_text(f"{pid + offset} (sh (x)) {state} 1 {pgid} {pgid} 0 -1\n")
        (root / str(pid) / "stat").write_text(f"{pid} (sh (x)) {states[0]} 1 {pgid} {pgid} 0 -1\n")
    return root


@pytest.mark.parametrize(("members", "mount_options", "expected"), [
    ({9101: (9100, "Z"), 9200: (9200, "R")}, "rw,nosuid", True),
    ({9101: (9100, "Z"), 9102: (9100, "S")}, "rw,nosuid", False),
    ({9101: (9100, "ZD")}, "rw,nosuid", False),
    ({9101: (9100, "Z")}, "rw,hidepid=invisible", False),
    ({9101: (9100, "Z")}, None, False),
    ({9200: (9200, "R")}, "rw,nosuid", False),
], ids=["zombie-only", "live-member", "live-thread", "hidepid", "no-proc", "no-member"])
def test_only_zombie_group_counts_as_exited(tmp_path, monkeypatch, members, mount_options, expected):
    monkeypatch.setattr(davellm_shell, "_PROC", fake_proc(tmp_path, members, mount_options))
    assert davellm_shell._only_zombies_remain(9100) is expected


@pytest.mark.skipif(sys.platform != "linux", reason="child subreaper and /proc are Linux-only")
@pytest.mark.asyncio
async def test_stop_acknowledges_group_held_only_by_unreaped_zombie(tmp_path):
    # Stand in for a container PID 1 that reaps slowly: as child subreaper, this
    # process adopts the killed orphan and leaves it a zombie until reaped below.
    libc = ctypes.CDLL(None, use_errno=True)
    subreaper = 36  # PR_SET_CHILD_SUBREAPER
    if libc.prctl(subreaper, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)):
        pytest.skip(f"child subreaper unavailable: errno {ctypes.get_errno()}")
    runner = ShellProcessRunner([tmp_path])
    target = tmp_path / "late.txt"
    pid_file = tmp_path / "orphan.pid"
    context = ExecutionContext(
        run_id="run_zombie", call_id="call_zombie", run_deadline=None,
        model_deadline=None, tool_deadline=None, output_budget_bytes=2000,
        cancellation=CancellationToken(),
    )
    child = "import pathlib,time; time.sleep(.5); pathlib.Path(%r).write_text('late')" % str(target)
    script = (
        "import pathlib,subprocess,sys,time; p = subprocess.Popen([sys.executable, '-c', %r]); "
        "pathlib.Path(%r).write_text(str(p.pid)); time.sleep(1)"
    ) % (child, str(pid_file))
    orphan = None
    try:
        task = asyncio.create_task(runner._run([sys.executable, "-c", script], tmp_path, context))
        for _ in range(500):
            if pid_file.exists() and pid_file.read_text().isdecimal():
                break
            await asyncio.sleep(0.01)
        orphan = int(pid_file.read_text())
        pgid = os.getpgid(orphan)
        assert await runner.stop(context) is True
        await task
        os.killpg(pgid, 0)  # signal 0 alone still sees the group
        _, status = os.waitpid(orphan, 0)
        orphan = None
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)
        await asyncio.sleep(0.55)
        assert not target.exists()
    finally:
        libc.prctl(subreaper, ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
        if orphan is not None:
            try:
                os.kill(orphan, signal.SIGKILL)
                os.waitpid(orphan, 0)
            except (ProcessLookupError, ChildProcessError):
                pass


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
