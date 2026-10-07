"""Disposable runner fixtures; no real Docker, media, network, push or account effect."""
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_toolpack import pack
from davellm_toolpack import ToolpackError, digest
from davellm_toolpack_catalog import SPECS
from davellm_toolpack_jobs import execute_job
from davellm_toolpack_local import execute_local
from davellm_toolpack_runner import JobRunner


@pytest.fixture
def runner(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("# 📡 Recorded Agent Output\n\nOlder fixture entry\n", encoding="utf-8")
    data = tmp_path / "data.csv"
    data.write_text("name,count\none,2\ntwo,3\n", encoding="utf-8")
    binary = tmp_path / "clip.wav"
    binary.write_bytes(b"RIFF" + b"\0" * 4 + b"WAVE" + b"fixture" * 20)
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    database = tmp_path / "vectors.db"
    sqlite3.connect(database).close()
    source = tmp_path / "docs"
    source.mkdir()
    (source / "doc.md").write_text("alpha beta gamma", encoding="utf-8")
    config = {"roots": [str(tmp_path)], "users": ["fixture"], "tools": [s.name for s in SPECS if s.backend == "job"],
              "sources": {alias: {"path": str(path), "users": ["fixture"]} for alias, path in (("notes", notes), ("docs", source), ("vectors", database), ("storage", tmp_path), ("model", binary))},
              "storage_source": "storage", "mount_path": str(tmp_path), "vector_database_source": "vectors", "whisper_model_source": "model",
              "containers": {"safe": "fixture-container"}, "executables": {name: sys.executable for name in ("docker", "tailscale", "ffprobe", "tesseract", "whisper", "vm_stat", "sysctl", "ps", "node", "mmdc")}}
    return JobRunner(config, "fixture"), tmp_path


def test_disk_mount_observations_unknown_vs_false(runner):
    job, root = runner
    for name in ("node.disk", "mac.disk.report"):
        result = job.execute(name, {"runner": "fixture"})
        assert result["free_bytes"] > 0 and result["mount_ready"] is False
    del job.config["mount_path"]
    assert job.execute("node.disk", {"runner": "fixture"})["mount_ready"] is None


def test_bounded_regex_and_index_are_retrievable(runner):
    job, root = runner
    result = job.execute("code.grep_regex", {"runner": "fixture", "source": "docs", "pattern": "a.*beta"})
    assert result["matches"][0]["line"] == 1
    index = job.execute("vector.index", {"runner": "fixture", "source": "docs"})
    assert index["indexed"] == 1
    with sqlite3.connect(root / "vectors.db") as connection:
        assert connection.execute("SELECT owner,content FROM tool_documents").fetchone() == ("fixture", "alpha beta gamma")


def test_vector_index_preserves_existing_application_tables(runner):
    job, root = runner
    with sqlite3.connect(root / "vectors.db") as connection:
        connection.execute("CREATE TABLE private_history(content)")
        connection.execute("INSERT INTO private_history VALUES ('preserve')")
    before = (root / "vectors.db").read_bytes()
    with pytest.raises(ToolpackError, match="Dedicated"):
        job.execute("vector.index", {"runner": "fixture", "source": "docs"})
    assert (root / "vectors.db").read_bytes() == before


def test_diagram_new_safe_svg_and_no_overwrite(runner, monkeypatch):
    job, root = runner
    def command(argv, **kwargs):
        Path(argv[-1]).write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><text>Fixture</text></svg>')
        return b""
    monkeypatch.setattr(job, "command", command)
    args = {"runner": "fixture", "source": "flowchart LR\n A-->B", "output": "fixture.svg"}
    assert job.execute("diagram.render", args)["outcome"] == "verified"
    with pytest.raises(ToolpackError, match="new SVG"):
        job.execute("diagram.render", args)
    with pytest.raises(ToolpackError, match="directives"):
        job.execute("diagram.render", {**args, "output": "other.svg", "source": 'flowchart LR\n click A "https://evil.test"'})


def test_duckdb_read_only_query_no_files_or_extensions(runner):
    pytest.importorskip("duckdb")
    job, _ = runner
    args = {"runner": "fixture", "path": "data.csv", "sql": "SELECT sum(CAST(count AS INTEGER)) AS total FROM records"}
    assert job.execute("data.query", args)["rows"] == [(5,)]
    for sql in ("SELECT 1; SELECT 2", "CREATE TABLE bad(x INT)", "COPY records TO '/tmp/must-not-write'", "SELECT * FROM read_csv('/etc/passwd')"):
        with pytest.raises(Exception):
            job.execute("data.query", {**args, "sql": sql})


def test_pdf_page_cap_and_secure_read(runner):
    pypdf = pytest.importorskip("pypdf")
    job, root = runner
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with (root / "sample.pdf").open("wb") as stream:
        writer.write(stream)
    result = job.execute("pdf.read", {"runner": "fixture", "path": "sample.pdf", "start_page": 1, "pages": 1})
    assert result["page_count"] == 2 and len(result["pages"]) == 1 and result["truncated"]


@pytest.mark.parametrize("name,args", [
    ("docker.ps", {"runner": "fixture"}), ("docker.logs", {"runner": "fixture", "container": "safe", "lines": 2}),
    ("tailscale.status", {"runner": "fixture"}), ("mac.pressure", {"runner": "fixture"}),
    ("video.probe", {"runner": "fixture", "path": "clip.wav"}),
    ("audio.transcribe", {"runner": "fixture", "path": "clip.wav", "language": "en"}),
    ("ocr.image", {"runner": "fixture", "path": "image.png"}),
])
def test_fixed_command_operations(runner, monkeypatch, name, args):
    job, root = runner
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if "--format" in argv:
            return b'{"Names":"fixture-container","Status":"Up"}\n{"Names":"other","Status":"Up"}'
        if "--json" in argv:
            return b'{"Peer":{"fixture":{"HostName":"Fixture","Online":true,"TailscaleIPs":["100.1.2.3"]}}}'
        if "-of" in argv:
            assert Path(argv[-1]).exists() and str(root) not in argv[-1]  # private decoder copy
            assert argv[argv.index("-protocol_whitelist") + 1] == "file"
            assert argv[argv.index("-f") + 1] == "wav"
            return b'{"format":{"duration":"1"}}'
        return b"Fixture output"
    monkeypatch.setattr(job, "command", command)
    result = job.execute(name, args)
    assert result and calls
    if name == "docker.ps":
        assert result["containers"] == [{"container": "safe", "status": "Up"}]
    if name == "tailscale.status":
        assert "100.1.2.3" not in json.dumps(result)


@pytest.mark.parametrize("name", ["test.run", "lint.run", "dep.audit"])
def test_recipes_are_fixed_operator_argv(runner, monkeypatch, name):
    job, root = runner
    job.config["recipes"] = {name: {"unit": {"source": "docs", "argv": [sys.executable, "-m", "pytest", "-q"]}}}
    captured = []
    monkeypatch.setattr(job, "command", lambda argv, **kw: captured.append((argv, kw)) or b"passed")
    result = job.execute(name, {"runner": "fixture", "recipe": "unit"})
    assert result["outcome"] == "verified" and captured[0][0] == [sys.executable, "-m", "pytest", "-q"]
    with pytest.raises(ToolpackError):
        job.execute(name, {"runner": "fixture", "recipe": "not-configured"})


def test_comms_append_preserves_prior_entry_and_checks_digest(runner):
    job, root = runner
    before = (root / "notes.md").read_text()
    args = {"runner": "fixture", "source": "notes", "text": "## Fixture new entry", "expected_digest": digest(before)}
    assert job.execute("comms.log.append", args)["outcome"] == "verified"
    assert "Older fixture entry" in (root / "notes.md").read_text()
    with pytest.raises(ToolpackError, match="changed"):
        job.execute("comms.log.append", args)


def test_nextsteps_updater_and_check_use_same_scoped_project(runner, monkeypatch):
    job, root = runner
    path = root / "next.json"
    before = '{"id":"fixture","status":"amber"}\n'
    path.write_text(before)
    job.config["sources"]["next"] = {"path": str(path), "users": ["fixture"]}
    job.config["nextsteps_updater"] = str(root / "approved-updater.mjs")
    calls = []
    monkeypatch.setattr(job, "command", lambda argv, **kw: calls.append(argv) or b"check passed")
    result = job.execute("nextsteps.update", {"runner": "fixture", "source": "next", "record_json": '{"id":"fixture","status":"green"}', "expected_digest": digest(before)})
    assert result["outcome"] == "verified" and len(calls) == 2 and calls[1][-1] == "--check"
    assert json.loads(path.read_text())["status"] == "green"


def test_git_commit_real_disposable_repo_and_claim_preservation(runner):
    job, root = runner
    if shutil.which("git") is None:
        pytest.skip("Git unavailable")
    repo = root / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)
    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    file = repo / "tracked.txt"
    file.write_text("before\n")
    git("add", "tracked.txt")
    git("commit", "-m", "base")
    file.write_text("after\n")
    job.config["sources"]["repo"] = {"path": str(repo), "users": ["fixture"]}
    preview = git("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "--", "tracked.txt").decode()
    args = {"runner": "fixture", "repository": "repo", "files": ["tracked.txt"], "message": "Approved fixture", "expected_digest": digest(preview)}
    claim = repo / ".agent-claim"
    claim.write_text("other owner")
    with pytest.raises(ToolpackError, match="claimed"):
        job.execute("git.commit", args)
    assert claim.read_text() == "other owner"
    claim.unlink()  # Test-owned fixture only.
    result = job.execute("git.commit", args)
    assert result["outcome"] == "verified" and git("log", "-1", "--format=%s").decode().strip() == "Approved fixture"
    assert not claim.exists()


def test_git_preview_and_commit_reject_untracked_without_staging(pack, runner):
    _, host, _, local_root = pack
    job, runner_root = runner
    repo = runner_root / "untracked-repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)
    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (repo / "tracked.txt").write_text("tracked\n")
    git("add", "tracked.txt")
    git("commit", "-m", "base")
    (repo / "new.txt").write_text("untracked\n")
    job.config["sources"]["repo"] = {"path": str(repo), "users": ["fixture"]}

    assert local_root == runner_root
    with pytest.raises(ToolpackError, match="tracked"):
        execute_local("git.write_preview", {"repository": str(repo), "files": ["new.txt"]}, host)
    with pytest.raises(ToolpackError, match="tracked"):
        job.execute("git.commit", {"runner": "fixture", "repository": "repo", "files": ["new.txt"],
                                   "message": "Must refuse", "expected_digest": digest("")})
    assert git("diff", "--cached", "--name-only") == b""
    assert git("status", "--short").decode().splitlines() == ["?? new.txt"]


def test_runner_select_filters_owner_and_returns_capabilities_only(pack):
    _, host, _, _ = pack
    host.config["runners"] = {
        "mine": {"users": ["default"], "capabilities": ["python", "pytest"],
                 "ssh_alias": "private-alias", "script": "/private/helper.py", "tools": ["test.run"]},
        "theirs": {"users": ["other"], "capabilities": ["python"],
                   "ssh_alias": "foreign-alias", "script": "/foreign/helper.py", "tools": ["test.run"]},
    }
    result = execute_local("runner.select", {"capability": "python"}, host)
    assert result["matches"] == [{"name": "mine", "capabilities": ["python", "pytest"]}]
    assert "alias" not in json.dumps(result) and "helper" not in json.dumps(result) and "other" not in json.dumps(result)


def test_command_abort_kills_descendants_but_not_unrelated_process(runner):
    if os.name != "posix" or not Path("/proc").exists():
        pytest.skip("POSIX process-group fixture requires /proc")
    job, root = runner
    descendant_pid = root / "descendant.pid"
    child = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(descendant_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent = f"""import os
import subprocess
import sys
import time
subprocess.Popen([sys.executable, "-c", {child!r}])
for _ in range(200):
    if os.path.exists({str(descendant_pid)!r}):
        break
    time.sleep(.01)
sys.stdout.buffer.write(b"x" * 70000)
sys.stdout.flush()
time.sleep(60)
"""
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        with pytest.raises(ToolpackError, match="output exceeded"):
            job.command([sys.executable, "-c", parent])
        pid = int(descendant_pid.read_text())
        for _ in range(200):
            state = Path(f"/proc/{pid}/stat")
            if not state.exists() or state.read_text().split()[2] == "Z":
                break
            import time
            time.sleep(.01)
        else:
            pytest.fail("descendant survived the aborted configured command")
        assert unrelated.poll() is None
    finally:
        if unrelated.poll() is None:
            os.killpg(unrelated.pid, signal.SIGKILL)
            unrelated.wait()


def test_push_exact_ref_and_env_only_auth_without_real_network(runner, monkeypatch):
    job, root = runner
    repo = root / "repo"
    repo.mkdir()
    job.config["sources"]["repo"] = {"path": str(repo), "users": ["fixture"]}
    job.config["git_destinations"] = {"repo": "https://github.com/fixture/repo.git"}
    head, calls = "a" * 40, []
    instance = SimpleNamespace(top=repo, env={"GIT_CONFIG_COUNT": "0"})
    def output(*args):
        calls.append((args, dict(instance.env)))
        if args[:2] == ("rev-parse", "HEAD"): data = head.encode()
        elif args[:2] == ("symbolic-ref", "--short"): data = b"main"
        elif "ls-remote" in args: data = (head + "\trefs/heads/main\n").encode()
        else: data = b""
        return SimpleNamespace(stdout=data)
    instance.output = output
    monkeypatch.setattr("davellm_toolpack_runner.open_repository", lambda *args: instance)
    monkeypatch.setenv("DAVE_GITHUB_TOKEN", "ghp_fixture_only_not_a_real_token")
    assert job.execute("git.push", {"runner": "fixture", "repository": "repo", "branch": "main", "expected_head": head})["outcome"] == "verified"
    pushed = next(call for call in calls if "push" in call[0])
    assert pushed[0][-1] == head + ":refs/heads/main" and "--force" not in pushed[0]
    assert not any("ghp_fixture" in str(call[0]) for call in calls)
    assert any("Authorization: Basic" in value for value in pushed[1].values())
    assert instance.env["GIT_CONFIG_COUNT"] == "0" and not (repo / ".agent-claim").exists()


def test_artnet_fixture_ten_replies_no_hardware(runner, monkeypatch):
    job, root = runner
    job.config["interfaces"] = {"lan": {"bind": "192.168.1.10", "broadcast": "192.168.1.255"}}
    packet = b"Art-Net\0\x00\x21" + b"\0" * 16 + b"Fixture\0" + b"\0" * 10
    class Sender:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def settimeout(self, *args): pass
        def bind(self, *args): pass
        def sendto(self, *args): pass
        def recvfrom(self, *args): return packet, ("192.168.1.20", 6454)
    monkeypatch.setattr("socket.socket", lambda *args: Sender())
    assert len(job.execute("artnet.discover", {"runner": "fixture", "interface": "lan"})["nodes"]) == 10


def test_admission_refuses_secrets_links_and_foreign_owner(runner):
    job, root = runner
    secret = root / ".env"
    secret.write_text("fixture-secret")
    with pytest.raises(Exception):
        job.resolve(str(secret))
    link = root / "outside"
    link.symlink_to("/etc/passwd")
    with pytest.raises(Exception):
        job.resolve(str(link))
    with pytest.raises(ToolpackError):
        JobRunner(job.config, "other")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [s.name for s in SPECS if s.backend == "job"])
async def test_every_job_transport_uses_configured_runner_and_no_model_command(pack, monkeypatch, name):
    _, host, _, _ = pack
    host.config["jobs_enabled"] = True
    host.config["runners"]["fixture"] = {"ssh_alias": "fixture", "python": "/usr/bin/python3", "script": "/srv/tools/scripts/tool_job_runner.py", "config": "/srv/tools/runner.json", "os": "posix", "host": "fixture-host", "users": ["default"], "tools": [name]}
    args = {"runner": "fixture"}
    class Reader:
        sent = False
        async def read(self, _size):
            if self.sent:
                return b""
            self.sent = True
            return json.dumps({"tool": name, "host": "fixture-host", "outcome": "verified"}).encode()
    class Writer:
        def write(self, payload):
            self.payload = json.loads(payload)
        async def drain(self):
            pass
        def close(self):
            pass
    process = SimpleNamespace(stdin=Writer(), stdout=Reader(), returncode=0, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    result = await execute_job(name, args, host)
    assert result["host"] == "fixture-host"
    assert spawn.call_args.args[0] == "/usr/bin/ssh" and "StrictHostKeyChecking=yes" in spawn.call_args.args
    assert process.stdin.payload["user"] == "default"


def test_catastrophic_regex_is_killed_by_actual_helper_watchdog(runner):
    if os.name != "posix":
        pytest.skip("POSIX helper only")
    job, root = runner
    (root / "docs" / "doc.md").write_text("a" * 1000 + "!")
    config = root / "runner.json"
    config.write_text(json.dumps(job.config))
    config.chmod(0o600)
    script = Path(__file__).resolve().parents[1] / "scripts" / "tool_job_runner.py"
    process = subprocess.Popen([sys.executable, str(script), "--config", str(config), "--tool", "code.grep_regex"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        process.communicate(json.dumps({"user": "fixture", "arguments": {"runner": "fixture", "source": "docs", "pattern": "(a+)+$"}}).encode(), timeout=35)
        assert process.returncode in (-9, -24), "helper must terminate the pathological job"
    finally:
        if process.poll() is None:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@pytest.mark.parametrize("name,path", [("video.probe", "playlist.m3u"), ("audio.transcribe", "playlist.m3u"), ("ocr.image", "playlist.m3u")])
def test_media_refuses_fetch_capable_inputs_before_decoder(runner, monkeypatch, name, path):
    job, root = runner
    (root / path).write_text("https://private-target.test/file")
    calls = []
    monkeypatch.setattr(job, "command", lambda *args, **kwargs: calls.append(args) or b"{}")
    args = {"runner": "fixture", "path": path, "language": "en"}
    with pytest.raises(ToolpackError):
        job.execute(name, args)
    assert not calls


@pytest.mark.parametrize("source", ['flowchart LR\n A["<img src=https://private.test>"]', 'flowchart LR\n A@{img: "https://private.test"}', 'flowchart LR\n style A fill:url(https://private.test)'])
def test_diagram_refuses_network_html_and_style_before_renderer(runner, monkeypatch, source):
    job, _ = runner
    calls = []
    monkeypatch.setattr(job, "command", lambda *args, **kwargs: calls.append(args) or b"")
    with pytest.raises(ToolpackError):
        job.execute("diagram.render", {"runner": "fixture", "source": source, "output": "new.svg"})
    assert not calls
