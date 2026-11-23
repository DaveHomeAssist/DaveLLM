# DL-DISPLAY-01 — Dominic 4K operations wallboard

## System overview

Turn Dominic's built-in 4K laptop display into an always-on, read-only DaveLLM operations wallboard. The wallboard presents the same validated on-call snapshot already owned by the DaveLLM Cluster view; it does not become a second monitor, perform recovery, hold the DaveLLM API key, or expose the Mac router over the tailnet.

```text
[hourly on-call technician on Mac]
        |
        v
[record_monitor_status.py]
        | validates + writes atomically
        +------------------------------> [Mac Cluster view]
        |
        v
[bounded status publisher over SSH]
        |
        v
[Dominic local status file] -> [local read-only HTTP server] -> [4K kiosk browser]
                                      |                              |
                                      +-- stale/failure contract ----+
```

### Inside the boundary

- A reusable browser renderer for the existing on-call status schema.
- A bounded publisher that copies only the validated operational snapshot from the Mac to Dominic.
- A local, unprivileged, read-only server on Dominic.
- A fullscreen kiosk session on Dominic's attached display.
- Boot, restart, stale-data, and display-power behavior.

### Outside the boundary

- Node recovery logic, service restarts, model pulls, chat, tools, project data, prompts, and credentials.
- General remote desktop, VNC, RDP, AnyDesk, or surveillance.
- Exposing the Mac backend or its API key to Dominic.
- Replacing the existing Cluster view or hourly automation.
- New inference qualification or model changes.

## User problem and expected outcome

The cluster state currently lives in an interactive application and is easy to miss. Dominic already has an attached 4K panel and normally runs unattended. The wallboard makes node health, freshness, latency, and recovery history visible at a glance from across the room while keeping all operational decisions in the existing on-call workflow.

Success looks like this: after Dominic boots, its screen reaches the wallboard without operator input; every successful hourly check appears within two minutes of publication; stale or missing data is impossible to mistake for healthy data; and the display workload does not materially interfere with Dominic's existing services.

## Smallest useful slice

The first release shows only trusted data already produced by `scripts/record_monitor_status.py`:

1. Overall state: healthy, degraded, outage, recovered, or stale.
2. Four fixed target cards: Local, Dominic, Walter, and Duncan.
3. Endpoint label, HTTP status, complete-response latency, and target state.
4. Last checked time, age, summary, and any recovery record.
5. Last 24 checks as a state timeline, with access to the retained seven-day history in a bounded panel.
6. A permanent connection/freshness indicator for the local status source.

No model inventory, GPU telemetry, temperatures, job queue, or storage metrics enter the first slice. Those are later increments because they have different sources, refresh rates, and failure meanings.

## Components

| Component | Single responsibility | Inputs | Outputs | Owner |
| --- | --- | --- | --- | --- |
| Existing recorder | Validate and persist one on-call check | CLI target records | `on_call_status.json` | DaveLLM |
| Snapshot exporter | Produce the public-safe display projection | Validated recorder file | `wallboard-status.json` | DaveLLM |
| Dominic publisher | Transfer one projection atomically | Projection file, documented SSH route | Staged then renamed file on Dominic | DaveLLM operations |
| Shared status renderer | Turn the schema into safe DOM nodes | Status JSON | Accessible status components | DaveLLM UI |
| Wallboard shell | Provide the 4K read-only presentation | Shared renderer, local JSON | Viewport-locked wallboard | DaveLLM UI |
| Local static server | Serve wallboard assets and status on loopback only | Installed asset directory | HTTP on `127.0.0.1` | Dominic system service |
| Kiosk session | Keep one browser fullscreen on the attached panel | Loopback wallboard URL | Visible 4K display | Dominic system service |

## Data and interface contracts

### Display projection

The exporter accepts only a recorder document that passes the existing schema validation. It emits a smaller versioned JSON object:

```json
{
  "version": 1,
  "published_at": "2026-10-20T01:58:16Z",
  "current": {
    "checked_at": "2026-10-20T01:58:16Z",
    "overall": "healthy",
    "summary": "All monitored targets are healthy.",
    "targets": [],
    "recovery": null
  },
  "history": []
}
```

The target and recovery records retain the existing `on_call_status.json` fields. The projection contains no API key, prompt, conversation, model response, user data, filesystem path, command output, SSH detail, or raw error text. Endpoint values remain operational metadata and are visible only on Dominic's physical screen and loopback service.

