# DaveLLM Project Specification

| Field | Current value |
|---|---|
| Product | DaveLLM |
| Status | Implemented local application; active development |
| Owner and primary operator | Dave Robertson |
| Canonical repository | `DaveHomeAssist/DaveLLM` |
| Application version | Router API `2.1`; desktop package `1.0.0` |
| Specification date | 2026-09-02 |
| Implementation baseline reviewed | `845e7db` (`main`, before this specification-only change) |
| Primary platforms | macOS Electron desktop; loopback browser mode |
| Inference provider | Operator-configured Ollama nodes |

This document is the current product and engineering contract for the shipped DaveLLM repository. It describes implemented behavior, required boundaries, and known limits. Dated analyses and the P0-P8 prompt ledger remain historical evidence; when they disagree with this specification or the current source, current source wins and this document must be updated.

## 1. Product definition

DaveLLM is Dave's private, local-first AI workbench. It combines an Electron chat client with a FastAPI control plane that discovers models on configured Ollama nodes, assembles inspectable project context, streams responses, preserves working history, and optionally brokers bounded tools.

The product is intended for one high-bandwidth technical operator moving between software, local AI, networking, AV, lighting, home automation, and business work. The core experience must make model choice, instructions, project context, generated artifacts, and failures visible instead of hiding them behind automatic behavior.

### Product goals

1. Provide a dependable desktop entry point to local models across multiple nodes.
2. Keep the operator in control of node, model, prompt, instructions, context, and tool approval.
3. Preserve useful project knowledge across conversations without replaying an entire transcript.
4. Keep credentials, node addresses, prompts, uploads, and runtime data local and out of source control.
5. Fail explicitly when authentication, inventory, models, dictation assets, tools, or nodes are unavailable.
6. Remain operable in browser mode for diagnosis while treating Electron as the primary client.

### Non-goals

- Hosting a public or multi-tenant AI service.
- Bundling Ollama models, Whisper assets, live node addresses, or credentials.
- Replacing Ollama's model lifecycle or cluster administration.
- Automatically overriding the operator's selected model during normal chat.
- Providing unrestricted filesystem or shell access.
- Treating the GitHub Pages documentation site as the application runtime.
- Providing rich-text, collaborative, or versioned notepad editing.

## 2. Product boundary

The current repository owns both the user-facing DaveLLM application and its optional bounded agent executor. The proposed separation between **DaveLLM** as the workbench and **DaveHarness** as an independent execution product has not been completed. Until that decision is implemented, the agent loop and built-in tools remain features of this repository and must obey its authentication, approval, timeout, and containment rules.

The application is standalone. It is not an Open WebUI fork, wrapper, or plugin.

## 3. Primary user journeys

### 3.1 Launch and select a model

1. The macOS launcher retrieves the local API key from Keychain.
2. It resolves required Tailscale peers and constructs `DAVE_NODES` in process memory.
3. Electron starts FastAPI on loopback and waits for `GET /health`.
4. The UI loads configured nodes, node health, and each selected node's live Ollama inventory.
5. Dave explicitly selects a node and one model returned for that node.

### 3.2 Start or continue a conversation

1. Dave creates a General conversation or attaches it to a project.
2. He enters text, adds a supported attachment, or records local dictation.
3. The UI shows the active project, node, and model before send.
4. FastAPI validates the selected node and model, assembles the effective request, and streams the response.
5. The conversation, performance metadata, and eligible project artifact are persisted locally.

### 3.3 Work with project context

1. Dave opens Project Home.
2. He manages Project Instructions, File Context Uploads, Artifact History, and BRAIN independently.
3. He can preview the exact next-request message assembly without sending or calling a model.
4. Context is bounded deterministically to the configured project and model budgets.
5. Reattaching an existing conversation changes future requests only and records a visible context event.

### 3.4 Inspect and change instructions

1. Dave opens the Instructions dialog from Chat.
2. The dialog exposes global, project, and session layers, their precedence, size estimates, and the exact effective system text.
3. Saving affects the next message without restarting DaveLLM.
4. Global and session reset actions restore their defined defaults.

### 3.5 Use local tools

1. The operator explicitly enables tools in the server environment.
2. The agent receives only schemas for the current runtime registry.
3. FastAPI validates every tool name and argument set.
4. File mutations and shell execution pause unless approved for that run.
5. The loop ends with an answer, approval request, model/tool failure, error budget, or step ceiling and always returns its partial transcript.

## 4. Functional requirements

