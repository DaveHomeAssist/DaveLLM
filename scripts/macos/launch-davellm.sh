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

resolve_optional_node_ip() {
    local node_name="$1"
    local node_ip
    node_ip="$(print -r -- "$tailscale_status" | "$JQ_BIN" -r --arg node "$node_name" '
        [
            .Peer[]
            | select(((.HostName // "") | ascii_downcase) == ($node | ascii_downcase))
            | select(.Online == true)
            | .TailscaleIPs[]
            | select(test("^[0-9]+(\\.[0-9]+){3}$"))
        ][0] // empty
    ' 2>/dev/null)" || return 0
    [[ -n "$node_ip" ]] && print -r -- "$node_ip"
    return 0
}

ollama_is_healthy() {
    local node_url="$1"
    /usr/bin/curl --fail --silent --max-time 4 "${node_url}/api/tags" >/dev/null 2>&1
}

dominic_ip="$(resolve_node_ip dominic)"
walter_ip="$(resolve_node_ip walter)"
duncan_node="null"
duncan_ip="$(resolve_optional_node_ip duncan)"
if [[ -n "$duncan_ip" ]]; then
    duncan_url="http://${duncan_ip}:11434"
    if ollama_is_healthy "$duncan_url"; then
        duncan_node="$($JQ_BIN -cn \
            --arg duncan_url "$duncan_url" \
            '{id: "duncan", name: "Duncan", url: $duncan_url,
              profile: {compute: "gpu", model_prompt_token_limits: {"gpt-oss:120b": 2500}}}')"
    else
        print -u2 -- "DaveLLM launcher: optional node unavailable: duncan"
    fi
else
    print -u2 -- "DaveLLM launcher: optional node offline or not found: duncan"
fi
# Capability profiles (DL-ROUTE-01, measured 2026-09-25): prompt sizes each node reads in reasonable
# time. Dominic is CPU-only; gpt-oss:120b on Duncan reads prompts slowly. Advisory: nothing is rerouted.
nodes_json="$($JQ_BIN -cn \
    --arg dominic_url "http://${dominic_ip}:11434" \
    --arg walter_url "http://${walter_ip}:11434" \
    --argjson duncan_node "$duncan_node" \
    '([
        {id: "dominic", name: "Dominic", url: $dominic_url,
         profile: {compute: "cpu", prompt_token_limit: 1300}},
        {id: "walter", name: "Walter", url: $walter_url, profile: {compute: "gpu"}}
    ] + if $duncan_node == null then [] else [$duncan_node] end)')"

# Optional web search: a SearXNG instance on Dominic. Never a startup dependency.
search_url=""
search_candidate="${DAVE_SEARCH_URL:-http://${dominic_ip}:${DAVE_SEARCH_PORT:-8890}}"
if /usr/bin/curl --fail --silent --max-time 4 "${search_candidate}/healthz" >/dev/null 2>&1; then
    search_url="$search_candidate"
else
    print -u2 -- "DaveLLM launcher: optional web search unavailable"
fi

