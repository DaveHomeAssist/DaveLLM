# DaveLLM Desktop

DaveLLM is an Electron desktop client backed by a FastAPI router. The router discovers models from configured Ollama nodes, streams chat responses from Ollama's native chat API, and persists conversations, projects, vector indexes, feedback, performance, and cost data on the router host.

## Documentation

| Document | Authority |
|---|---|
| [README.md](README.md) | Primary setup, configuration, feature, and validation guide. |
| [PROJECT_SPEC.md](PROJECT_SPEC.md) | Current product scope, functional requirements, architecture, security, limits, and acceptance criteria. |
| [INTEGRATION.md](INTEGRATION.md) | Authoritative frontend/backend flow, authentication, and API contracts. |
| [CLAUDE.md](CLAUDE.md) | Maintainer architecture, trust boundaries, and repository constraints. |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Placeholder-only launch, health, inventory, persistence, and troubleshooting runbook. |
| [docs/DAVELLM_TOOLS.md](docs/DAVELLM_TOOLS.md) | Tool settings, the qualified tools, and the extended tools' arguments, limits, path rules, and approved edits. |
| [docs/TOOLS100.md](docs/TOOLS100.md) | Opt-in 100-tool expansion, named configuration, runner contract, approvals and acceptance boundaries. |
| [docs/DAVELLM_GIT_SECURITY_REVIEW.md](docs/DAVELLM_GIT_SECURITY_REVIEW.md) | Independent security review of the read-only Git tools, its findings, and their fixes. |
| [docs/DAVEHARNESS_CAPABILITIES.md](docs/DAVEHARNESS_CAPABILITIES.md) | Generated capabilities manifest: every tool, flag, run budget, host limit, `/tools` route, and tool-module constant, with a JSON twin for tooling. |
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

The installer creates a strong `DAVE_API_KEY` in macOS Keychain when the `com.davellm.api-key` item does not already exist, preserves an existing item, and installs `~/Applications/DaveLLM Launcher.app`. The launcher requires the Tailscale peers named `dominic` and `walter`, then adds `duncan` only when that peer is online and its Ollama `/api/tags` endpoint responds within four seconds. An unavailable Duncan is logged and skipped without blocking startup. The launcher constructs `DAVE_NODES` in memory, with a capability profile for each node (DL-ROUTE-01, below), uses `~/Library/Application Support/DaveLLM` for persistence, and starts the existing Electron application. It also sets `DAVE_ENABLE_TOOLS=true` and `DAVE_ENABLE_EXTENDED_TOOLS=true` so the Run tools button works, and sets `DAVE_SEARCH_URL` when a SearXNG instance on Dominic (port `8890`, or `DAVE_SEARCH_PORT`) answers its health check; export either flag as `false` before launching to turn it off. It does not enable `shell.exec` or set `DAVE_TOOL_ROOTS`, so file tools stay contained until you configure roots. It does not write the key, live node addresses, or generated node JSON to the repository.

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

The UI loads nodes from `GET /nodes`, then loads the selected node's inventory from `GET /nodes/{node_id}/models`. The router queries Ollama `GET /api/tags`. Chat requests are accepted only when `node_id` is a configured node and `model` appears in the loaded inventory for that node. Plain chat (`POST /chat`, `POST /chat/stream`, and conversation-summary generation) uses Ollama's native `POST /api/chat` through the shared helper in `davellm_ollama.py`, which sends the router's own `num_ctx` (the model's window capped at `DAVE_CHAT_NUM_CTX`, default `16384`) and `keep_alive` (`DAVE_CHAT_KEEP_ALIVE`, default `30m`, for chats in a conversation), so a node's own default context no longer decides plain chat. The bounded tool loop (`POST /tools/agent/run` and the lifecycle run routes) also uses native `POST /api/chat` (DL-TRANSPORT-01b): it sends the tool schemas, the same `num_ctx` and `keep_alive`, and a byte-capped read; DaveHarness reads the native reply directly, and only the tool-call arguments and tool-result names it writes back are converted. Ollama's OpenAI-compatible endpoint ignores `num_ctx` and `keep_alive` (verified on Ollama 0.33.3). Sampling is unchanged: `/v1` sent `top_p` 1.0, so the native payload sends `top_p` 1.0 too instead of the model's Modelfile default. Request-field mapping is in [INTEGRATION.md](INTEGRATION.md).