| ID | Requirement | Current contract |
|---|---|---|
| FR-01 | Local runtime | Electron starts uvicorn on `127.0.0.1`; browser mode may start the same router manually. |
| FR-02 | Authentication | `GET /health` is public. Every operational route requires `X-API-Key`; an unset server key returns `503`, and a missing or wrong caller key returns `401`. |
| FR-03 | Node configuration | `DAVE_NODES` is the only live inventory override. Malformed JSON registers zero nodes and emits a startup explanation. Source placeholders are never evidence of a working node. |
| FR-04 | Model discovery | The router reads Ollama `GET /api/tags`, publishes the exact model IDs returned, and retains the last good inventory across a transient refresh failure. |
| FR-05 | Model validation | Chat rejects an unknown node, an inventory not yet loaded, or a model absent from that node's loaded inventory. |
| FR-06 | Chat transport | The router supports complete and server-sent-event streaming chat through Ollama's OpenAI-compatible `POST /v1/chat/completions`. |
| FR-07 | Manual control | Normal chat uses the visible node and model selection. Local suggestions may propose actions but cannot silently send or change context. |
| FR-08 | Conversation lifecycle | The app lists, loads, creates, renames, clears, deletes, searches, exports, and project-attaches conversations. |
| FR-09 | Templates | New conversations may use `general`, `code_review`, or `brainstorm`; template CRUD is not provided. |
| FR-10 | Layered instructions | Global, project, and session layers resolve into one exact primary system message in global-to-project-to-session order, with later layers taking precedence. |
| FR-11 | Instruction inspection | The UI shows editable available layers, precedence, character/token estimates, and a scrollable exact-effective preview. |
| FR-12 | Project Home | Each project owns Instructions, File Context Uploads, Artifact History, and BRAIN as separate components with independent lifecycle controls. |
| FR-13 | Context preview | An authenticated preview returns exact next-request messages and budget use without calling a model or mutating the conversation. |
| FR-14 | File context | Files are bounded to 10 MB. Only attached, successfully indexed UTF-8 text chunks are eligible for request context. |
| FR-15 | Artifacts | Eligible assistant outputs from attached chats are captured; artifacts may be inspected, pinned, archived, edited, or deleted. |
| FR-16 | BRAIN | BRAIN stores pinned, active, and compactable recent text with optimistic revisions, explicit/threshold compaction, immutable snapshots, restore, and recoverable delete. |
| FR-17 | Notepad | Every project has a bounded plain-text notepad with autosave, message-to-notepad, and send-full-note behavior. |
| FR-18 | Attachments | Chat accepts bounded text and image inputs; image payloads are limited to 5 MB of base64 data. |
| FR-19 | Dictation | Audio uploads and recorded microphone blobs are sent as multipart audio to local Whisper; the request must carry the same authenticated transport as other protected routes. |
| FR-20 | Copy actions | Completed user and assistant messages provide keyboard-reachable raw-source Copy and Add to notepad actions. |
| FR-21 | Suggestions | The client shows no more than three deterministic next actions. Prediction performs no network, DOM, or storage work. |
| FR-22 | Monitoring | The app exposes authenticated health, cost analytics, feedback, and recent operational error data. |
| FR-23 | Tool catalog | Tools default off. When enabled, active schemas and permission metadata come from the runtime registry. |
| FR-24 | Agent execution | Model-selected tools run through schema validation, per-tool timeouts, an error budget, an eight-step default ceiling, approval boundaries, and a complete partial transcript. |
| FR-25 | Responsive access | Chat, History, and Runtime navigation remains usable at mobile widths; motion respects `prefers-reduced-motion`. |

## 5. Context and prompt contract

### Instruction precedence

The primary system message is assembled in this order:

1. Runtime global default.
2. Attached project instructions.
3. Conversation session override.

The exact merged result must be available in the Instructions dialog and must match the next model request. Legacy conversation snapshots stay in replace mode until the operator saves or resets them.

### Project request assembly

After reserving output, a five-percent safety margin, the current user input, and bounded conversation history, the remaining project budget is allocated as follows:

| Component | Baseline share | Overflow behavior |
|---|---:|---|
| Project Instructions | 25% | Protected; reject overflow rather than silently truncate. |
| BRAIN | 25% | Protected pinned/active content; compact eligible recent content. |
| File Context Uploads | 30% | Rank and bound eligible text chunks. |
| Artifact History | 20% | Rank and bound eligible artifacts, favoring pinned content. |

