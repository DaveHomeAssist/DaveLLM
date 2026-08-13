# DaveLLM Desktop

DaveLLM is an Electron desktop client backed by a FastAPI router. The router discovers models from configured Ollama nodes, streams OpenAI-compatible chat responses, and persists conversations, projects, vector indexes, feedback, performance, and cost data on the router host.

## Requirements

- Python 3
- Node.js and npm
- One or more reachable Ollama nodes
- `DAVE_API_KEY` set to a non-empty local secret

The repository does not include models, Whisper assets, credentials, or runtime data.

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

Electron starts uvicorn, waits up to 15 seconds for public `/health`, injects `X-API-Key` only into requests to its exact loopback backend origin, and then loads the UI. Override the Python executable with `DAVE_PYTHON`, the port with `DAVE_PORT`, or the readiness timeout with `DAVE_STARTUP_TIMEOUT_MS`.

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

## Data and tools

`DAVE_DATA_DIR` relocates all six persistence artifacts. When unset, the current working directory remains the default.

- `dave_conversations.json`
- `dave_projects.json`
- `dave_vectors.db`
- `feedback.db`
- `performance.db`
- `cost_log.jsonl`

Tools are disabled by default. To enable them, set `DAVE_ENABLE_TOOLS=true` and provide `DAVE_TOOL_ROOTS` as a JSON array of absolute paths. File read, write, and append operations share the same containment check. `shell.exec` remains disabled unless `DAVE_ENABLE_SHELL_TOOL=true` is also set. `web.fetch` accepts only bounded public HTTP/HTTPS responses and validates DNS plus each redirect target.

## Local suggestions and mobile navigation

The browser client derives at most three deterministic Suggested next actions from the current composer, attachment type, validated project/node/model selection, and response shape. Prediction generation lives in `static/anticipation.js`; it performs no network, DOM, or storage work, and suggestion chips never submit or change context without a visible user action. Valid last-used selections may be restored only on the empty startup state after inventory and node-health checks, with a visible status and immediate Undo. The client no longer calls `/route/decision` while sending a message, so the selected model remains under manual control.

Suggestion preferences use the versioned `davellm_anticipation_v1` local-storage record. Its schema is limited to stable IDs, booleans, capped counters, and timestamps; prompt/response text, attachment names or contents, credentials, node URLs, and system prompts are excluded. Unsent composer text and the active Chat/History/Runtime mobile tab remain session-only. Reset Suggestions removes only the versioned suggestion record.

## Validation

```bash
source venv/bin/activate
python -m py_compile app.py
python -m pytest -q
node --check static/app.js
node --check static/anticipation.js
node --check static/prompt-contract.js
node --check desktop/main.js
node --check desktop/preload.js
bash -n deploy/check-cluster.sh scripts/verify-cluster.sh
npm ci
npm ls --depth=0
```

Runtime UI files are under `static/`; only that directory is mounted at `/`. The separate `docs/` content is unchanged and is not used by the desktop runtime.

## Dependency note

`package.json` intentionally retains Electron `^30.0.0`. The generated lockfile currently resolves Electron 30.5.1. Current `npm audit` data reports a high-severity advisory set that requires a major Electron upgrade to clear; that upgrade is a separate compatibility decision.