A `DAVE_NODES` entry can carry an optional capability profile (DL-ROUTE-01): `"profile": {"compute": "cpu", "prompt_token_limit": 1300, "model_prompt_token_limits": {"gpt-oss:120b": 2500}}`. `prompt_token_limit` is the prompt size, in estimated tokens for everything sent (system prompt, project context, history and the new message), that the node reads in reasonable time. A per-model entry overrides it. `GET /nodes` returns the profile, and the node picker labels a CPU-only node "(CPU, slower)". When a streamed prompt is over its node's limit, the reply bubble says so while it waits (DL-ROUTE-02). The bubble also says whether the model is loading into memory or already loaded (DL-ROUTE-03, from the node's `/api/ps`), and whether the node is busy with other replies (DL-ROUTE-04). The limits are advisory: the request still goes to the node and model you picked. The launcher sets Dominic (CPU) to 1,300 tokens and gpt-oss:120b on Duncan to 2,500, from the 2026-09-25 benchmarks. Every prompt carries the ~900-token default system prompt, so on Dominic most conversations show the warning. A malformed profile is ignored with a startup warning, and the node stays registered.

While a streamed reply is still silent, the chat shows what the model is doing: "Waiting for the model (loading it and reading your message)…", then "Thinking…" if it reasons first. When the reply finishes, a small line under it gives the speed, e.g. `58.2 tok/s · first token 1.9 s · 256 tokens · model load 1.5 s`. Model load is shown only when it took at least 1 s. These are the `status` and `stats` stream events (DL-UX-01) in [INTEGRATION.md](INTEGRATION.md). The speed line is not saved with the conversation.

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

Tools are disabled by default in the router; the macOS launcher turns them on. To enable them elsewhere, set `DAVE_ENABLE_TOOLS=true` and provide `DAVE_TOOL_ROOTS` as a JSON array of absolute paths. File read, write, and append operations share the same containment check. `shell.exec` remains disabled unless `DAVE_ENABLE_SHELL_TOOL=true` is also set. `DAVE_ENABLE_EXTENDED_TOOLS=true`, honored only when `DAVE_ENABLE_TOOLS=true`, adds the read-only `file.list`, `file.search`, `file.read_lines`, `md.outline`, `md.section`, `git.status`, `git.diff`, `git.log`, `git.show`, the run-scoped native tools `project.notepad.read`, `project.brain.read`, `project.artifacts`, `chat.search`, and `cluster.status`, and `file.edit`, which replaces exact text in an existing file only after you approve a before-and-after preview; the file tools hide protected paths such as `.env` files, keys, and SSH folders, and never follow symlinks out of a root. See [docs/DAVELLM_TOOLS.md](docs/DAVELLM_TOOLS.md) for their arguments and limits. `web.fetch` accepts only bounded public HTTP/HTTPS responses and validates DNS plus each redirect target.

