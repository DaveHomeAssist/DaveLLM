import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "macos" / "launch-davellm.sh"
INSTALLER = REPO / "scripts" / "macos" / "install-launcher.sh"
WHISPER_INSTALLER = REPO / "scripts" / "macos" / "install-whisper-runtime.sh"


def run_context_settings(
    tmp_path: Path, contents: str | None, *, trusted: bool = True,
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("launcher validation requires jq")
    if contents is not None:
        (tmp_path / "model-context.json").write_text(contents)
    launcher = LAUNCHER.read_text()
    section = launcher.split("# Model context settings:", 1)[1].split(
        "# End model context settings.", 1
    )[0]
    section = section.split("\n", 1)[1]
    # Run the actual launcher section with portable stand-ins for zsh print and
    # the existing, separately pinned macOS ownership check. No app is started.
    script = r'''
set -euo pipefail
print() {
    if [[ "$1" == "-u2" ]]; then
        shift 2
        printf '%s\n' "$*" >&2
    else
        shift 2
        printf '%s\n' "$*"
    fi
}
private_settings_file() { [[ "$PRIVATE_SETTINGS_OK" == "1" ]]; }
tool_env=()
''' + section + '\nprintf "%s\\n" "${tool_env[@]-}"\n'
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    env.update(DATA_DIR=str(tmp_path), JQ_BIN=jq, PRIVATE_SETTINGS_OK=str(int(trusted)))
    env.update(overrides or {})
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True,
        check=True, timeout=5,
    )


def test_model_context_settings_preserve_other_model_defaults(tmp_path: Path) -> None:
    result = run_context_settings(tmp_path, json.dumps({
        "max_context": 32768, "default_context": 16384,
        "models": {"qwen3.5:4b": 32768},
    }))
    values = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert values["DAVE_CHAT_NUM_CTX"] == "32768"
    assert values["DAVE_MODEL_CONTEXT_DEFAULT"] == "16384"
    assert json.loads(values["DAVE_MODEL_CONTEXT_WINDOWS"]) == {"qwen3.5:4b": 32768}
    assert not result.stderr


def test_model_context_environment_overrides_win_individually(tmp_path: Path) -> None:
    overrides = {"DAVE_CHAT_NUM_CTX": "65536", "DAVE_MODEL_CONTEXT_WINDOWS": '{"custom":8192}'}
    result = run_context_settings(tmp_path, json.dumps({
        "max_context": 32768, "default_context": 16384, "models": {},
    }), overrides=overrides)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert values == {**overrides, "DAVE_MODEL_CONTEXT_DEFAULT": "16384"}


@pytest.mark.parametrize("contents", [
    "not json", "[]", "{}", '{"secret":"must-not-be-logged"}',
    json.dumps({"max_context": 32768, "default_context": 16384, "models": {}, "extra": 1}),
    json.dumps({"max_context": 4096, "default_context": 4096, "models": {}}),
    json.dumps({"max_context": 32768.5, "default_context": 16384, "models": {}}),
    json.dumps({"max_context": 1048576, "default_context": 16384, "models": {}}),
    json.dumps({"max_context": 32768, "default_context": 65536, "models": {}}),
    json.dumps({"max_context": 32768, "default_context": 16384, "models": []}),
    json.dumps({"max_context": 32768, "default_context": 16384, "models": {"": 8192}}),
    json.dumps({"max_context": 32768, "default_context": 16384, "models": {"m": 65536}}),
    json.dumps({"max_context": 32768, "default_context": 16384, "models": {"m": True}}),
    " " * 16385,
])
def test_invalid_model_context_settings_are_ignored(tmp_path: Path, contents: str) -> None:
    result = run_context_settings(tmp_path, contents)
    assert not result.stdout.strip()
    assert result.stderr == (
        "DaveLLM launcher: ignoring model-context.json "
        "(needs private, valid context limits, under 16 KiB)\n"
    )


def test_missing_and_untrusted_model_context_settings_do_not_apply(tmp_path: Path) -> None:
    assert not run_context_settings(tmp_path, None).stdout.strip()
    settings = json.dumps({"max_context": 32768, "default_context": 16384, "models": {}})
    result = run_context_settings(tmp_path, settings, trusted=False)
    assert not result.stdout.strip()
    assert "ignoring model-context.json" in result.stderr