Unused capacity rolls forward to BRAIN, then files, then artifacts. It never borrows the model output reserve or safety margin.

The request order is: exact primary system message, BRAIN, file context, artifact history, bounded conversation history, then the current user message. Evidence layers cannot override instructions.

### BRAIN retention

- **Pinned:** facts and decisions that survive verbatim.
- **Active:** current goals, constraints, open work, and open risks.
- **Recent:** working context eligible for deterministic compaction.
- **Discardable during compaction:** duplicates and lines marked resolved, superseded, or as raw tool traffic.

Compaction writes a recoverable immutable revision before promotion. Soft-deleted BRAIN content remains recoverable for the configured retention window, 30 days by default.

## 6. User interface surfaces

| Surface | Responsibility |
|---|---|
| Chat | Composer, streaming transcript, attachments, dictation, instruction access, copy/notepad actions, and visible node/model/project state. |
| History | Conversation discovery and lifecycle actions. |
| Runtime | Node health, live model inventory, monitoring, and operational status. |
| Project Home | Project metadata and the four request-context components. |
| Instructions dialog | Layer editing, reset actions, precedence explanation, counts, and exact-effective preview. |
| Notepad panel | Project-scoped plain-text scratchpad and send action. |

The visual system uses local assets, including vendored Lucide icons and GSAP. The runtime must not depend on a CDN. Controls must remain keyboard reachable, labelled, responsive, and compatible with reduced-motion preference.

## 7. System architecture

```text
Dave
  |
  v
Electron renderer or loopback browser
  |  X-API-Key on protected requests
  v
FastAPI router on 127.0.0.1
  |-- JSON and SQLite persistence under DAVE_DATA_DIR
  |-- Project context allocator and BRAIN revision store
  |-- Optional schema-validated tool executor
  |-- Local ffmpeg + whisper-cli transcription
  |
  +---- Ollama /api/tags              (inventory)
  +---- Ollama /v1/chat/completions   (inference)
           on DAVE_NODES
```

### Runtime components

| Component | Source | Responsibility |
|---|---|---|
| FastAPI router | `app.py` | Auth, routes, Ollama transport, persistence integration, dictation, monitoring, routing advice, and tool registration. |
| Project context store | `project_context.py` | Normalized project components, quotas, retrieval, BRAIN revisions, and exact request assembly. |
| Tool executor | `tool_executor.py` | Registry, schemas, validation, approvals, timing, error handling, and bounded model/tool loop. |
| Runtime client | `static/` | Chat and project UI served by FastAPI. |
| Desktop shell | `desktop/` | Backend lifecycle, exact-origin API-key injection, and isolated preload bridge. |
| macOS launcher | `scripts/macos/` | Keychain, Tailscale node discovery, data-root selection, optional Duncan check, and application launch. |
| Operations CLI | `scripts/project_context_cli.py` | Authenticated BRAIN inspection, pinning, compaction, revisions, and restore. |
| Public documentation | `docs/` | GitHub Pages artifact only; never the runtime static root. |

### API families

There is no `/api` prefix.

- Readiness and inventory: `/health`, `/nodes`, `/nodes/status`, `/nodes/{node_id}/models`
- Chat and routing: `/chat`, `/chat/stream`, `/route/decision`, `/route/decision/cascade`
- Conversations and instructions: `/conversations/...`, `/instructions/global`
- Projects and context: `/projects/...`, including homepage, preview, files, artifacts, BRAIN, and notepad
- Local transcription: `/audio/transcribe`
- Tools and agent loop: `/tools`, `/tools/execute`, `/tools/agent/run`
- Operations: `/feedback`, `/analytics/costs`, `/monitoring/health`, `/search`

Routing-decision endpoints remain available for explicit advisory use. The normal chat send path does not call them and does not replace a manual model selection.

## 8. Data and persistence

All runtime paths resolve beneath `DAVE_DATA_DIR`; when unset, they resolve beneath the process working directory.

| Artifact | Purpose |
|---|---|
| `dave_conversations.json` | Conversation messages and metadata. |
| `dave_projects.json` | Core project metadata and plain-text notepads. |
| `dave_settings.json` | Editable runtime settings, including the global instruction layer. |
| `dave_project_context.db` | Project profiles, files/chunks, artifacts, and BRAIN state/revisions. |
| `dave_vectors.db` | Conversation embedding index. |
| `feedback.db` | Model feedback records. |
| `performance.db` | Cost, token, latency, and model performance records. |
| `cost_log.jsonl` | Append-only cost events. |
| `project_uploads/` | Project-owned uploaded file bodies. |