### Publication

- Trigger: immediately after a successful recorder write on every hourly run, including outages and failed recovery attempts.
- Transport: the documented strict-host-key SSH route to Dominic.
- Write contract: upload to a sibling temporary file, verify non-empty valid JSON and owner, then rename atomically.
- Timeout: bounded independently from the health check. A publication failure never rewrites the last good file.
- Result: success, unavailable, refused, or invalid. It never claims the screen updated merely because SSH connected.
- Retry: one bounded retry after a short delay; no persistent retry loop.

### Wallboard fetch

- Protocol: unauthenticated loopback HTTP on Dominic only.
- Cache: `Cache-Control: no-store`; the client also uses a cache-busting timestamp.
- Poll interval: 15 seconds.
- Validity: reject malformed JSON and unsupported versions without replacing the last rendered good snapshot.
- Freshness: fresh through 75 minutes, delayed from 75 to 120 minutes, stale after 120 minutes, and missing when no valid snapshot exists.
- Failure: after two failed fetches, show a source warning. Never change an outage to healthy because a fetch failed.

## 4K interface specification

### Primary target

- Landscape 3840 × 2160, with the physical EDID and effective desktop scale verified during installation.
- Viewport locked to `100dvh` and `100vw`; the page never scrolls.
- Light theme is the first-run default. A visible light/dark toggle persists locally on Dominic; dark may be selected for normal wallboard use.
- No auto-rotating pages. All critical state remains simultaneously visible.

### Layout

```text
+--------------------------------------------------------------------------------+
| DaveLLM Operations       OVERALL HEALTHY       checked 8m ago       21:58 EDT |
+-------------------+-------------------+-------------------+--------------------+
| Local             | Dominic           | Walter            | Duncan             |
| HEALTHY           | HEALTHY           | HEALTHY           | HEALTHY            |
| 87 ms · HTTP 200  | 22 ms · HTTP 200  | 262 ms · HTTP 200 | 239 ms · HTTP 200  |
+-------------------+-------------------+-------------------+--------------------+
| Current summary / recovery notice                                             |
+------------------------------------------------------+-------------------------+
| Last 24 checks timeline                               | Freshness / source      |
|                                                      | Last recovery          |
+------------------------------------------------------+-------------------------+
| Seven-day incident history (bounded local panel)                                |
+--------------------------------------------------------------------------------+
```

### Legibility

- Overall state: 64–88 CSS px using `clamp()`.
- Target name: 38–48 px; target state: 30–40 px; latency: 56–72 px.
- Supporting labels: never below 22 px at the 4K target.
- State is conveyed by text, icon, and color together.
- Healthy uses restrained green; degraded amber; outage red; recovered cyan; stale gray/amber striping.
- Contrast meets WCAG AA. Reduced-motion mode removes all transitions.
- Full-screen alert treatment changes the perimeter and headline only; no flashing.

### Controls

- Theme toggle.
- Refresh now.
- Show/hide endpoint details.
- Open seven-day history.
- Exit fullscreen through the standard browser shortcut only; no operational controls are added.

## Dominic runtime

Current read-only inspection found `graphical.target`, Intel HD Graphics 630, and NVIDIA Quadro M1200 graphics, but no active display manager, desktop shell, VNC/RDP listener, or detected remote-display service. The physical 4K panel is confirmed by Dave; its live EDID and scale remain an installation check.

Use a minimal dedicated kiosk session rather than a general desktop:

1. Dedicated unprivileged display account with no sudo and no DaveLLM credentials.
2. Minimal Wayland/X kiosk runtime selected only after confirming the supported Ubuntu 24.04 package source and Intel display compatibility.
3. One browser process pointed at the loopback wallboard.
4. A system service for the loopback static server and a supervised kiosk service for the graphical session.
5. Automatic start after boot, bounded restart on crash, and an explicit operator stop command.
6. Display power schedule is a separate operator setting; monitoring and publication continue while the panel sleeps.

Prefer the Intel integrated GPU for display composition. Do not bind the kiosk to the NVIDIA device until current Ollama/GPU ownership is checked. Do not install a full desktop environment unless the minimal kiosk path fails acceptance.

## Failure modes

