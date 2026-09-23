# DaveLLM Frontend and Backend Integration

## Runtime flow

1. Electron spawns uvicorn on loopback, or an operator starts uvicorn for browser mode.
2. Electron waits for public `GET /health` before loading the UI.
3. Protected UI requests receive `X-API-Key` from the Electron main process or browser `sessionStorage`.
4. The UI loads `GET /nodes`.
5. The UI loads `GET /nodes/{node_id}/models`; the router queries that Ollama node at `GET /api/tags` and records the returned inventory.
6. The UI submits `/chat` or `/chat/stream` only with the selected configured node and a model in that node's loaded inventory.
7. The router sends an OpenAI-compatible request to the selected Ollama node at `/v1/chat/completions`.

There is no `/api` prefix.

## Authentication

`GET /health` is public. All operational, conversation, chat, monitoring, export, project, model, and tool routes are protected.

- Server missing `DAVE_API_KEY`: protected route returns `503`.
- Missing or wrong `X-API-Key`: protected route returns `401`.
- Correct `X-API-Key`: request proceeds.

Electron stores no renderer credential. `desktop/main.js` injects the environment key only for the exact `http://127.0.0.1:<DAVE_PORT>` origin. `desktop/preload.js` never receives the key. Non-Electron browser use prompts for a session-only value.

## Core contracts

### `GET /health`

```json
{
  "status": "ok",
  "version": "2.1.0",
  "nodes": [],
  "active_conversations": 0
}
```

The version is read from root `VERSION`, the canonical DaveLLM Semantic Version, and must match the desktop package manifests.

### `GET /nodes`

Returns configured node objects with `id`, `name`, and `url`.

### `GET /nodes/{node_id}/models`

```json
{
  "node_id": "<configured-node-id>",
  "node_name": "<configured-display-name>",
  "models": [
    {"id": "<ollama-model-id>", "vision": false}
  ]
}
```

The model IDs are taken from Ollama `/api/tags`; they are not synthesized by the router.

### `POST /conversations/from_template`

```json
{
  "template_name": "code_review",
  "project_id": null
}
```

The body is validated by Pydantic. Existing templates remain `general`, `code_review`, and `brainstorm`. There is no template CRUD API.

### `POST /chat`

```json
{
  "conversation_id": "<conversation-id>",
  "prompt": "Exact text displayed in the UI",
  "node_id": "<configured-node-id>",
  "model": "<model-id-from-that-node>",
  "max_tokens": 2048,
  "temperature": 0.7,
  "project_id": null,
  "images": []
}
```

`node_id` and `model` are required at runtime. An unloaded inventory returns `409`; an unavailable model returns `400`; an unknown node returns `404`. Attached text and the Support prefix are incorporated into one effective prompt used both for display and the backend request.

### `POST /chat/stream`

Success token:

```text
data: {"token":"partial text","done":false}
```

Terminal event:

```text
data: {"token":"","done":true,"message_count":2}
```

Node failures are returned as SSE error events. The renderer promotes `error` events into the visible chat error state rather than treating them as JSON parse failures.

### `GET /conversations/{conversation_id}/export`

The renderer performs authenticated fetch, converts the response to a Markdown Blob, triggers a download, and revokes the object URL. Non-success responses are shown to the user.

### `GET /tools` and `POST /tools/execute`

Both return `403` unless `DAVE_ENABLE_TOOLS=true`. `DAVE_TOOL_ROOTS` must be a JSON array of absolute paths. File read, write, and append use the same resolved-path containment rule. `shell.exec` also requires `DAVE_ENABLE_SHELL_TOOL=true`.

The generic registry, validation, policy, budgets, execution result, call parsing, exact-call approval state, and bounded loop are provided by the in-process `daveharness` package at version `0.9.0`. Its `CONTRACT_VERSION = 1` envelope from `0.3.0` serializes only `ParsedToolCall`, `PendingCall`, `ToolExecution`, and `ExecutorOutcome` through `encode_contract`/`decode_contract`; it is not an HTTP response format. `app.py` owns and injects the concrete tools, configured roots, authentication, Ollama model adapter, and HTTP routes. `tool_executor.py` remains a compatibility re-export only. First-party handlers are synchronous except for explicitly opted-in and name-allowlisted `web.fetch`.

The `0.2.0` milestone introduced the current sync-first execution and exact-call approval/resume semantics. H1 leaves those behaviors in place.

DaveHarness `0.4.0` added policy, fingerprints, and budgets. Version `0.5.0` adds a separate serializable run-state contract; Version `0.6.0` adds opt-in cancellation and absolute deadlines; version `0.7.0` adds metadata-only ordered events; version `0.8.0` adds the bounded store and instance-owned facade. DaveHarness `0.9.0` adds DaveLLM lifecycle routes, a run ledger, and frozen project context. The legacy HTTP routes remain on their shipped adapter.

