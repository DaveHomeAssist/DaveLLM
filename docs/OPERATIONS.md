# DaveLLM Operations

This runbook covers the DaveLLM router and clients using placeholders only. It does not define real node addresses, credentials, installed models, or cluster ownership. Query the authorized runtime environment for those values.

## Prerequisites

- Python 3 with the repository virtual environment installed
- Node.js and npm dependencies installed
- One or more reachable Ollama nodes
- `DAVE_API_KEY` set to a non-empty local secret
- `DAVE_NODES` set to a JSON array of configured Ollama nodes

Never commit `DAVE_API_KEY`, real node addresses, credentials, or runtime data.

## Configure the process

The following values are placeholders, not working cluster inventory:

```bash
export DAVE_API_KEY='<local-secret>'
export DAVE_NODES='[{"id":"<node-id>","name":"<display-name>","url":"http://<ollama-host>:11434"}]'
```

Set `DAVE_DATA_DIR` to an existing writable directory when runtime data must live outside the current working directory:

```bash
export DAVE_DATA_DIR='/absolute/path/to/davellm-data'
```

## Launch the Electron client

From the repository root:

```bash
npm start
```

Electron starts uvicorn on loopback, waits for public `GET /health`, injects `X-API-Key` only for its exact backend origin, and loads the runtime UI from `static/`.

### Install the one-click macOS launcher

After the standard setup is complete, run:

```bash
npm run install:macos
```

The installer creates or preserves the `com.davellm.api-key` generic-password item in the current user's macOS Keychain and installs `~/Applications/DaveLLM Launcher.app`. The launcher retrieves the key at runtime, discovers the Tailscale peers named `dominic` and `walter`, constructs `DAVE_NODES` only in process memory, sets `DAVE_DATA_DIR` to `~/Library/Application Support/DaveLLM`, and runs `npm start` from the repository root. No key or resolved node address is written into source control.

Double-click **DaveLLM Launcher** for normal operation. Review `~/Library/Logs/DaveLLM/launcher.log` if startup fails. The launcher stops before starting a second backend when TCP port `8000` is already occupied.

### Install local dictation on macOS

Run the dedicated runtime installer once:

```bash
npm run install:whisper:macos
```

It installs Homebrew `whisper-cpp` when needed, verifies the official `tiny.en` model checksum, and stores the model under `~/Library/Application Support/DaveLLM/models/`. DaveLLM discovers `whisper-cli` on `PATH` and reads the model from `DAVE_DATA_DIR/models/ggml-tiny.en.bin`. Use `DAVE_WHISPER_BIN` and `DAVE_WHISPER_MODEL` only for explicit alternate installations.

## Launch browser mode

From the repository root with the virtual environment and required variables configured:

```bash
./venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`. The browser prompts for the API key and keeps it in `sessionStorage` for the current tab session.

## Verify router health

`GET /health` is public and proves only that the router process is ready:

```bash
curl -fsS http://127.0.0.1:8000/health
```

Do not treat this response alone as node, model, or chat proof.

## Verify configured nodes

Use authenticated router endpoints instead of contacting guessed hosts:

```bash
curl -fsS \
  -H "X-API-Key: $DAVE_API_KEY" \
  http://127.0.0.1:8000/nodes

curl -fsS \
  -H "X-API-Key: $DAVE_API_KEY" \
  http://127.0.0.1:8000/nodes/status
```

Record each returned node `id`, status, and latency. `DAVE_NODES` is the only runtime node-inventory override.

## Verify model inventory

Use a node ID returned by `GET /nodes`:

```bash
NODE_ID='<node-id>'
curl -fsS \
  -H "X-API-Key: $DAVE_API_KEY" \
  "http://127.0.0.1:8000/nodes/$NODE_ID/models"
```

The router obtains model IDs from that node's Ollama `GET /api/tags` response. Do not invent model aliases or claim a model is available without this live inventory.

## Verify user-facing behavior

After health, node status, and inventory checks pass:

1. Open the Electron client or `http://127.0.0.1:8000/`.
2. Confirm the expected configured node and model are selectable.
3. Send a non-sensitive test prompt through `/chat` or `/chat/stream`.
4. Confirm the response is visibly rendered and the conversation persists after reload.
5. Record the node ID, model ID, observable result, and verification time without recording credentials or prompt contents that contain sensitive data.

## Persistence

When `DAVE_DATA_DIR` is unset, persistence defaults to the process working directory. When set, these six artifacts resolve beneath it:

- `dave_conversations.json`
- `dave_projects.json`
- `dave_vectors.db`
- `feedback.db`
- `performance.db`
- `cost_log.jsonl`

Stop DaveLLM before copying or relocating persistence artifacts. Do not infer, import, or overwrite legacy data locations.

## Shutdown

- Electron mode: close the DaveLLM desktop application so the main process can terminate its uvicorn child.
- Browser mode: press `Ctrl+C` in the foreground uvicorn terminal.
- Confirm shutdown without terminating unrelated processes:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

No output means no process is listening on TCP port `8000`.

## Troubleshooting

### Connection refused

Confirm that `DAVE_API_KEY` and `DAVE_NODES` are set in the process environment, then launch DaveLLM. Verify a listener exists on TCP port `8000` before retrying the UI.

### Protected endpoint returns `503`

The server process has no configured `DAVE_API_KEY`. Stop it, set the variable, and start it again.

### Protected endpoint returns `401`

The request is missing `X-API-Key` or the supplied value does not match the server process.

### Node status is offline

Treat the node as unavailable. Verify its network and Ollama service through the authorized infrastructure channel; do not substitute an unverified address.

### Chat returns `409`

The selected node's inventory has not loaded. Fetch `GET /nodes/{node_id}/models` successfully before retrying.

### Chat returns `400` for the model

The selected model is not in the loaded inventory for that node. Choose an ID returned by the node-model endpoint.

## Completion evidence

An operational verification report should record:

- Router `/health` result
- Authenticated node-status result
- Selected node ID and model ID
- Visible chat result
- Persistence reload result
- Exact failures or skipped checks
- Local verification timestamp