| Failure | Required behavior | Fallback |
| --- | --- | --- |
| Hourly check does not run | Existing snapshot ages into delayed then stale | Show last known state and exact age |
| Publisher cannot reach Dominic | Mac Cluster remains authoritative; publisher reports failure | Dominic keeps last good snapshot and becomes stale |
| Invalid projection arrives | Atomic activation is refused | Keep last good file and show source warning |
| Local server stops | Kiosk shows browser connection failure; service restarts boundedly | No false healthy state |
| Browser crashes | Supervisor restarts only the kiosk browser | Static server and snapshot remain intact |
| Dominic reboots | Services start in dependency order and render the last snapshot as stale until refreshed | No operator login required |
| One node is down | Its card and overall state show the recorder result | No restart action from the wallboard |
| Mac is down | Local target eventually appears stale according to the last published record | Dominic must not synthesize Mac health |
| Screen sleeps | Data publication and local serving continue | Wake reveals current or explicitly stale state |

## Performance and coexistence budget

- One browser tab, no WebSocket, no animation loop, no video, and no runtime CDN.
- Static server idle CPU effectively zero; browser average CPU below 2% after render and RSS below 600 MiB.
- Status file and retained assets below 5 MiB.
- No NVIDIA allocation attributable to the kiosk unless separately accepted.
- No service restart, model unload, or inference configuration change during installation.
- Compare Dominic `/api/tags` latency and system load before and after activation. A sustained regression greater than 10% or visible thermal/fan impact blocks acceptance pending tuning.

## Security and privacy

- Never store or transmit `DAVE_API_KEY` to Dominic.
- Bind the wallboard HTTP server to loopback only.
- Preserve strict SSH host-key checking; do not weaken the existing access route.
- Transfer only the versioned display projection.
- The display account can read the installed wallboard and status file but cannot write source or control Ollama.
- Render all strings with DOM text nodes. No raw HTML or dynamic script evaluation.
- Log publication result and service lifecycle metadata only; no snapshot body in logs.

## Implementation slices

### Slice A — Shared renderer and projection

- Extract the on-call DOM rendering into a browser module reused by Cluster and the wallboard.
- Add a deterministic exporter and schema tests.
- Add `static/wallboard.html`, its small controller, and wallboard CSS.
- Add fixture browser tests at 3840 × 2160, 1920 × 1080, 1440 × 900, and 320 × 256.

### Slice B — Publication and Dominic installer

- Add a bounded publisher CLI with atomic remote activation and safe failure output.
- Add system-service and kiosk templates plus install, verify, and stop commands.
- Preserve existing services and inspect port/display ownership before activation.

### Slice C — Installed acceptance

- Verify physical EDID, effective scale, startup after reboot, dark/light themes, stale transition, outage rendering, browser recovery, and screen sleep/wake.
- Measure resource usage and Dominic service latency before and after.
- Update the hourly automation only after the installed publisher and display have passed.

### Later increments

- Model inventory and loaded-model state.
- GPU/CPU temperature, memory, storage, and Ollama process metrics.
- Active job queue and runner capacity.
- Quiet-hours display schedule and optional low-light palette.

Each later source must define its own timestamp, refresh interval, stale threshold, and failure contract. It may not reuse the hourly snapshot timestamp as proof of live telemetry.

## Acceptance criteria

1. The shared renderer produces equivalent healthy, degraded, outage, recovered, stale, missing, and malformed states in Cluster and the wallboard.
2. No root overflow exists at 3840 × 2160, 1920 × 1080, 1440 × 900, or 320 × 256; only the seven-day history panel may scroll internally.
3. All critical text is readable at 4K from the intended viewing position during physical acceptance.
4. Publication copies only a validated projection, activates it atomically, and leaves the prior file untouched on timeout, invalid input, or remote refusal.
5. Dominic serves the page on loopback only and holds no DaveLLM API key.
6. Missing or stale data is never presented as healthy.
7. Reboot reaches the wallboard without manual login; killing the browser causes bounded automatic recovery.
8. The existing Ollama service, models, and DaveLLM node configuration remain unchanged.
9. Resource and latency budgets pass, with the actual execution host and measurements recorded.
10. The active hourly automation publishes only after installed acceptance and continues to escalate meaningful incidents through the existing thread.

## Verification plan