### `POST /tools/agent/run`

Accepts a message array, inventory-backed node and model, step ceiling, error budget, and the existing per-run approved tool-name list. FastAPI sends the current registered JSON schemas on every Ollama request. Existing request fields are unchanged. The response contains `status`, `transcript`, `final_answer`, `steps`, `errors`, `status_message`, and any `pending_tool_call`, plus additive `run_id` and pending-call fields. The default ceiling is eight. Mutating and execution tools stop at `approval_required` unless already named in `approved_tools` for that run.

An approval-required call is schema-validated before it is exposed as pending. Its in-process record contains the call ID, tool name, canonical arguments, SHA-256 digest, transcript revision, single-use nonce, creation time, and expiry time. The public pending object omits the nonce. Its default time-to-live is 300 seconds.

### `POST /tools/agent/resume`

Requires the same API-key authentication and tools-enabled flag as the other tool routes. The JSON body is exactly `run_id`, `call_id`, `digest`, and `decision`, where `decision` is `approve` or `deny`. Approval atomically consumes the pending nonce, executes only the stored canonical arguments, and continues from the paused transcript without invoking the model again for that step. Denial atomically consumes the same decision, appends a tool message with `status: "denied"` and `termination: "denied"`, and continues the bounded loop without consuming the tool-error budget.

Non-executing resume outcomes are distinct: `approval_not_found`, `approval_call_mismatch`, `approval_digest_mismatch`, `approval_stale`, `approval_replayed`, and `approval_expired`. The pending store is memory-only, so process restart makes pending runs unavailable. Tool-result `termination` is additive and may be `completed`, `deadline_abandoned`, `denied`, or `error`; existing `status` values and transcript fields remain intact. `deadline_abandoned` means the harness stopped waiting and the underlying synchronous worker may still run.

### Instruction endpoints

`GET /conversations/{conversation_id}/instructions` returns the global, project, and session layers plus their precedence and exact effective text. `PUT` on the same route saves supplied layers and applies them to the next message. `DELETE /conversations/{conversation_id}/instructions/session` reverts the session override. `GET` and `DELETE /instructions/global` inspect or restore the source-controlled global default.

### Project notepad endpoints

`GET /projects/{project_id}/notepad` returns the project-scoped plain text. `PUT` autosaves a bounded `content` string. The notepad does not create artifact history, rich text, or browser-persisted note copies.

### Project Homepage and context contracts

`GET /projects/{project_id}/homepage` returns the project record, attached conversation IDs, exact baseline quotas, usage, and four independently owned components: Project Instructions, File Context Uploads, Artifact History, and BRAIN.

- `POST /projects/{project_id}/files`, plus file `PUT`, `reindex`, and `DELETE` routes, own local upload, status, attach/detach, reindex, and deletion. Only attached, successfully indexed UTF-8 text chunks are eligible for requests.
- Artifact list/get/update/delete routes own retained assistant outputs. Attached-chat responses are captured automatically; pinning affects retrieval priority and archiving removes an artifact from request context.
- BRAIN get/update/delete, compact, revisions, and restore routes own tiered durable context. Updates use optimistic revision checks; deletion is soft until the configured recovery window expires.
- `POST /projects/{project_id}/context-preview` returns the exact assembled next-request messages and budget without calling a model or mutating the conversation.
- `PUT /conversations/{conversation_id}/project` is the only route that reattaches an existing chat. It records a future-only context event. A `null` project creates a General chat context.

The request allocator reserves model output, a five-percent safety margin, and non-project history first. Its baseline project split is 25 percent instructions, 25 percent BRAIN, 30 percent files, and 20 percent artifacts. Unused tokens roll forward to BRAIN, files, then artifacts. The payload order is one exact primary system prompt containing global, project, and session instruction layers; BRAIN; ranked file context; ranked artifact history; bounded conversation history; and the current user message.

## Persistence and static serving

All persistence artifacts, including `dave_settings.json`, `dave_project_context.db`, and `project_uploads/`, resolve under `DAVE_DATA_DIR`, with the current directory retained as the unset default. Runtime UI files are served only from `static/`. Requests for source, `.git`, JSON, SQLite, and log paths return `404` unless a separately declared API route owns the path.

## Verified versus runtime-dependent

Automated tests verify auth states, static isolation, Ollama-compatible inventory and chat transports, stream success and failure events, templates, export, tools default-off behavior, title generation, raw embedding indexes, exact effective-prompt construction, Project Homepage lifecycles, request-context order and preview, BRAIN compaction/recovery, token rollover, and data-directory containment.

Real cluster reachability, actual model inventory, Whisper binaries, inference performance, and end-to-end hardware behavior remain runtime-dependent and require an authorized cluster check.
