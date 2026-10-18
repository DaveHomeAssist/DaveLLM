# DL-OPS-01 — On-call monitor

## Outcome

Make the DaveLLM Cluster view the durable home for hourly backend and inference-node checks. Routine healthy checks update the interface instead of producing conversational noise; outages, failed recovery, and required operator action still escalate through the automation thread.

## Product contract

- The existing hourly technician records one structured snapshot after every run.
- The snapshot names local, Dominic, Walter, and Duncan; preserves endpoint, health, HTTP status, and measured complete-response latency; and records recovery details when applicable.
- A bounded local history survives backend and UI restarts. It contains operational metadata only, never credentials, prompts, or model responses.
- The authenticated Cluster view shows freshness, overall state, target rows, the most recent recovery note, and recent run history.
- Missing, malformed, or stale data is shown explicitly. It is never rendered as healthy.
- The automation stays quiet in chat for unchanged healthy runs and escalates incidents as before.

## Implementation

- `scripts/record_monitor_status.py` validates command-line records and atomically writes `on_call_status.json` under `DAVE_DATA_DIR` (the installed Mac default is `~/Library/Application Support/DaveLLM`). History is capped at 168 hourly checks.
- `GET /monitoring/on-call` reads that bounded file behind the existing API-key guard and returns a safe empty state when no run has published yet.
- The existing Cluster view renders the record with DOM text nodes only and refreshes it with the other cluster data.

## Acceptance

1. Recorder rejects malformed status, latency, HTTP status, and target records without changing the last good file.
2. Successful writes are atomic, retain at most 168 entries, and round-trip through the authenticated API.
3. The Cluster view has styled current, empty, stale, recovery, and failure states at desktop and mobile widths.
4. Keyboard and screen-reader semantics remain intact; no new dependency or browser-persisted operational data is added.
5. The hourly automation is updated only after installed source activation so it never targets an unavailable recorder.

## Evidence boundaries

Source tests and browser fixtures do not prove the installed scheduler writes the file. Installed activation, one live hourly-style write, UI readback, and the automation prompt update are separate gates.