- Unit: projection schema, rejection, history cap, timestamp parsing, and error redaction.
- DOM: shared renderer parity and safe text construction.
- Browser: viewport, theme, accessibility, stale timing, fetch failure, and no-page-scroll checks.
- Linux service: syntax/validation for service definitions and install/stop scripts in a disposable path.
- Installed Dominic: port ownership, EDID, display scale, service ownership, reboot, browser crash, screen sleep/wake, loopback binding, resource budget, and source readback.
- End to end: one manually invoked hourly-style check appears on both the Mac Cluster view and Dominic within two minutes with matching values.

## Evidence boundaries

This brief is a proposed build contract, not implementation approval or evidence that Dominic's display is configured. Source tests will not prove the physical panel, boot path, effective scale, operator readability, or inference coexistence. Those remain installed and human-acceptance gates.

## Slice A source receipt, October 30, 2026

Implemented the shared Cluster and display renderer, standalone wallboard shell, and a bounded projection exporter. Free-form summaries and recovery commands are replaced with fixed display text; endpoint credentials, query strings, invalid states, non-finite metrics and inconsistent healthy results are refused. Invalid input preserves the existing destination.

Dominic passed 12 Python projection checks, ten DOM/console checks and browser fixtures at six viewports, including 4K, ultrawide and compact 320 by 256. Theme persistence, missing and malformed input, outage retention and delayed/stale boundaries passed. The compact screenshot was inspected. Required PR CI and source review remain separate.

Export with `python scripts/export_wallboard_status.py SOURCE_JSON OUTPUT_JSON` using explicit local paths. This command writes a local projection only. Publisher, service installer, physical display activation, startup, resource coexistence and human acceptance remain open. No runtime or service was changed in this slice.


## Slice B publisher source, October 30, 2026

The bounded publisher accepts a recorder file, validates it through the existing projection exporter, and transfers only the sanitized projection. It invokes the existing SSH alias with BatchMode and strict host key checking, a five-second connection bound and a fifteen-second attempt bound. One retry sends identical bytes. Remote error text and payload bodies are never printed.

The receiver requires an existing private directory owned by the remote user. It rejects symlink destinations, verifies the transferred digest, writes a unique sibling temporary file, flushes it, checks owner and exact readback, then atomically replaces the destination. It installs no server, service, account, timer or display runtime.

After separately authorized installation and publication, the CLI is `python scripts/publish_wallboard_status.py SOURCE_JSON --host DOCUMENTED_ALIAS --destination ABSOLUTE_STATUS_PATH`. This sprint runs only synthetic transport and isolated temporary-directory fixtures. No actual wallboard publication is authorized or performed.

Results are `success`, `invalid`, `refused` or `unavailable`. Success proves the receiver acknowledged an atomic file update, not that a screen changed. Invalid input and failures before replacement preserve the previous file. A timeout after remote replacement can lose the acknowledgement: `unavailable` therefore means activation is unconfirmed, rather than proving the old file remained. Retrying identical bytes is idempotent; the lost-acknowledgement regression covers this boundary explicitly. A later installed verifier must read the destination before resolving an ambiguous result.

Service/kiosk templates, installer selection, activation and installed or human acceptance remain open. Source preparation can continue independently of the queued candidate CI.

Dominic source verification: all 24 projection and publisher tests pass with Python 3.12 in an isolated container. The fixture invokes the real receiver as a subprocess against disposable files; it does not contact a publication host. Required repository CI remains separate.


## Loopback service preparation scope

Prepare an allowlisted read-only HTTP server and a staged user-service bundle. The server binds IPv4 loopback only, exposes exactly the four wallboard assets plus the sanitized status projection, disables caching and rejects directory, symlink and traversal requests. Missing or invalid status must fail without serving raw operational input. The stager writes only to a new explicit directory, records content hashes, and refuses existing destinations. It never installs, enables or starts a service. Test real loopback HTTP and staged bundle verification in an isolated runner container. Kiosk runtime/account selection and real installation remain separate from these source fixtures.

## Loopback service source receipt, October 30, 2026

The stdlib server serves only the four display assets and a revalidated projection on IPv4 loopback, port 8766 by default. It accepts only localhost or 127.0.0.1 Host headers for its bound port. Exact original request targets prevent HTTP parser normalization from widening the route allowlist. Reads are limited to regular, non-symlink files below 1 MiB; unknown routes, write methods, invalid status and unexpected Host headers are refused. Responses disable caching and carry a restrictive content security policy. Publication time is preserved, never refreshed by a read.

