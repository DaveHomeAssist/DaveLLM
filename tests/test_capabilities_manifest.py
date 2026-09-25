"""The generated capabilities manifest stays current, pinned, and free of local values."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "generate_capabilities_manifest.py"
JSON_NAME = "DAVEHARNESS_CAPABILITIES.json"
MARKDOWN_NAME = "DAVEHARNESS_CAPABILITIES.md"
CATALOG = json.loads((REPO / "tests" / "fixtures" / "davellm" / "tool_catalog.json").read_text())
PINNED_FIELDS = (
    "description", "parameters", "permission", "approval_required", "timeout_seconds",
    "cancellation", "async_handler", "context_handler",
)


def generate(*arguments):
    environment = {name: value for name, value in os.environ.items() if not name.startswith("DAVE_")}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments], cwd=REPO, env=environment,
        capture_output=True, text=True, timeout=120,
    )


def test_committed_manifest_matches_the_code():
    result = generate("--check")

    assert result.returncode == 0, result.stderr


def test_check_names_each_stale_file(tmp_path):
    assert generate("--docs-dir", str(tmp_path)).returncode == 0
    assert generate("--check", "--docs-dir", str(tmp_path)).returncode == 0

    manifest = json.loads((tmp_path / JSON_NAME).read_text())
    manifest["tools"]["file.write"]["approval_required"] = False
    (tmp_path / JSON_NAME).write_text(json.dumps(manifest))
    result = generate("--check", "--docs-dir", str(tmp_path))

    assert result.returncode == 1
    assert f"{JSON_NAME} is out of date" in result.stderr
    assert MARKDOWN_NAME not in result.stderr

    (tmp_path / MARKDOWN_NAME).unlink()
    result = generate("--check", "--docs-dir", str(tmp_path))

    assert result.returncode == 1
    assert f"{JSON_NAME} is out of date" in result.stderr
    assert f"{MARKDOWN_NAME} is out of date" in result.stderr


def test_manifest_tools_match_the_pinned_catalog():
    tools = json.loads((REPO / "docs" / JSON_NAME).read_text())["tools"]
    pinned = {**CATALOG["HARNESS_REGISTRY"], **CATALOG["extended_tools"]}

    assert set(tools) == set(pinned)
    for name, tool in tools.items():
        assert {field: tool[field] for field in PINNED_FIELDS} == {field: pinned[name][field] for field in PINNED_FIELDS}, name
    assert {name for name, tool in tools.items() if tool["requires_flags"][-1] == "DAVE_ENABLE_EXTENDED_TOOLS"} == set(
        CATALOG["extended_tools"]
    )


def test_manifest_records_no_local_or_secret_values():
    text = (REPO / "docs" / JSON_NAME).read_text() + (REPO / "docs" / MARKDOWN_NAME).read_text()
    manifest = json.loads((REPO / "docs" / JSON_NAME).read_text())

    for fragment in ("://", "/home/", "/tmp/", "/Users/", "/private/"):
        assert fragment not in text, fragment
    assert not re.search(r"\b[0-9a-f]{64}\b", text)  # no fingerprints or other digests
    assert not any("fingerprint" in field for tool in manifest["tools"].values() for field in tool)
    assert manifest["flags"]["DAVE_TOOL_ROOTS"]["default"] == []
    assert all(flag["default"] is False for name, flag in manifest["flags"].items() if name != "DAVE_TOOL_ROOTS")
    assert {route["authentication"] for route in manifest["routes"]} == {"X-API-Key"}
