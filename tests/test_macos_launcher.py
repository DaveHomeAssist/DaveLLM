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
        '{id: "duncan", name: "Duncan", url: $duncan_url}',
        'if $duncan_node == null then [] else [$duncan_node] end',
        "optional node unavailable: duncan",
        "optional node offline or not found: duncan",
    ):
        assert required in launcher

    assert "resolve_node_ip duncan" not in launcher


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
