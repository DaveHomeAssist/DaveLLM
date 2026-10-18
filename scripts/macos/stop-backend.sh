#!/bin/zsh

set -euo pipefail

SERVICE_LABEL="${DAVE_BACKEND_SERVICE_LABEL:-com.davellm.backend}"
SERVICE_TARGET="gui/$(/usr/bin/id -u)/${SERVICE_LABEL}"

if /bin/launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
    /bin/launchctl bootout "$SERVICE_TARGET"
    print -- "Stopped DaveLLM backend service: ${SERVICE_LABEL}"
else
    print -- "DaveLLM backend service is already stopped: ${SERVICE_LABEL}"
fi

