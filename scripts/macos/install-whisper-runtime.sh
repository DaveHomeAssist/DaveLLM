#!/bin/zsh

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
umask 077

APP_SUPPORT_DIR="${DAVE_DATA_DIR:-${HOME}/Library/Application Support/DaveLLM}"
MODEL_DIR="${APP_SUPPORT_DIR}/models"
MODEL_PATH="${DAVE_WHISPER_MODEL:-${MODEL_DIR}/ggml-tiny.en.bin}"
MODEL_PARENT="${MODEL_PATH:h}"
MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin"
MODEL_SHA1="c78c86eb1a8faa21b369bcd33207cc90d64ae9df"

fail() {
    print -u2 -- "DaveLLM Whisper installer: $1"
    exit 1
}

[[ "$(/usr/bin/uname -s)" == "Darwin" ]] || fail "this installer requires macOS"
BREW_BIN="$(command -v brew 2>/dev/null || true)"
[[ -n "$BREW_BIN" ]] || fail "Homebrew is required to install whisper-cpp"
command -v curl >/dev/null 2>&1 || fail "curl is unavailable"
command -v shasum >/dev/null 2>&1 || fail "shasum is unavailable"

if ! command -v whisper-cli >/dev/null 2>&1; then
    "$BREW_BIN" install whisper-cpp
fi
WHISPER_CLI="$(command -v whisper-cli 2>/dev/null || true)"
[[ -x "$WHISPER_CLI" ]] || fail "whisper-cli was not installed successfully"

/bin/mkdir -p "$MODEL_PARENT"
if [[ -f "$MODEL_PATH" ]]; then
    existing_sha1="$(/usr/bin/shasum -a 1 "$MODEL_PATH" | /usr/bin/awk '{print $1}')"
    [[ "$existing_sha1" == "$MODEL_SHA1" ]] || fail "existing model checksum does not match: ${MODEL_PATH}"
else
    model_download="${MODEL_PATH}.download.$$"
    trap '/bin/unlink "$model_download" 2>/dev/null || true' EXIT
    /usr/bin/curl --fail --location --show-error --output "$model_download" "$MODEL_URL"
    downloaded_sha1="$(/usr/bin/shasum -a 1 "$model_download" | /usr/bin/awk '{print $1}')"
    [[ "$downloaded_sha1" == "$MODEL_SHA1" ]] || fail "downloaded model checksum does not match"
    /bin/mv "$model_download" "$MODEL_PATH"
    trap - EXIT
fi
/bin/chmod 600 "$MODEL_PATH"

print -- "Whisper binary: ${WHISPER_CLI}"
print -- "Whisper model: ${MODEL_PATH}"
print -- "DaveLLM local dictation runtime is ready."