The browser may persist only the versioned, allowlisted suggestion preferences in `localStorage`. The API key, unsent composer, mobile tab, and other session state must remain session-only. Prompt/response content, attachment names or bodies, node URLs, credentials, and system prompts are forbidden from the suggestion record.

Conversation JSON and core project metadata remain compatibility stores. A full conversation migration to SQLite is not part of the current implementation.

## 9. Security and privacy requirements

1. FastAPI must bind to loopback in the supported desktop and browser launch paths.
2. Protected routes must fail closed when `DAVE_API_KEY` is absent.
3. Electron may read the key only in the main process and inject it only for the exact configured backend origin.
4. The preload bridge may expose the API base and desktop marker, never the key or Node.js primitives.
5. Browser mode may keep the key only in `sessionStorage`.
6. Only `static/` may be mounted at `/`; repository source, Git metadata, logs, JSON, databases, and uploads must not become static files.
7. Real node addresses and generated `DAVE_NODES` JSON must not be written to the repository.
8. The macOS launcher must keep the API key in the `com.davellm.api-key` Keychain item.
9. File tools must resolve within explicit absolute `DAVE_TOOL_ROOTS`.
10. `web.fetch` must accept only credential-free public HTTP/HTTPS targets, validate DNS and every redirect target, stop after five redirects, and read no more than 1 MB.
11. `shell.exec` requires the tools flag, a second shell flag, per-run approval, no shell interpolation, and the fixed command allowlist: `echo`, `date`, `pwd`, `ls`, `wc`, `head`, and `tail`.
12. Runtime data, secrets, prompts, and user files must not be committed or published through GitHub Pages.

## 10. Configuration contract

| Variable | Required | Default or purpose |
|---|---:|---|
| `DAVE_API_KEY` | Yes | Shared local secret for all protected routes. |
| `DAVE_NODES` | Yes for real use | JSON array of `{id,name,url}` Ollama nodes. Source fallbacks are non-working placeholders. |
| `DAVE_DATA_DIR` | Recommended | Runtime data root; process working directory when unset. |
| `DAVE_PORT` | No | Loopback router port; `8000`. |
| `DAVE_PYTHON` | No | Python executable Electron uses; repository venv by default. |
| `DAVE_STARTUP_TIMEOUT_MS` | No | Electron readiness deadline; 15 seconds. |
| `DAVE_NODE_TIMEOUT` | No | Ollama inventory timeout; 10 seconds. |
| `DAVE_CORS_ORIGINS` | No | Comma-separated allowed browser origins. |
| `DAVE_RATE_WINDOW` | No | Rate-limit window; 60 seconds. |
| `DAVE_RATE_MAX` | No | Requests permitted per window; 30. |
| `DAVE_BUDGET_DEFAULT` | No | Default synthetic cost budget; `100`. |
| `DAVE_USER_BUDGETS` | No | JSON map of per-user synthetic budgets. |
| `DAVE_MODEL_CONTEXT_DEFAULT` | No | Fallback model window; 32,768 tokens. |
| `DAVE_MODEL_CONTEXT_WINDOWS` | No | JSON map of model IDs to windows of at least 4,096 tokens. |
| `DAVE_PROJECT_CONTEXT_TOKENS` | No | New-project context budget; 16,384 tokens. |
| `DAVE_BRAIN_COMPACT_TOKENS` | No | New-project compaction threshold; 3,072 tokens. |
| `DAVE_BRAIN_RECOVERY_DAYS` | No | Soft-delete recovery window; 30 days. |
| `DAVE_ENABLE_TOOLS` | No | Enables the bounded tool registry; false. |
| `DAVE_TOOL_ROOTS` | With file tools | JSON array of explicit absolute roots. |
| `DAVE_ENABLE_SHELL_TOOL` | No | Second opt-in for `shell.exec`; false. |
| `DAVE_WHISPER_BIN` | No | Alternate `whisper-cli` path. |
| `DAVE_WHISPER_MODEL` | No | Alternate Whisper model path. |
| `FFMPEG_BIN` | No | Alternate ffmpeg path. |

## 11. Operational limits

