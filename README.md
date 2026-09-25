# DaveLLM Desktop

DaveLLM is an Electron desktop client backed by a FastAPI router. The router discovers models from configured Ollama nodes, streams OpenAI-compatible chat responses, and persists conversations, projects, vector indexes, feedback, performance, and cost data on the router host.

## Documentation

| Document | Authority |
|---|---|
| [README.md](README.md) | Primary setup, configuration, feature, and validation guide. |
| [PROJECT_SPEC.md](PROJECT_SPEC.md) | Current product scope, functional requirements, architecture, security, limits, and acceptance criteria. |
| [INTEGRATION.md](INTEGRATION.md) | Authoritative frontend/backend flow, authentication, and API contracts. |
| [CLAUDE.md](CLAUDE.md) | Maintainer architecture, trust boundaries, and repository constraints. |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Placeholder-only launch, health, inventory, persistence, and troubleshooting runbook. |
| [DaveHarness boundary and versioning decision](docs/decisions/0001-daveharness-boundary-and-versioning.md) | Implemented product ownership, package seam, SemVer authority, and revisit triggers. |
| [DaveHarness implementation plan](docs/DAVEHARNESS_IMPLEMENTATION_PLAN.md) | Authoritative 60-action roadmap through shipped `0.2.0` execution semantics, `0.3.0` leaf contracts, `0.4.0` policy and budgets, `0.5.0` run state, `0.6.0` cancellation/deadlines, `0.7.0` events, `0.8.0` facade, and `0.9.0` DaveLLM integration toward a qualified `1.0.0` contract. |
| [dave-llm-feature-analysis-2026-03-25.md](dave-llm-feature-analysis-2026-03-25.md) | Dated feature-status analysis with explicit verification boundaries. |
| [docs/EXECUTABLE_PROMPT_SERIES.md](docs/EXECUTABLE_PROMPT_SERIES.md) | P0 through P8 decision, plan, implementation, and review contracts. |
| [Public landing page](https://davehomeassist.github.io/DaveLLM/) | Published product overview and quickstart; not the desktop runtime static root. |

## Requirements

- Python 3
- Node.js and npm
- One or more reachable Ollama nodes
- `DAVE_API_KEY` set to a non-empty local secret

The repository does not include models, Whisper assets, credentials, or runtime data. On macOS, install the optional local dictation runtime after the normal setup:

```bash
npm run install:whisper:macos
```

This installs Homebrew `whisper-cpp` when needed and downloads the checksum-verified `tiny.en` model to `~/Library/Application Support/DaveLLM/models/`. The model remains app-managed runtime data and is never committed.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
npm ci
```

Configure the process environment before starting. Values below are placeholders, not working cluster inventory:

```bash
export DAVE_API_KEY='<local-secret>'
export DAVE_NODES='[{"id":"<node-id>","name":"<display-name>","url":"http://<ollama-host>:11434"}]'
npm start
```

Electron starts uvicorn, waits up to 15 seconds for public `/health`, injects `X-API-Key` only into requests to its exact loopback backend origin, and then loads the UI. The default Python is the repository virtual environment (`venv/bin/python` on macOS and Linux, `venv\Scripts\python.exe` on Windows). Override the Python executable with `DAVE_PYTHON`, the port with `DAVE_PORT`, or the readiness timeout with `DAVE_STARTUP_TIMEOUT_MS`.

### One-click macOS launcher

Install a Keychain-backed launcher after completing the normal Python and npm setup:

```bash
npm run install:macos
```

The installer creates a strong `DAVE_API_KEY` in macOS Keychain when the `com.davellm.api-key` item does not already exist, preserves an existing item, and installs `~/Applications/DaveLLM Launcher.app`. The launcher requires the Tailscale peers named `dominic` and `walter`, then adds `duncan` only when that peer is online and its Ollama `/api/tags` endpoint responds within four seconds. An unavailable Duncan is logged and skipped without blocking startup. The launcher constructs `DAVE_NODES` in memory, uses `~/Library/Application Support/DaveLLM` for persistence, and starts the existing Electron application. It does not write the key, live node addresses, or generated node JSON to the repository.

Double-click **DaveLLM Launcher** in `~/Applications` for subsequent launches. Startup failures are written to `~/Library/Logs/DaveLLM/launcher.log`. Stop any browser-mode process already using TCP port `8000` before launching the desktop app.

The launcher automatically discovers Homebrew `whisper-cli`. Local dictation uses the model under `DAVE_DATA_DIR/models/ggml-tiny.en.bin`; override either path with `DAVE_WHISPER_BIN` or `DAVE_WHISPER_MODEL`.

## Browser mode

```bash
source venv/bin/activate
export DAVE_API_KEY='<local-secret>'
export DAVE_NODES='[...]'
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Browser mode prompts for the key and keeps it in `sessionStorage`; it is not written to persistent `localStorage`. `/health` is public. Protected endpoints fail with `503` when the server key is unset and `401` when the supplied key is missing or wrong.

## Ollama contract

The UI loads nodes from `GET /nodes`, then loads the selected node's inventory from `GET /nodes/{node_id}/models`. The router queries Ollama `GET /api/tags`. Chat requests are accepted only when `node_id` is a configured node and `model` appears in the loaded inventory for that node. Inference uses Ollama's OpenAI-compatible `POST /v1/chat/completions` endpoint.

The repository intentionally contains no real node addresses or verified model inventory. Cluster reachability and installed models remain runtime-dependent.

### When no models load

`GET /nodes/{node_id}/models` and `GET /nodes/status` both return an `error` field so an empty inventory explains itself instead of looking like an empty cluster:

- `error: null` with an empty `models` list means the node answered `/api/tags` and has nothing pulled. Run `ollama pull <model>` on that node.
- `error` set means the node was not reached. The message names the cause: connection refused, an HTTP status, or a timeout.
- A node that answers `curl http://127.0.0.1:11434/api/tags` locally but refuses the router is bound to loopback only. Start it with `OLLAMA_HOST=0.0.0.0:11434`.
- A malformed `DAVE_NODES` value registers zero nodes. The router now prints the parse error and the expected JSON shape at startup instead of failing silently.

`DAVE_NODE_TIMEOUT` sets the per-node `/api/tags` timeout in seconds and defaults to `10`. Raise it for nodes that are slow to answer while loading a large model.

## Data and tools

`DAVE_DATA_DIR` relocates all eight persistence files and the project-upload directory. When unset, the current working directory remains the default.

- `dave_conversations.json`
- `dave_projects.json`
- `dave_settings.json`
- `dave_project_context.db`
- `dave_vectors.db`
- `feedback.db`
- `performance.db`
- `cost_log.jsonl`
- `project_uploads/`

Tools are disabled by default. To enable them, set `DAVE_ENABLE_TOOLS=true` and provide `DAVE_TOOL_ROOTS` as a JSON array of absolute paths. File read, write, and append operations share the same containment check. `shell.exec` remains disabled unless `DAVE_ENABLE_SHELL_TOOL=true` is also set. `DAVE_ENABLE_EXTENDED_TOOLS=true` is the opt-in for the extended tool set; it is honored only when `DAVE_ENABLE_TOOLS=true` and currently adds no tools. `web.fetch` accepts only bounded public HTTP/HTTPS responses and validates DNS plus each redirect target.

The tool registry publishes one JSON schema per active tool. `POST /tools/agent/run` sends the current schemas to Ollama on every bounded model step, validates arguments, logs call and result timing, returns the complete transcript, stops after eight steps by default, and pauses before tools marked as requiring approval. The pending response includes a `run_id` plus exact-call metadata. `POST /tools/agent/resume` accepts that run ID, call ID, SHA-256 argument digest, and an `approve` or `deny` decision for up to 300 seconds. Approval executes the stored canonical arguments without replaying the paused model step; denial records an operator-denied tool result and continues.

The generic registry and bounded-loop implementation lives in the in-process `daveharness` package at version `1.0.0-rc.1`. DaveLLM imports that public API and retains concrete tools, authentication, Ollama transport, persistence, and HTTP routes in `app.py`. Root `tool_executor.py` and `daveharness/executor.py` remain compatibility re-exports; new code should import `daveharness`.

DaveHarness `0.3.0` adds `CONTRACT_VERSION = 1` and the separate `encode_contract`/`decode_contract` envelope for `ParsedToolCall`, `PendingCall`, `ToolExecution`, and `ExecutorOutcome`. The envelope has exactly `contract_version`, `contract_type`, and `payload`; canonical JSON uses sorted keys, compact separators, and UTF-8 without ASCII escaping. Unknown versions, types, fields, invalid timestamps, non-finite numbers, and non-JSON nested values are rejected. DaveHarness `0.4.0` adds injected policy decisions, definition fingerprints, and immutable budgets. DaveHarness `0.5.0` adds versioned `RunSnapshot`, exact `ApprovalDecision`, and compare-and-swap `resume_run`/`decide_run` operations; see [H3 state contract](docs/DAVEHARNESS_H3_RUN_STATE.md). DaveHarness `0.6.0` adds [cancellation and deadline contracts](docs/DAVEHARNESS_H4_CANCELLATION.md). DaveHarness `0.7.0` adds [metadata-only lifecycle events](docs/DAVEHARNESS_H5_EVENTS.md). DaveHarness `0.8.0` adds the [bounded run store and instance-owned facade](docs/DAVEHARNESS_H6_FACADE.md). DaveHarness `0.9.0` adds the [DaveLLM lifecycle integration](docs/DAVEHARNESS_H7_INTEGRATION.md). The legacy route retains its original step and error limits. `ExecutorOutcome.to_dict()` and legacy `/tools/agent/*` responses keep their existing fields.

Tool handlers are synchronous by default. Coroutine handlers require both `async_handler=True` and a host-supplied name allowlist; DaveLLM currently permits only `web.fetch`. Definitions declare `cancellation` as `bounded` or `abandon`, and write or approval-required tools cannot use `abandon`. Existing tool status strings are unchanged. The additive `termination` field is `completed`, `deadline_abandoned`, `denied`, or `error`; `deadline_abandoned` means the response deadline elapsed and the underlying synchronous worker may still be running. Pending calls live only in the current process and do not survive restart.

## Instructions, copy, and project notes

The Chat header opens a layered instruction editor. It shows the global default, attached project instructions, session override, precedence, live character and token estimates, and the exact effective system text. Saves apply to the next message without restarting the application. Session overrides and the editable runtime global default each have a one-action reset.

Completed user and assistant messages expose keyboard-reachable Copy and Add to notepad actions. Copy preserves raw message source. The plain-text notepad persists per project, autosaves, stays inside Chat, can accept a selection from either message role, and can send its full contents as one user message.

## Project Homepage and BRAIN

Project Home is the inspectable owner for exactly four request-context components: Project Instructions, File Context Uploads, Artifact History, and BRAIN. Its baseline budget is deterministic: 25 percent instructions, 25 percent BRAIN, 30 percent files, and 20 percent artifacts. Unused capacity rolls forward to BRAIN, then files, then artifacts; instructions and protected BRAIN text are rejected instead of silently truncated. Preview context assembles the exact next-request messages without calling a model.

BRAIN stores pinned facts, active work, and compactable recent context in `dave_project_context.db`. Threshold and explicit compaction create immutable revisions; duplicate, resolved, superseded, and raw tool-log lines can be removed while pinned and active tiers remain verbatim. Delete is recoverable for `DAVE_BRAIN_RECOVERY_DAYS`, after which the daily worker permanently clears old content and starts a fresh revision history.

The authenticated local CLI uses the running router and `DAVE_API_KEY`:

```bash
python scripts/project_context_cli.py show <project-id>
python scripts/project_context_cli.py pin <project-id> --text 'Decision: verify before release.'
python scripts/project_context_cli.py compact <project-id>
python scripts/project_context_cli.py revisions <project-id>
python scripts/project_context_cli.py restore <project-id> 2
```

Project-context configuration:

- `DAVE_MODEL_CONTEXT_DEFAULT` — fallback model window, default `32768`.
- `DAVE_MODEL_CONTEXT_WINDOWS` — JSON object of model IDs to context-window tokens.
- `DAVE_PROJECT_CONTEXT_TOKENS` — new-project context budget, default `16384`.
- `DAVE_BRAIN_COMPACT_TOKENS` — new-project compaction threshold, default `3072`.
- `DAVE_BRAIN_RECOVERY_DAYS` — soft-delete recovery window, default `30`.

The Project Homepage uses a vendored GSAP 3.15.0 core timeline for its precision-control-deck reveal and context-preview feedback. It animates transform and opacity only, switches directly to final states under `prefers-reduced-motion: reduce`, and performs no CDN request. The vendored notice is in `static/vendor/gsap/NOTICE.md`.

## Local suggestions and mobile navigation

The browser client derives at most three deterministic Suggested next actions from the current composer, attachment type, validated project/node/model selection, and response shape. Prediction generation lives in `static/anticipation.js`; it performs no network, DOM, or storage work, and suggestion chips never submit or change context without a visible user action. Valid last-used selections may be restored only on the empty startup state after inventory and node-health checks, with a visible status and immediate Undo. The client no longer calls `/route/decision` while sending a message, so the selected model remains under manual control.

Suggestion preferences use the versioned `davellm_anticipation_v1` local-storage record. Its schema is limited to stable IDs, booleans, capped counters, and timestamps; prompt/response text, attachment names or contents, credentials, node URLs, and system prompts are excluded. Unsent composer text and the active Chat/History/Runtime mobile tab remain session-only. Reset Suggestions removes only the versioned suggestion record.

## Validation

Install `requirements-dev.txt` in the project virtual environment before running these checks.

```bash
source venv/bin/activate
python -m py_compile app.py
python -m py_compile project_context.py scripts/project_context_cli.py
python -m py_compile tool_executor.py
python -m compileall -q daveharness
python -m mypy daveharness
python -m pytest -q
node --check static/app.js
node --check static/anticipation.js
node --check static/prompt-contract.js
node --check static/vendor/gsap/gsap.min.js
node --check desktop/main.js
node --check desktop/preload.js
bash -n deploy/check-cluster.sh scripts/verify-cluster.sh scripts/macos/install-launcher.sh scripts/macos/install-whisper-runtime.sh scripts/macos/launch-davellm.sh
npm ci
npm ls --depth=0
npm audit
git diff --check
```

Runtime UI files are under `static/`; only that directory is mounted at `/`. The separate `docs/` content is published through GitHub Pages and is not used by the desktop runtime.

## Dependency note

`package.json` pins Electron `^44.0.0`, upgraded on 2026-08-26 to resolve the prior high-severity advisory set. Keep Electron on a supported major and rerun `npm audit` after dependency changes.

DaveHarness `1.0.0-rc.1` adds H8 offline qualification, bounded JSON admission, and Python 3.12–3.14 CI. See [H8 qualification](docs/DAVEHARNESS_H8_QUALIFICATION.md). This candidate does not establish live-model qualification or human acceptance. DaveLLM remains `2.1.0`.

H9 action 55 adds the authorized live-evaluation runner `scripts/evaluate_live_daveharness.py`, which runs DaveLLM's Ollama adapter and file/system tools inside disposable roots and reports the action 56 thresholds per model. See [H9 live evaluation](docs/DAVEHARNESS_H9_LIVE_EVALUATION.md). No target model is qualified yet, and DaveHarness remains `1.0.0-rc.1`.
