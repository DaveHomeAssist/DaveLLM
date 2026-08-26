# DaveLLM Router

## Product

DaveLLM is a FastAPI router with an Electron and browser UI for authenticated chat against configured Ollama nodes. The repository preserves conversation and project JSON semantics, SQLite vector/feedback/performance stores, streamed chat, attachments, export, monitoring, and optional local tools.

## Source layout

- `app.py`: FastAPI routes, persistence, inventory, chat, tools, monitoring
- `tool_executor.py`: runtime tool registry, schema validation, timing, approval boundaries, and bounded executor loop
- `static/`: runtime HTML, CSS, JavaScript, monitoring, favicon
- `desktop/`: Electron main process and preload bridge
- `scripts/macos/`: Keychain-backed launcher and local app installer; node addresses are resolved from live Tailscale state
- `tests/`: source-aligned FastAPI, mocked Ollama transport, security, and renderer contract tests
- `docs/`: separate existing documentation artifact; do not use it as the runtime static root

## Trust boundaries

- `/health` is public for readiness.
- Protected routes require `X-API-Key` and fail closed if `DAVE_API_KEY` is unset.
- Electron reads the key only in `desktop/main.js` and injects it only for the exact loopback backend origin.
- The preload exposes the API base and an Electron marker, never the key.
- Browser credentials use `sessionStorage`, never persistent `localStorage`.
- The macOS launcher stores only `DAVE_API_KEY` in Keychain and constructs `DAVE_NODES` in memory from live Tailscale peer records. Dominic and Walter are required; Duncan is appended only when online and Ollama-responsive, and never blocks startup.
- Only `static/` is mounted at `/`; source, Git metadata, JSON, SQLite, and logs must remain unreachable.
- Tools default off. File tools require explicit absolute roots. Shell execution requires a second opt-in.
- The agent loop defaults to eight model steps, returns partial transcripts, and requires per-run approval for mutating or execution tools.
- Global, project, and session instruction layers are visible in the UI and resolve into one exact primary system message.
- Project notepads are plain text in project persistence. Do not add rich text, history, collaboration, or browser note storage.

## Ollama integration

`DAVE_NODES` is the only runtime node inventory override. Do not add real addresses or fabricate aliases in source. Model discovery uses `/api/tags`; inference uses `/v1/chat/completions`. A chat must name a node returned by `/nodes` and a model returned for that node. Automatic routing is advisory and may replace a manual model only when the suggestion exists in the currently loaded inventory.

## Persistence

Every runtime persistence path is based on `BASE_DIR`, which is derived from `DAVE_DATA_DIR` and defaults to the current directory:

- `dave_conversations.json`
- `dave_projects.json`
- `dave_settings.json`
- `dave_vectors.db`
- `feedback.db`
- `performance.db`
- `cost_log.jsonl`

Do not import, move, or infer legacy model or data locations.

## Running and verification

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
npm ci

export DAVE_API_KEY='<local-secret>'
export DAVE_NODES='[{"id":"<node-id>","name":"<display-name>","url":"http://<ollama-host>:11434"}]'
npm start
```

Required checks after relevant changes:

```bash
python -m py_compile app.py
python -m pytest -q
node --check static/app.js static/prompt-contract.js desktop/main.js desktop/preload.js
bash -n deploy/check-cluster.sh scripts/verify-cluster.sh
npm ci
npm ls --depth=0
git diff --check
```

## Current limits and open decisions

- Real node reachability, installed model inventory, inference quality, Whisper execution, and hardware performance require cluster access and are not proven by repository tests.
- Electron is pinned to `^44.0.0` (upgraded from `^30.0.0` per audit finding H-1, 2026-08-26). npm audit reports no known vulnerabilities at this line; keep the pin on a supported major.
- FastAPI startup/shutdown event deprecation warnings are known; a lifespan migration is deferred because it is outside the P0 stabilization scope.
- JSON conversation/project persistence is preserved. A SQLite migration is deferred.