def test_macos_launcher_uses_keychain_and_live_tailscale_inventory():
    launcher = LAUNCHER.read_text()

    for required in (
        "security",
        "find-generic-password",
        "com.davellm.api-key",
        "tailscale",
        "status --json",
        "resolve_node_ip dominic",
        "resolve_node_ip walter",
        'DAVE_API_KEY="$api_key"',
        'DAVE_NODES="$nodes_json"',
        'DAVE_DATA_DIR="$DATA_DIR"',
        'DAVE_ENABLE_TOOLS="${DAVE_ENABLE_TOOLS:-true}"',
        'DAVE_BACKEND_MANAGED=true',
        '"${REPO_ROOT}/venv/bin/python" -m uvicorn',
    ):
        assert required in launcher

    assert not re.search(r"http://(?:\d{1,3}\.){3}\d{1,3}:11434", launcher)
    assert "localStorage" not in launcher
    assert "sessionStorage" not in launcher


def test_macos_launcher_adds_only_a_healthy_optional_duncan_node():
    launcher = LAUNCHER.read_text()

    for required in (
        "resolve_optional_node_ip duncan",
        'select(.Online == true)',
        'ollama_is_healthy "$duncan_url"',
        '"${node_url}/api/tags"',
        '{id: "duncan", name: "Duncan", url: $duncan_url,',
        'profile: {compute: "gpu", model_prompt_token_limits: {"gpt-oss:120b": 2500}}}',
        'if $duncan_node == null then [] else [$duncan_node] end',
        "optional node unavailable: duncan",
        "optional node offline or not found: duncan",
    ):
        assert required in launcher

    assert "resolve_node_ip duncan" not in launcher


def test_macos_launcher_enables_tools_but_not_shell_or_roots():
    launcher = LAUNCHER.read_text()

    # Tools are on for the desktop app (overridable); the shell tool and file roots stay opt-in.
    assert 'DAVE_ENABLE_TOOLS="${DAVE_ENABLE_TOOLS:-true}"' in launcher
    assert 'DAVE_ENABLE_EXTENDED_TOOLS="${DAVE_ENABLE_EXTENDED_TOOLS:-true}"' in launcher
    assert "DAVE_ENABLE_SHELL_TOOL" not in launcher
    # Roots come only from the environment or an operator-created file, never a default.
    assert 'tool_env+=("DAVE_TOOL_ROOTS=${tool_roots}")' in launcher
    assert not re.search(r'DAVE_TOOL_ROOTS=["\']?\[', launcher)


def test_macos_launcher_reads_tool_settings_only_from_private_files():
    launcher = LAUNCHER.read_text()

    for required in (
        'tool_roots_file="${DATA_DIR}/tool-roots.json"',
        'toolpack_file="${DATA_DIR}/toolpack.json"',
        '[[ -f "$file" && ! -L "$file" ]] || return 1',
        '[[ "$owner" == "$(/usr/bin/id -u)" ]] && (( (8#$mode & 8#022) == 0 ))',
        'if [[ -z "${DAVE_TOOL_ROOTS:-}" && -e "$tool_roots_file" ]]; then',
        'if [[ -z "${DAVE_TOOLPACK_CONFIG:-}" && -e "$toolpack_file" ]]; then',
        '(.enabled_tools | type) == "array" and (.enabled_tools | length) > 0',
        '<= 65536',
        '"${REPO_ROOT}/venv/bin/python" -m uvicorn',
    ):
        assert required in launcher

    # Settings files carry no secrets, and the launcher reads no token for them.
    assert "TOKEN" not in launcher
    assert not re.search(r"ignoring [^\n]*\$tool_roots|ignoring [^\n]*\$toolpack_config", launcher)


