#!/bin/zsh

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
umask 077

SCRIPT_DIR="${0:A:h}"
REPO_ROOT="${SCRIPT_DIR:h:h}"
RUNNER_PATH="${SCRIPT_DIR}/launch-davellm.sh"
KEYCHAIN_SERVICE="${DAVE_KEYCHAIN_SERVICE:-com.davellm.api-key}"
KEYCHAIN_ACCOUNT="${DAVE_KEYCHAIN_ACCOUNT:-${USER:-$(/usr/bin/id -un)}}"
APP_DIR="${HOME}/Applications"
APP_PATH="${APP_DIR}/DaveLLM Launcher.app"
APP_SUPPORT_DIR="${HOME}/Library/Application Support/DaveLLM"
BACKUP_DIR="${APP_SUPPORT_DIR}/Launcher Backups"
LOG_DIR="${HOME}/Library/Logs/DaveLLM"
LOG_PATH="${LOG_DIR}/launcher.log"

fail() {
    print -u2 -- "DaveLLM launcher installer: $1"
    exit 1
}

escape_applescript() {
    print -r -- "$1" | /usr/bin/sed 's/\\/\\\\/g; s/"/\\"/g'
}

[[ "$(/usr/bin/uname -s)" == "Darwin" ]] || fail "this installer requires macOS"
[[ -x "$RUNNER_PATH" ]] || fail "launcher is not executable: ${RUNNER_PATH}"
command -v security >/dev/null 2>&1 || fail "macOS security command is unavailable"
command -v openssl >/dev/null 2>&1 || fail "openssl is unavailable"
command -v osacompile >/dev/null 2>&1 || fail "osacompile is unavailable"
command -v codesign >/dev/null 2>&1 || fail "codesign is unavailable"
command -v tailscale >/dev/null 2>&1 || fail "Tailscale CLI is unavailable"
command -v jq >/dev/null 2>&1 || fail "jq is unavailable"
command -v npm >/dev/null 2>&1 || fail "npm is unavailable"
[[ -x "${REPO_ROOT}/venv/bin/python" ]] || fail "Python environment missing; run the README setup first"
[[ -d "${REPO_ROOT}/node_modules/electron" ]] || fail "Electron dependency missing; run npm ci first"

if /usr/bin/security find-generic-password \
    -a "$KEYCHAIN_ACCOUNT" \
    -s "$KEYCHAIN_SERVICE" \
    -w >/dev/null 2>&1; then
    print -- "Preserved existing Keychain item: ${KEYCHAIN_SERVICE}"
else
    generated_key="$(openssl rand -hex 32)"
    /usr/bin/security add-generic-password \
        -U \
        -a "$KEYCHAIN_ACCOUNT" \
        -s "$KEYCHAIN_SERVICE" \
        -w "$generated_key" >/dev/null
    unset generated_key
    print -- "Created Keychain item: ${KEYCHAIN_SERVICE}"
fi

/bin/mkdir -p "$APP_DIR" "$BACKUP_DIR" "$LOG_DIR"

compiled_app="${APP_DIR}/.DaveLLM Launcher.$$.app"
runner_escaped="$(escape_applescript "$RUNNER_PATH")"
log_escaped="$(escape_applescript "$LOG_PATH")"

/usr/bin/osacompile -o "$compiled_app" \
    -e 'on run' \
    -e "set runnerPath to \"${runner_escaped}\"" \
    -e "set logPath to \"${log_escaped}\"" \
    -e 'try' \
    -e 'do shell script "/bin/zsh " & quoted form of runnerPath & " >> " & quoted form of logPath & " 2>&1"' \
    -e 'on error' \
    -e 'display alert "DaveLLM could not start" message "See " & logPath & " for details." as critical' \
    -e 'end try' \
    -e 'end run'

/usr/libexec/PlistBuddy -c 'Set :CFBundleName DaveLLM Launcher' "${compiled_app}/Contents/Info.plist"
if ! /usr/libexec/PlistBuddy -c 'Set :CFBundleDisplayName DaveLLM Launcher' "${compiled_app}/Contents/Info.plist" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c 'Add :CFBundleDisplayName string DaveLLM Launcher' "${compiled_app}/Contents/Info.plist"
fi
if ! /usr/libexec/PlistBuddy -c 'Set :CFBundleIdentifier com.davellm.launcher' "${compiled_app}/Contents/Info.plist" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c 'Add :CFBundleIdentifier string com.davellm.launcher' "${compiled_app}/Contents/Info.plist"
fi
/usr/bin/codesign --force --deep --sign - "$compiled_app" >/dev/null

if [[ -e "$APP_PATH" ]]; then
    backup_path="${BACKUP_DIR}/DaveLLM Launcher $(/bin/date +%Y%m%d-%H%M%S).app"
    /bin/mv "$APP_PATH" "$backup_path"
    print -- "Moved the previous launcher to: ${backup_path}"
fi

/bin/mv "$compiled_app" "$APP_PATH"
/usr/bin/touch "$LOG_PATH"

print -- "Installed: ${APP_PATH}"
print -- "Runtime log: ${LOG_PATH}"
print -- "Double-click DaveLLM Launcher in ~/Applications to start DaveLLM."