`python3 scripts/stage_wallboard.py /tmp/davellm-wallboard-bundle` creates a new private staging directory and verifies SHA-256 hashes for the eight bundled files. The destination must not already exist. `python3 scripts/stage_wallboard.py /tmp/davellm-wallboard-bundle --verify` checks its contents again without modifying it. Staging is preparation only. The bundle includes a user service template, but the command neither copies that template into a service directory nor invokes systemctl.

The template expects reviewed assets in the service owner's `.local/share/davellm-wallboard` directory and the publisher's private status file in `.local/state/davellm-wallboard/status.json`. The future display account reads HTTP only; it needs no direct access to the status file or publisher credentials. This refines the earlier proposed direct-file access and system-service design. Login persistence, account selection and the kiosk supervisor still require an installation plan. A future operator can stop an authorized installation with `systemctl --user stop davellm-wallboard.service`; this sprint does not run it or any enable/start/reload command.

Dominic passed all 38 projection, publisher, HTTP and staging fixtures with Python 3.12, plus the real staged-server Chromium smoke check for rendering, CSP, theme switching, invalid-source warnings and zero external requests. `systemd-analyze --user verify` passed for the template without activation. The browser runs headlessly in an isolated container. No installed service, boot behavior, physical output, resource coexistence or human acceptance is claimed. Repository CI remains a separate delivery gate.

## Kiosk installation plan, October 30, 2026

Source preparation is ready for review. Actual installation is excluded from the current sprint. The next authorized installation session must resolve the following gates before changing accounts, packages, login configuration, units or display ownership.

1. **Runtime:** prefer the distribution's Cage package with a supported Chromium browser using native Wayland. [Ubuntu Noble packages Cage](https://packages.ubuntu.com/noble/cage); its current target-host candidate is `0.1.5+20240127-2build1`. The target's `chromium-browser` package is a transitional package for Chromium Snap, not a standalone browser binary. Qualify Snap confinement, browser update policy and required interfaces against the dedicated session before selecting the final launcher. Do not substitute the test container's browser as an installed runtime.
2. **Capacity:** refresh package metadata and obtain package plus browser/base-snap download and installed-size estimates only in the installation session. Reserve room for updates and rollback as well as the initial install. Current target capacity is insufficiently qualified for this operation; no files or packages have been removed to make room. A simulated Cage dependency resolution establishes availability only, not installation success or browser compatibility.
3. **Accounts and boot:** keep the publisher-owned status private. Run its loopback user service under the same owner, then give the dedicated non-sudo kiosk account HTTP access only. Verify that the owner user manager starts at boot; existing linger state alone does not establish service startup. Preserve existing accounts, SSH access, services and credential boundaries.
4. **Display session:** [Cage's systemd boot guide](https://github.com/cage-kiosk/cage/wiki/Starting-Cage-on-boot-with-systemd) requires a logind session and virtual-terminal ownership. Adapt it only after inspecting the target seat and console owner. Its example replaces a getty and changes display-manager ownership, so copying it blindly could disrupt access. Do not enable it, alter PAM, switch a terminal or change the default target during source preparation. Kiosk supervision remains a distinct system-level component from the loopback user service.
5. **Installation and rollback:** capture exact package versions, selected unit names, account ownership, file hashes and prior enabled/active states. Stage and verify the bundle first. Refuse an existing unowned destination; preserve the previous verified bundle and status separately. After authorization, install the server first, verify loopback-only serving and refusal behavior, and activate the kiosk only after the server is accepted. Stop only the new named kiosk unit, then the named wallboard user unit if rollback is required; restore captured files and enabled states. Never stop unrelated graphical or monitoring services as a shortcut.
6. **Acceptance:** separately verify fresh/missing/stale/outage display behavior, effective 4K scale, theme persistence, exit and restart controls, boot, browser crash, display sleep/wake, service resource budgets and operator readability. Retain the prior snapshot throughout failures. Do not attach hourly publication until these installed checks pass. Source fixtures do not close any physical or human gate.

Read-only target inspection found no Cage or browser executable, no active display manager or graphical user session, and no installed acceptance evidence. The safe next action is installation preflight under explicit deployment and hardware-output authority, with browser distribution and capacity resolved first. Until then, continue other eligible project source work.
