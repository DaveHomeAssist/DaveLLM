#!/bin/zsh

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
umask 077

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h:h}"
KEYCHAIN_SERVICE="${DAVE_KEYCHAIN_SERVICE:-com.davellm.api-key}"
KEYCHAIN_ACCOUNT="${DAVE_KEYCHAIN_ACCOUNT:-${USER:-$(/usr/bin/id -un)}}"
DATA_DIR="${DAVE_DATA_DIR:-${HOME}/Library/Application Support/DaveLLM}"

fail() {
    print -u2 -- "DaveLLM launcher: $1"
    exit 1
}

command_path() {
    local command_name="$1"
    local resolved
    resolved="$(command -v "$command_name" 2>/dev/null || true)"
    [[ -n "$resolved" ]] || fail "required command not found: ${command_name}"
    print -r -- "$resolved"
}

[[ "$(/usr/bin/uname -s)" == "Darwin" ]] || fail "this launcher requires macOS"
[[ -f "${REPO_ROOT}/package.json" ]] || fail "repository not found at ${REPO_ROOT}"
[[ -x "${REPO_ROOT}/venv/bin/python" ]] || fail "Python environment missing; run the README setup first"
[[ -d "${REPO_ROOT}/node_modules/electron" ]] || fail "Electron dependency missing; run npm ci first"

SECURITY_BIN="$(command_path security)"
TAILSCALE_BIN="$(command_path tailscale)"
JQ_BIN="$(command_path jq)"
NPM_BIN="$(command_path npm)"

api_key="$($SECURITY_BIN find-generic-password \
    -a "$KEYCHAIN_ACCOUNT" \
    -s "$KEYCHAIN_SERVICE" \
    -w 2>/dev/null)" || fail "Keychain item ${KEYCHAIN_SERVICE} was not found; rerun the launcher installer"
[[ -n "$api_key" ]] || fail "Keychain item ${KEYCHAIN_SERVICE} is empty"

tailscale_status="$($TAILSCALE_BIN status --json 2>/dev/null)" || fail "Tailscale status is unavailable"

resolve_node_ip() {
    local node_name="$1"
    local node_ip
    node_ip="$(print -r -- "$tailscale_status" | "$JQ_BIN" -er --arg node "$node_name" '
        [
            .Peer[]
            | select(((.HostName // "") | ascii_downcase) == ($node | ascii_downcase))
            | .TailscaleIPs[]
            | select(test("^[0-9]+(\\.[0-9]+){3}$"))
        ][0]
    ' 2>/dev/null)" || fail "Tailscale peer not found: ${node_name}"
    print -r -- "$node_ip"
}

dominic_ip="$(resolve_node_ip dominic)"
walter_ip="$(resolve_node_ip walter)"
nodes_json="$($JQ_BIN -cn \
    --arg dominic_url "http://${dominic_ip}:11434" \
    --arg walter_url "http://${walter_ip}:11434" \
    '[
        {id: "dominic", name: "Dominic", url: $dominic_url},
        {id: "walter", name: "Walter", url: $walter_url}
    ]')"

if /usr/sbin/lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    fail "TCP port 8000 is already in use; stop the existing DaveLLM/browser-mode process first"
fi

/bin/mkdir -p "$DATA_DIR"
cd "$REPO_ROOT"

exec /usr/bin/env \
    DAVE_API_KEY="$api_key" \
    DAVE_NODES="$nodes_json" \
    DAVE_DATA_DIR="$DATA_DIR" \
    "$NPM_BIN" start