# Optional tool settings: nonsecret JSON files the operator creates in the data folder, so a
# Dock launch gets what a Terminal launch would. A value already in the environment wins.
# Tokens never go in these files (docs/TOOLS100_SETUP.md). Never a startup dependency.
private_settings_file() {
    local file="$1" owner mode
    [[ -f "$file" && ! -L "$file" ]] || return 1
    owner="$(/usr/bin/stat -f '%u' "$file" 2>/dev/null)" || return 1
    mode="$(/usr/bin/stat -f '%Lp' "$file" 2>/dev/null)" || return 1
    [[ "$owner" == "$(/usr/bin/id -u)" ]] && (( (8#$mode & 8#022) == 0 ))
}

tool_env=()
tool_roots_file="${DATA_DIR}/tool-roots.json"
if [[ -z "${DAVE_TOOL_ROOTS:-}" && -e "$tool_roots_file" ]]; then
    if private_settings_file "$tool_roots_file" && tool_roots="$("$JQ_BIN" -ce '
        if type == "array" and length > 0 and all(.[]; type == "string" and startswith("/"))
        then . else error("invalid") end' "$tool_roots_file" 2>/dev/null)"; then
        tool_env+=("DAVE_TOOL_ROOTS=${tool_roots}")
    else
        print -u2 -- "DaveLLM launcher: ignoring tool-roots.json (needs an owner-only list of absolute folders)"
    fi
fi

# Tools100 registers only what toolpack.json names in enabled_tools: every registered schema
# reaches the model on each agent step, and the whole pack would crowd out a small model.
toolpack_file="${DATA_DIR}/toolpack.json"
if [[ -z "${DAVE_TOOLPACK_CONFIG:-}" && -e "$toolpack_file" ]]; then
    if private_settings_file "$toolpack_file" && toolpack_config="$("$JQ_BIN" -ce '
        if type == "object" and (.enabled_tools | type) == "array" and (.enabled_tools | length) > 0
        then . else error("invalid") end' "$toolpack_file" 2>/dev/null)" \
        && (( $(print -rn -- "$toolpack_config" | /usr/bin/wc -c) <= 65536 )); then
        tool_env+=("DAVE_ENABLE_TOOLPACK=${DAVE_ENABLE_TOOLPACK:-true}" "DAVE_TOOLPACK_CONFIG=${toolpack_config}")
    else
        print -u2 -- "DaveLLM launcher: ignoring toolpack.json (needs an owner-only JSON object naming enabled_tools, under 64 KiB)"
    fi
fi

# Model context settings: share the existing router knobs without raising every model's window.
model_context_file="${DATA_DIR}/model-context.json"
if [[ -e "$model_context_file" ]]; then
    if private_settings_file "$model_context_file" \
        && (( $(/usr/bin/wc -c < "$model_context_file") <= 16384 )) \
        && model_context="$("$JQ_BIN" -ce '
            def window($minimum; $maximum):
                type == "number" and . == floor and . >= $minimum and . <= $maximum;
            if type == "object"
                and (keys == ["default_context", "max_context", "models"])
                and (.max_context | window(8192; 262144))
                and (.default_context | window(4096; 262144))
                and (.default_context <= .max_context)
                and (.models | type == "object")
                and (.max_context as $maximum | .models | to_entries | all(.[];
                    (.key | length > 0 and length <= 200)
                    and (.value | window(4096; $maximum))))
            then . else error("invalid") end' "$model_context_file" 2>/dev/null)"; then
        tool_env+=(
            "DAVE_CHAT_NUM_CTX=${DAVE_CHAT_NUM_CTX:-$(print -r -- "$model_context" | "$JQ_BIN" -r '.max_context')}"
            "DAVE_MODEL_CONTEXT_DEFAULT=${DAVE_MODEL_CONTEXT_DEFAULT:-$(print -r -- "$model_context" | "$JQ_BIN" -r '.default_context')}"
            "DAVE_MODEL_CONTEXT_WINDOWS=${DAVE_MODEL_CONTEXT_WINDOWS:-$(print -r -- "$model_context" | "$JQ_BIN" -c '.models')}"
        )
    else
        print -u2 -- "DaveLLM launcher: ignoring model-context.json (needs private, valid context limits, under 16 KiB)"
    fi
fi
# End model context settings.

if /usr/sbin/lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
    fail "TCP port 8000 is already in use; stop the existing DaveLLM/browser-mode process first"
fi

/bin/mkdir -p "$DATA_DIR"
cd "$REPO_ROOT"

exec /usr/bin/env \
    DAVE_API_KEY="$api_key" \
    DAVE_NODES="$nodes_json" \
    DAVE_DATA_DIR="$DATA_DIR" \
    DAVE_ENABLE_TOOLS="${DAVE_ENABLE_TOOLS:-true}" \
    DAVE_ENABLE_EXTENDED_TOOLS="${DAVE_ENABLE_EXTENDED_TOOLS:-true}" \
    DAVE_SEARCH_URL="$search_url" \
    "${tool_env[@]}" \
    "$NPM_BIN" start