With extended tools on, `web.search` searches the web through a self-hosted [SearXNG](https://docs.searxng.org/) instance named by `DAVE_SEARCH_URL` (its JSON format must be enabled), and `web.read` returns a public page as readable text. Together they let a Run tools request look something up and read the source. See [docs/DAVELLM_TOOLS.md](docs/DAVELLM_TOOLS.md#web-tools) and [docs/OPERATIONS.md](docs/OPERATIONS.md#web-search).

`DAVE_ENABLE_NOTION_TOOLS=true`, also honored only with `DAVE_ENABLE_TOOLS=true`, adds `notion.page.read`, `notion.page.append`, and `notion.block.update` for the pages named in `DAVE_NOTION_PAGES`, using DaveLLM's own internal Notion connection (`DAVE_NOTION_TOKEN`, environment only). Every write needs your approval, is checked against a fresh read before it is sent, and reports `verified`, `failed`, or `unknown`. See [docs/DAVELLM_TOOLS.md](docs/DAVELLM_TOOLS.md#notion-tools).

The tool registry publishes one JSON schema per active tool. `POST /tools/agent/run` sends the current schemas to Ollama on every bounded model step, validates arguments, logs call and result timing, returns the complete transcript, stops after eight steps by default, and pauses before tools marked as requiring approval. The pending response includes a `run_id` plus exact-call metadata. `POST /tools/agent/resume` accepts that run ID, call ID, SHA-256 argument digest, and an `approve` or `deny` decision for up to 300 seconds. Approval executes the stored canonical arguments without replaying the paused model step; denial records an operator-denied tool result and continues.

The generic registry and bounded-loop implementation lives in the in-process `daveharness` package at version `1.0.0-rc.1`. DaveLLM imports that public API and retains concrete tools, authentication, the native Ollama transport (`davellm_ollama.py`), persistence, and HTTP routes in `app.py`, with the native `/api/chat` transport in `davellm_ollama.py`. Root `tool_executor.py` and `daveharness/executor.py` remain compatibility re-exports; new code should import `daveharness`.

DaveHarness `0.3.0` adds `CONTRACT_VERSION = 1` and the separate `encode_contract`/`decode_contract` envelope for `ParsedToolCall`, `PendingCall`, `ToolExecution`, and `ExecutorOutcome`. The envelope has exactly `contract_version`, `contract_type`, and `payload`; canonical JSON uses sorted keys, compact separators, and UTF-8 without ASCII escaping. Unknown versions, types, fields, invalid timestamps, non-finite numbers, and non-JSON nested values are rejected. DaveHarness `0.4.0` adds injected policy decisions, definition fingerprints, and immutable budgets. DaveHarness `0.5.0` adds versioned `RunSnapshot`, exact `ApprovalDecision`, and compare-and-swap `resume_run`/`decide_run` operations; see [H3 state contract](docs/DAVEHARNESS_H3_RUN_STATE.md). DaveHarness `0.6.0` adds [cancellation and deadline contracts](docs/DAVEHARNESS_H4_CANCELLATION.md). DaveHarness `0.7.0` adds [metadata-only lifecycle events](docs/DAVEHARNESS_H5_EVENTS.md). DaveHarness `0.8.0` adds the [bounded run store and instance-owned facade](docs/DAVEHARNESS_H6_FACADE.md). DaveHarness `0.9.0` adds the [DaveLLM lifecycle integration](docs/DAVEHARNESS_H7_INTEGRATION.md). The legacy route retains its original step and error limits. `ExecutorOutcome.to_dict()` and legacy `/tools/agent/*` responses keep their existing fields.

Tool handlers are synchronous by default. Coroutine handlers require both `async_handler=True` and a host-supplied name allowlist; DaveLLM permits `web.fetch`, `web.search`, `web.read`, and the three Notion tools. Definitions declare `cancellation` as `bounded` or `abandon`, and write or approval-required tools cannot use `abandon`. Existing tool status strings are unchanged. The additive `termination` field is `completed`, `deadline_abandoned`, `denied`, or `error`; `deadline_abandoned` means the response deadline elapsed and the underlying synchronous worker may still be running. Pending calls live only in the current process and do not survive restart.

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
- `DAVE_MODEL_CONTEXT_WINDOWS` — JSON object of model IDs to context-window tokens. For plain chat the router sends each window, capped at `DAVE_CHAT_NUM_CTX`, to Ollama as `num_ctx`, and sizes plain chat's project-context budget (and the context preview) with the same capped number. Agent runs, which still use `/v1` and send no `num_ctx`, budget with the uncapped window.
- `DAVE_CHAT_NUM_CTX` — largest context the router requests for plain chat, default `16384`; values below `8192` are raised to `8192`, the smallest window that leaves room for project context after the output and safety reserves.
- `DAVE_CHAT_KEEP_ALIVE` — how long Ollama keeps a model loaded after a chat in a conversation, default `30m`; empty uses the server default.
- `DAVE_NODE_CONNECT_TIMEOUT` — seconds to connect to a node before giving up, default `10`.
- `DAVE_NODE_FIRST_CHUNK_TIMEOUT` — seconds a streamed chat may wait for its first line (model load plus prompt reading), default `300`.
- `DAVE_NODE_IDLE_TIMEOUT` — seconds a streamed chat may wait between lines once tokens flow, default `120`.
- `DAVE_NODE_TOTAL_TIMEOUT` — seconds for a whole non-streaming reply (`/chat` and each tool-loop model turn), default `600`.
- `DAVE_PROJECT_CONTEXT_TOKENS` — new-project context budget, default `16384`.
- `DAVE_BRAIN_COMPACT_TOKENS` — new-project compaction threshold, default `3072`.
- `DAVE_BRAIN_RECOVERY_DAYS` — soft-delete recovery window, default `30`.

The Project Homepage uses a vendored GSAP 3.15.0 core timeline for its precision-control-deck reveal and context-preview feedback. It animates transform and opacity only, switches directly to final states under `prefers-reduced-motion: reduce`, and performs no CDN request. The vendored notice is in `static/vendor/gsap/NOTICE.md`.

## Console navigation and local suggestions

The runtime console uses Chat, Projects, Cluster, and Settings in a desktop side rail or mobile bottom rail. Light is the initial theme; the visible theme toggle preserves the chosen light/dark mode. Conversation history keeps colored age groups: green under 24 hours, amber 1–3 days, red 3–7 days, and gray over 7 days. History and the Context/Run/Notepad inspector can be opened independently on desktop and as alternate panels on narrow screens. Cluster reports real node status and router metrics; unavailable data is explicitly labeled. `monitoring.html` now opens that same Cluster view.

Project attachment uses an explicit dialog and the existing backend update route. Context previews report the actual backend budget, including carried-forward unused capacity. Approval decisions retain the exact pending-call credentials; the composer remains editable while sending is disabled until the run ends. Model output is rendered using text nodes and a small Markdown subset, never raw HTML.

The browser client derives at most three deterministic Suggested next actions from the current composer, attachment type, validated project/node/model selection, and response shape. Prediction generation lives in `static/anticipation.js`; it performs no network, DOM, or storage work, and suggestion chips never submit or change context without a visible user action. Valid last-used selections may be restored only on the empty startup state after inventory and node-health checks, with a visible status and immediate Undo. The client no longer calls `/route/decision` while sending a message, so the selected model remains under manual control.

Suggestion preferences use the versioned `davellm_anticipation_v1` local-storage record. Its schema is limited to stable IDs, booleans, capped counters, and timestamps; prompt/response text, attachment names or contents, credentials, node URLs, and system prompts are excluded. Unsent composer text remains session-only; navigation state is not persisted. Reset Suggestions removes only the versioned suggestion record.

## Validation

Install `requirements-dev.txt` in the project virtual environment before running these checks.

```bash
source venv/bin/activate
python -m py_compile app.py
python -m py_compile project_context.py scripts/project_context_cli.py
python -m py_compile tool_executor.py
python -m py_compile davellm_ollama.py
python -m py_compile davellm_web.py
python -m compileall -q daveharness
python -m mypy daveharness
python -m pytest -q
node --check static/app.js
node --check static/console.js
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

Runtime UI files are under `static/`; only that directory is mounted at `/`, with `Cache-Control: no-cache` so browsers revalidate the bundle on every load. The separate `docs/` content is published through GitHub Pages and is not used by the desktop runtime.

## Dependency note

`package.json` pins Electron `^44.0.0`, upgraded on 2026-08-26 to resolve the prior high-severity advisory set. Keep Electron on a supported major and rerun `npm audit` after dependency changes.

DaveHarness `1.0.0-rc.1` adds H8 offline qualification, bounded JSON admission, and Python 3.12–3.14 CI. See [H8 qualification](docs/DAVEHARNESS_H8_QUALIFICATION.md). This candidate does not establish live-model qualification or human acceptance. DaveLLM remains `2.1.0`.

An approval-required tool can declare an optional [approval preflight](docs/DAVEHARNESS_PREFLIGHT.md) that refuses a call certain to fail before the run pauses for approval, as an ordinary tool error with termination `denied`. `file.edit` uses it to check `old_text` against the file as it is now. A preflight never authorizes an effect; the handler checks everything again after approval.

H9 action 55 adds the authorized live-evaluation runner `scripts/evaluate_live_daveharness.py`, which runs DaveLLM's Ollama adapter and file/system tools inside disposable roots and reports the action 56 thresholds per model. See [H9 live evaluation](docs/DAVEHARNESS_H9_LIVE_EVALUATION.md). No target model is qualified yet, and DaveHarness remains `1.0.0-rc.1`.

### Optional console browser acceptance

`tests/browser_console.cjs` exercises seven viewport widths, themes, colored age groups, safe Markdown, runtime selection, cluster state, project creation, approval rejection, streaming, and credential cancellation. It needs Playwright and a local Chrome binary from the developer environment; neither is a runtime dependency. Run it only against a disposable router with `DAVE_API_KEY=console-fixture-key`, an empty node list, tools enabled, and a temporary `DAVE_DATA_DIR`. Set `CONSOLE_URL` (default `http://127.0.0.1:8769`) and optionally `CHROME_BIN` and `CONSOLE_SCREENSHOTS`, then run `node tests/browser_console.cjs`. Model and run responses are fixtures; project creation uses the disposable backend. These tests do not qualify live model inference or physical phone keyboard behavior.
