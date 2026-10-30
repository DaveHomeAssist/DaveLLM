import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_wallboard_status as publisher
import receive_wallboard_status as receiver
from test_wallboard_projection import fixture


@pytest.fixture
def files(tmp_path):
    tmp_path.chmod(0o700)
    source = tmp_path / "source.json"
    source.write_text(json.dumps(fixture()))
    destination = tmp_path / "status.json"
    destination.write_text("last good")
    return source, destination


def test_real_receiver_subprocess_and_sanitized_transport(files, monkeypatch):
    source, destination = files
    real_run = subprocess.run
    calls = []
    def transport(command, **kwargs):
        calls.append(command)
        assert "StrictHostKeyChecking=yes" in command
        assert "BatchMode=yes" in command
        assert b"private-value" not in kwargs["input"]
        assert b"secret raw error" not in kwargs["input"]
        remote = shlex.split(command[-1])
        return real_run([sys.executable, *remote[1:]], **kwargs)
    monkeypatch.setattr(publisher.subprocess, "run", transport)
    assert publisher.publish(source, "fixture-host", str(destination)) == "success"
    assert json.loads(destination.read_bytes())["current"]["overall"] == "healthy"
    assert len(calls) == 1
    assert destination.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("problem", ["digest", "malformed", "oversized", "symlink", "writable-parent", "replace-failure", "flush-failure"])
def test_failed_activation_preserves_last_good(files, monkeypatch, problem):
    source, destination = files
    data = publisher.projection(source)
    if problem == "malformed":
        data = b"{broken"
    if problem == "oversized":
        data = b"x" * (receiver.LIMIT + 1)
    digest = hashlib.sha256(data).hexdigest()
    if problem == "digest":
        digest = "0" * 64
    if problem == "symlink":
        link = destination.with_name("link")
        link.symlink_to(destination)
        target = link
    else:
        target = destination
    if problem == "writable-parent":
        destination.parent.chmod(0o777)
    def fail(*args, **kwargs):
        raise OSError("private remote error")
    if problem == "replace-failure":
        monkeypatch.setattr(receiver.os, "replace", fail)
    if problem == "flush-failure":
        monkeypatch.setattr(receiver.os, "fsync", fail)
    assert receiver.receive(str(target), digest, data) in {"invalid", "refused"}
    assert destination.read_text() == "last good"
    assert not list(destination.parent.glob(".wallboard-*"))


def test_bounded_timeout_and_retry_keeps_identical_payload(files, monkeypatch):
    source, destination = files
    payloads = []
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 15
        payloads.append(kwargs["input"])
        raise subprocess.TimeoutExpired(command, 15, stderr=b"sensitive error")
    monkeypatch.setattr(publisher.subprocess, "run", timeout)
    sleeps = []
    monkeypatch.setattr(publisher.time, "sleep", sleeps.append)
    assert publisher.publish(source, "fixture-host", str(destination)) == "unavailable"
    assert len(payloads) == 2 and payloads[0] == payloads[1]
    assert sleeps == [0.5]
    assert destination.read_text() == "last good"


def test_lost_ack_retry_is_idempotent(files, monkeypatch):
    source, destination = files
    calls = []
    def transport(command, **kwargs):
        payload = kwargs["input"]
        assert receiver.receive(str(destination), hashlib.sha256(payload).hexdigest(), payload) == "success"
        calls.append(payload)
        return subprocess.CompletedProcess(command, 255 if len(calls) == 1 else 0, b"" if len(calls) == 1 else b"success\n", b"")
    monkeypatch.setattr(publisher.subprocess, "run", transport)
    monkeypatch.setattr(publisher.time, "sleep", lambda _: None)
    assert publisher.publish(source, "fixture-host", str(destination)) == "success"
    assert len(calls) == 2 and calls[0] == calls[1] == destination.read_bytes()


def test_invalid_source_never_opens_transport(files, monkeypatch):
    source, destination = files
    source.write_text('{"version": 2}')
    def forbidden(*args, **kwargs):
        pytest.fail("invalid source attempted transport")
    monkeypatch.setattr(publisher.subprocess, "run", forbidden)
    assert publisher.publish(source, "fixture-host", str(destination)) == "invalid"
    assert publisher.publish(source, "-oProxyCommand=bad", str(destination)) == "refused"
    assert destination.read_text() == "last good"


def test_malformed_endpoint_returns_only_invalid(files, monkeypatch):
    source, destination = files
    document = fixture()
    document["current"]["targets"][0]["endpoint"] = {"secret": "private"}
    source.write_text(json.dumps(document))
    monkeypatch.setattr(publisher.subprocess, "run", lambda *a, **k: pytest.fail("unexpected SSH"))
    assert publisher.publish(source, "fixture-host", str(destination)) == "invalid"
