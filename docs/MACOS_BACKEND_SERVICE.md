# macOS backend service

## Problem

The installed Electron app owned the Uvicorn child process. Closing or terminating Electron therefore stopped `http://127.0.0.1:8000`, even when the backend itself was healthy.

## Accepted behavior

- A per-user LaunchAgent named `com.davellm.backend` owns Uvicorn.
- The service resolves Keychain and live Tailscale configuration at process start; no secret or node address is written to its plist.
- Closing or Force Quitting Electron does not stop the backend.
- launchd restarts an unexpectedly terminated backend.
- An unknown listener on port 8000 is reported and preserved.
- `npm run stop:macos` intentionally unloads only the named user service.
- Opening the installed launcher loads a stopped service, requires `/health` to return HTTP 2xx with `status: ok`, and then opens Electron without giving it backend ownership.

## Verification boundaries

Source tests cover the managed Electron lifecycle, service installation contract, port ownership guard, and explicit stop command. Installed acceptance must separately verify UI-close persistence, launchd recovery, intentional stop, and launcher restart on macOS. Login-cycle persistence remains a separate physical session test.
