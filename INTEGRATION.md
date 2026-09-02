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

### `POST /tools/agent/run`

Accepts a message array, inventory-backed node and model, step ceiling, error budget, and per-run approved tool names. FastAPI sends the current registered JSON schemas on every Ollama request. The response contains `status`, `transcript`, `final_answer`, `steps`, `errors`, `status_message`, and any `pending_tool_call`. The default ceiling is eight. Mutating and execution tools stop at `approval_required` unless named in `approved_tools` for that run.

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