def test_macos_launcher_adds_web_search_only_when_searxng_is_healthy():
    launcher = LAUNCHER.read_text()

    for required in (
        'search_candidate="${DAVE_SEARCH_URL:-http://${dominic_ip}:${DAVE_SEARCH_PORT:-8890}}"',
        '"${search_candidate}/healthz"',
        'search_url="$search_candidate"',
        "optional web search unavailable",
        'DAVE_SEARCH_URL="$search_url"',
    ):
        assert required in launcher
    # The log line never carries the search address.
    assert not re.search(r"unavailable[^\n]*search_(?:url|candidate)", launcher)


def test_macos_installer_generates_a_key_and_one_click_app_without_printing_it():
    installer = INSTALLER.read_text()

    for required in (
        "openssl rand -hex 32",
        "security add-generic-password",
        "osacompile",
        "com.davellm.launcher",
        "codesign --force --deep --sign -",
        "DaveLLM Launcher.app",
        "Launcher Backups",
        "Library/Logs/DaveLLM",
        "Preserved existing Keychain item",
        "com.davellm.backend",
        "RunAtLoad",
        "KeepAlive",
        "backend.error.log",
    ):
        assert required in installer

    assert 'print -- "$generated_key"' not in installer
    assert not re.search(r"http://(?:\d{1,3}\.){3}\d{1,3}:11434", installer)


def test_macos_installer_hands_off_without_waiting_for_the_app():
    installer = INSTALLER.read_text()
    assert '/usr/bin/nohup /bin/zsh ' in installer
    assert ' < /dev/null >> ' in installer
    assert ' 2>&1 &"' in installer


def test_macos_launcher_manages_backend_with_launchd_and_refuses_unknown_listener():
    launcher = LAUNCHER.read_text()
    for required in (
        'SERVICE_LABEL="${DAVE_BACKEND_SERVICE_LABEL:-com.davellm.backend}"',
        '[[ "${1:-}" == "--backend-only" ]]',
        'launchctl bootstrap',
        'launchctl kickstart',
        "refusing to start a duplicate",
        "refusing to replace or duplicate its listener",
        "http://127.0.0.1:8000/health",
        "DAVE_BACKEND_MANAGED=true",
    ):
        assert required in launcher
    assert "launchctl kill" not in launcher
    assert "kill -9" not in launcher


def test_macos_stop_command_unloads_only_the_named_backend_service():
    stop_script = (REPO / "scripts" / "macos" / "stop-backend.sh").read_text()
    assert "com.davellm.backend" in stop_script
    assert 'launchctl bootout "$SERVICE_TARGET"' in stop_script
    assert "kill" not in stop_script


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS AppleScript")
def test_installed_applescript_command_returns_while_child_is_alive(tmp_path: Path):
    # Exercise the exact generated AppleScript command, not a lookalike shell.
    # Quoted paths and redirected descriptors are required for an actual handoff.
    runner = tmp_path / "Dave's test runner.sh"
    log = tmp_path / "Dave's launcher.log"
    runner.write_text('#!/bin/zsh\nprint -r -- $$\nexec /bin/sleep 5\n')
    command = next(
        line.strip()[4:-3] for line in INSTALLER.read_text().splitlines()
        if line.strip().startswith("-e 'do shell script ")
    )
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", f'set runnerPath to "{runner}"',
             "-e", f'set logPath to "{log}"', "-e", command],
            capture_output=True, text=True, timeout=3, check=True,
        )
        assert time.monotonic() - started < 3
        assert not result.stdout.strip()
        deadline = time.monotonic() + 1
        while (not log.exists() or not log.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.02)
        child_pid = int(log.read_text().strip())
        os.kill(child_pid, 0)
    finally:
        if log.exists() and log.read_text().strip():
            try:
                os.kill(int(log.read_text().strip()), signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_macos_whisper_installer_keeps_the_verified_model_out_of_source():
    installer = WHISPER_INSTALLER.read_text()

    for required in (
        "brew",
        "install whisper-cpp",
        "ggml-tiny.en.bin",
        "c78c86eb1a8faa21b369bcd33207cc90d64ae9df",
        "Library/Application Support/DaveLLM",
        "shasum -a 1",
    ):
        assert required in installer

    assert "MODEL_PATH=\"${DAVE_WHISPER_MODEL:-${MODEL_DIR}/ggml-tiny.en.bin}\"" in installer
