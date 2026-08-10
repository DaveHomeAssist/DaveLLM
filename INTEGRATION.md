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
  "nodes": [],
  "active_conversations": 0
}
```

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

## Persistence and static serving

All persistence artifacts resolve under `DAVE_DATA_DIR`, with the current directory retained as the unset default. Runtime UI files are served only from `static/`. Requests for source, `.git`, JSON, SQLite, and log paths return `404` unless a separately declared API route owns the path.

## Verified versus runtime-dependent

Automated tests verify auth states, static isolation, Ollama-compatible inventory and chat transports, stream success and failure events, templates, export, tools default-off behavior, title generation, raw embedding indexes, exact effective-prompt construction, and data-directory containment.

Real cluster reachability, actual model inventory, Whisper binaries, inference performance, and end-to-end hardware behavior remain runtime-dependent and require an authorized cluster check.
