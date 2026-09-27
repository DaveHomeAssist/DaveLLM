import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "scripts" / "macos" / "launch-davellm.sh"
INSTALLER = REPO / "scripts" / "macos" / "install-launcher.sh"
WHISPER_INSTALLER = REPO / "scripts" / "macos" / "install-whisper-runtime.sh"


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
        '"$NPM_BIN" start',
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
    assert "DAVE_TOOL_ROOTS" not in launcher


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
    ):
        assert required in installer

    assert 'print -- "$generated_key"' not in installer
    assert not re.search(r"http://(?:\d{1,3}\.){3}\d{1,3}:11434", installer)


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