| Boundary | Current limit |
|---|---:|
| User prompt | 100,000 characters |
| Project context budget | 1,024 to 262,144 tokens |
| Project file upload | 10 MB |
| Base64 image payload | 5 MB |
| Audio upload | 20 MB |
| Notepad | 200,000 characters |
| Each BRAIN text tier | 1,000,000 characters |
| Context-preview query | 1,000,000 characters |
| Tool output returned to model/UI | 5,000 characters |
| Public web fetch | 1 MB and 5 redirects |
| Agent input messages | 1 to 200; serialized request up to 1 MB |
| Agent loop | 8 steps and 2 errors by default; configurable request bounds are 1-32 steps and 1-8 errors |
| Tool timeout | 10 seconds by default |
| Model turn timeout in agent loop | 120 seconds |

## 12. Installation and launch

### Repository setup

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
npm ci
```

### Standard desktop launch

```bash
export DAVE_API_KEY='<local-secret>'
export DAVE_NODES='[{"id":"<node-id>","name":"<display-name>","url":"http://<ollama-host>:11434"}]'
npm start
```

### macOS operator installation

```bash
npm run install:macos
npm run install:whisper:macos   # optional local dictation runtime
```

The launcher is installed at `~/Applications/DaveLLM Launcher.app`, persists data under `~/Library/Application Support/DaveLLM`, and logs startup failures to `~/Library/Logs/DaveLLM/launcher.log`. Dominic and Walter are required launcher peers. Duncan is included only when online and its Ollama inventory responds within four seconds; Duncan unavailability must not block startup.

The optional dictation installer adds `whisper-cpp` and a checksum-verified English `tiny.en` model outside the repository. ffmpeg is also required at runtime.

## 13. Quality and acceptance criteria

A DaveLLM release is acceptable only when all applicable checks below pass:

1. Python modules compile and the complete pytest suite passes.
2. Runtime JavaScript and Electron entry points pass `node --check`.
3. Maintained shell scripts pass syntax checks.
4. `npm ci`, the dependency tree check, and the repository whitespace check pass.
5. Authentication, static isolation, node/model validation, streaming errors, tool default-off behavior, context ordering, BRAIN recovery, and renderer contracts remain covered by automated tests.
6. For UI changes, the actual Electron or browser surface is exercised at the relevant desktop/mobile width and with keyboard access and reduced motion when applicable.
7. For cluster claims, live node status, live model inventory, a visible model response, and persistence reload are verified separately from router health.
8. The exact commit is pushed, required GitHub Actions complete, and the Pages artifact is verified when documentation changes.

Repository verification commands:

```bash
python -m py_compile app.py project_context.py scripts/project_context_cli.py tool_executor.py
python -m pytest -q
node --check static/app.js static/anticipation.js static/prompt-contract.js desktop/main.js desktop/preload.js
bash -n deploy/check-cluster.sh scripts/verify-cluster.sh
npm ci
npm ls --depth=0
git diff --check
```

## 14. Current implementation snapshot

As of 2026-09-02:

- `main` and `origin/main` matched at implementation commit `845e7db` before this document was added.
- That implementation includes the Instructions exact-effective scroll repair and authenticated local microphone dictation repair.
- CI and GitHub Pages passed for that implementation commit.
- The public documentation artifact is `https://davehomeassist.github.io/DaveLLM/`.
- The local DaveLLM process was stopped during this specification review; no claim of live node, inventory, model, or dictation availability is made by this snapshot.

## 15. Known limits and open decisions

1. **Runtime proof:** repository tests cannot prove current node reachability, installed model inventory, inference quality, Whisper accuracy, or hardware performance.
2. **Product split:** decide whether DaveHarness becomes a separately named package/service or remains an internal DaveLLM capability.
3. **Version source:** the FastAPI application reports `2.1` while the Electron package reports `1.0.0`; a single release-version source has not been established.
4. **Lifecycle API:** FastAPI startup/shutdown event handlers emit deprecation warnings; migration to lifespan handlers remains deferred.
5. **Persistence evolution:** conversations and core project metadata remain JSON while normalized project context is SQLite; full migration is deferred.
6. **Dictation quality:** the installed default `tiny.en` model is English-only and favors a small local footprint over maximum accuracy.
7. **Tools UI:** the backend returns a complete bounded run result; a full streaming agent-run ledger remains future work.
8. **Placeholder inventory:** source fallback nodes are deliberately non-working. Normal macOS operation depends on launcher-resolved Tailscale peers or an explicitly supplied `DAVE_NODES` value.

## 16. Change control

Update this specification in the same change whenever a release materially changes the product boundary, user journeys, API families, instruction/context order, persistence layout, security model, configuration, operational limits, or acceptance gates. Keep volatile node addresses, installed model lists, credentials, personal content, and runtime data out of this document.
