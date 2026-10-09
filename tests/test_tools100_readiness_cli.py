import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_tools100_readiness import check_readiness


def private_json(path: Path, value) -> None:
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def group(report, name):
    return next(item for item in report["groups"] if item["group"] == name)


def test_absent_files_are_missing_without_network_or_secrets(tmp_path):
    report = check_readiness(tmp_path)
    assert report["overall"] == "Missing"
    assert report["files"] == {
        "toolpack": {"status": "Missing", "reason": "not_found"},
        "tool_roots": {"status": "Missing", "reason": "not_found"},
    }
    assert report["enabled_tools"] == {"status": "Missing", "count": 0, "names": []}
    assert len(report["groups"]) == 21
    assert report["secrets"] == "not_read" and report["network"] == "not_used"


@pytest.mark.parametrize("contents", ["not-json", "[]", '{"enabled_tools":"route.suggest"}'])
def test_malformed_or_wrong_shape_toolpack_fails_closed(tmp_path, contents):
    (tmp_path / "toolpack.json").write_text(contents)
    (tmp_path / "toolpack.json").chmod(0o600)
    private_json(tmp_path / "tool-roots.json", [str(tmp_path)])
    report = check_readiness(tmp_path)
    assert report["files"]["toolpack"]["status"] == "Missing"
    assert report["enabled_tools"]["count"] == 0
    assert all(item["status"] == "Missing" for item in report["groups"])


def test_partial_config_reports_ready_missing_and_unknown_without_values(tmp_path):
    sentinel = "do-not-print-this-provider-id"
    private_json(tmp_path / "tool-roots.json", [str(tmp_path)])
    private_json(tmp_path / "toolpack.json", {
        "enabled_tools": ["calc.eval", "time.convert", "json.validate", "gh.pr.status", "route.suggest"],
        "repositories": {"davellm": f"owner/{sentinel}"},
    })
    report = check_readiness(tmp_path)
    assert group(report, "EX-20")["status"] == "Ready"
    assert group(report, "EX-10")["status"] == "Unknown"
    assert group(report, "EX-04")["status"] == "Unknown"
    assert group(report, "EX-09")["status"] == "Missing"
    rendered = json.dumps(report)
    assert sentinel not in rendered and str(tmp_path) not in rendered


def test_fully_shaped_files_report_key_and_runner_shape_but_not_live_acceptance(tmp_path):
    runner_path = tmp_path / "runner.json"
    private_json(runner_path, {
        "users": ["default"],
        "tools": ["test.run"],
        "capabilities": ["python"],
        "executables": {name: f"/opt/bin/{name}" for name in (
            "git", "docker", "tailscale", "vm_stat", "sysctl", "ps", "ffprobe", "tesseract", "whisper", "mmdc"
        )},
    })
    private_json(tmp_path / "tool-roots.json", [str(tmp_path)])
    private_json(tmp_path / "toolpack.json", {
        "enabled_tools": ["test.run", "gh.pr.status", "notify.push", "route.suggest"],
        "sources": {"repo": {"path": str(tmp_path), "users": ["default"]}},
        "runners": {"dominic": {
            "ssh_alias": "dominic", "os": "posix", "python": "/usr/bin/python3",
            "script": "/opt/davellm/tool_job_runner.py", "config": "/opt/davellm/runner.json",
            "host": "dominic", "users": ["default"], "tools": ["test.run"],
            "capabilities": ["python"],
        }},
        "repositories": {"davellm": "DaveHomeAssist/DaveLLM"},
        "notifications": {"ops": {"url": "https://example.invalid", "topic": "ops"}},
    })
    report = check_readiness(tmp_path, {"dominic": runner_path})
    assert report["files"]["toolpack"]["status"] == "Ready"
    assert report["files"]["tool_roots"]["status"] == "Ready"
    assert report["configuration_keys"]["runners"] == "Ready"
    assert report["runner_configs"]["dominic"]["fields"]["executables"]["git"] == "Ready"
    assert group(report, "EX-10")["status"] == "Unknown"
    assert group(report, "EX-14")["status"] == "Unknown"
    assert report["overall"] == "Missing"


def test_symlink_and_group_writable_files_are_refused(tmp_path):
    target = tmp_path / "target.json"
    private_json(target, {"enabled_tools": ["calc.eval"]})
    (tmp_path / "toolpack.json").symlink_to(target)
    roots = tmp_path / "tool-roots.json"
    private_json(roots, [str(tmp_path)])
    roots.chmod(0o620)
    report = check_readiness(tmp_path)
    assert report["files"]["toolpack"]["reason"] == "not_private_regular_file"
    assert report["files"]["tool_roots"]["reason"] == "not_private_regular_file"


def test_cli_prints_json_and_uses_nonready_exit(tmp_path):
    result = subprocess.run(
        [sys.executable, "scripts/check_tools100_readiness.py", "--data-dir", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["scope"] == "local_nonsecret_files_only"
    assert result.stderr == ""
