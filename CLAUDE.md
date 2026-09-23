# DaveLLM Router

## Product

DaveLLM is a FastAPI router with an Electron and browser UI for authenticated chat against configured Ollama nodes. The repository preserves conversation and core project JSON semantics, normalized SQLite project context plus vector/feedback/performance stores, streamed chat, attachments, export, monitoring, and optional local tools.

## Source layout

- `app.py`: FastAPI routes, persistence, inventory, chat, tools, monitoring
- `project_context.py`: normalized Project Homepage storage, BRAIN revisions, file/artifact retrieval, and bounded request assembly
- `daveharness/`: headless in-process `0.3.0` library for typed leaf contracts, versioned serialization, registry, schema validation, timing, exact-call approval/resume, and bounded executor loop
- DaveHarness `0.2.0` was the prior execution-semantics milestone; `0.3.0` adds the internal module split and leaf-contract envelope without changing DaveLLM endpoints.
- `tool_executor.py`: compatibility re-export for legacy imports; do not add implementation here
- `VERSION`: canonical DaveLLM Semantic Version mirrored into package metadata and runtime output
- `static/`: runtime HTML, CSS, JavaScript, monitoring, favicon
- `static/vendor/`: pinned browser-only Lucide and GSAP assets with their license notices; no CDN runtime path
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
- The agent loop defaults to eight model steps, returns partial transcripts, and requires exact-call approval for mutating or execution tools. Pending calls use canonical arguments, a SHA-256 digest, transcript revision, single-use nonce, and a 300-second in-memory expiry.
- First-party tool handlers are synchronous except for explicitly opted-in, registry-allowlisted `web.fetch`. Every tool definition declares `cancellation` as `bounded` or `abandon`; approval-required and write tools must be `bounded`.
- Preserve existing tool status strings. The additive result `termination` is `completed`, `deadline_abandoned`, `denied`, or `error`. `deadline_abandoned` means DaveHarness stopped waiting and does not prove an underlying synchronous worker stopped.
- `POST /tools/agent/resume` must execute only stored canonical arguments for the matching run, call, digest, transcript revision, unexpired nonce, and current registry definition. It must not replay the paused model step or reuse a consumed decision.
- Global, project, and session instruction layers are visible in the UI and resolve into one exact primary system message.
- Project notepads are plain text in project persistence. Do not add rich text, history, collaboration, or browser note storage.
- Existing-chat project changes must use the explicit attachment endpoint and apply only to future messages.
- DaveLLM and DaveHarness are separate product concepts with one dependency direction: DaveLLM consumes DaveHarness through the in-process `daveharness` package. DaveHarness owns generic registry, validation, permission, approval, budget, terminal-state, and transcript contracts; it must not directly own DaveLLM HTTP, Ollama inventory, persistence, project context, UI, environment, or product-specific tool implementations. Preserve `tool_executor.py` only as the legacy compatibility import.
- Do not create a DaveHarness network service or separate repository without a second production consumer, independent deployment cadence, incompatible dependency requirement, or required remote execution boundary.
- Keep the default pending-call store in-process. Do not move approval state into DaveLLM persistence or claim process-restart durability without a separately approved lifecycle design.
- Root `VERSION` is the DaveLLM release-version authority. FastAPI metadata, startup output, `GET /health`, `package.json`, and root lockfile metadata must match it.

## Ollama integration

`DAVE_NODES` is the only runtime node inventory override. Do not add real addresses or fabricate aliases in source. Model discovery uses `/api/tags`; inference uses `/v1/chat/completions`. A chat must name a node returned by `/nodes` and a model returned for that node. Automatic routing is advisory and may replace a manual model only when the suggestion exists in the currently loaded inventory.

## Persistence

Every runtime persistence path is based on `BASE_DIR`, which is derived from `DAVE_DATA_DIR` and defaults to the current directory:

- `dave_conversations.json`
- `dave_projects.json`
- `dave_settings.json`
- `dave_project_context.db`
- `dave_vectors.db`
- `feedback.db`
- `performance.db`
- `cost_log.jsonl`
- `project_uploads/`

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

GitHub Actions enforces these checks on every push to `main` and every pull request via `.github/workflows/ci.yml`.

Required checks after relevant changes:

```bash
python -m py_compile app.py project_context.py scripts/project_context_cli.py tool_executor.py
python -m compileall -q daveharness
python -m mypy daveharness
python -m pytest -q
node --check static/app.js static/anticipation.js static/prompt-contract.js static/vendor/gsap/gsap.min.js desktop/main.js desktop/preload.js
bash -n deploy/check-cluster.sh scripts/verify-cluster.sh scripts/macos/install-launcher.sh scripts/macos/install-whisper-runtime.sh scripts/macos/launch-davellm.sh
npm ci
npm ls --depth=0
npm audit --audit-level=high
git diff --check
```

## Current limits and open decisions

- Real node reachability, installed model inventory, inference quality, Whisper execution, and hardware performance require cluster access and are not proven by repository tests.
- Electron is pinned to `^44.0.0` (upgraded from `^30.0.0` per audit finding H-1, 2026-08-26). npm audit reports no known vulnerabilities at this line; keep the pin on a supported major.
- FastAPI startup/shutdown event deprecation warnings are known; a lifespan migration is deferred because it is outside the P0 stabilization scope.
- Conversation JSON and core project metadata remain compatible. Project Instructions, BRAIN revisions, uploaded-file indexes, and artifact history are normalized in `dave_project_context.db`; a full conversation migration remains deferred.
