# DaveLLM Router

## Product

DaveLLM is a FastAPI router with an Electron and browser UI for authenticated chat against configured Ollama nodes. The repository preserves conversation and core project JSON semantics, normalized SQLite project context plus vector/feedback/performance stores, streamed chat, attachments, export, monitoring, and optional local tools.

## Source layout

- `app.py`: FastAPI routes, persistence, inventory, chat, tools, monitoring
- `project_context.py`: normalized Project Homepage storage, BRAIN revisions, file/artifact retrieval, and bounded request assembly
- `daveharness/`: headless in-process `1.0.0-rc.1` library for typed leaf contracts, versioned serialization, policy, budgets, registry, schema validation, timing, exact-call approval/resume, and bounded executor loop
- DaveHarness `0.2.0` was the execution-semantics milestone; `0.3.0` added the internal module split and leaf-contract envelope; `0.4.0` added policy, fingerprints, and budgets; `0.5.0` adds snapshot and exact decision operations without changing DaveLLM endpoints. `0.6.0` adds opt-in cooperative cancellation and absolute deadlines; `0.7.0` adds metadata-only ordered events; `0.8.0` adds a bounded store and instance-owned facade; `0.9.0` integrates the DaveLLM lifecycle and frozen BRAIN run context.
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
- `DAVE_ENABLE_EXTENDED_TOOLS` is a separate opt-in, honored only when `DAVE_ENABLE_TOOLS` is also on, that adds exactly `file.list`, `file.search`, `file.read_lines`, `md.outline`, `md.section`, `git.status`, `git.diff`, `git.log`, and `git.show` (`read_files`, no approval, bounded). With it off, the qualified catalog and its definition fingerprints are unchanged. The file tools live in `davellm_files.py`; the Markdown tools live in `davellm_markdown.py`, share the one heading parser there, and reuse the `davellm_files.py` path, read, and budget helpers rather than their own. The Git tools live in `davellm_git.py`. Keep new `app.py` code below the existing tool handlers, qualified and extended, because handler line numbers are part of the fingerprints (`tests/test_tool_catalog_provenance.py` pins them and the extended definitions). The tool catalog is `docs/DAVELLM_TOOLS.md`.
- Extended tools admit every path through `resolve_extended_tool_path`, which anchors a relative path at the single tool root (refusing it when several roots are configured) and wraps `resolve_tool_path` with the secret denylist: `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_rsa*`, `*.p12`, `.ssh`, `.aws`, and `.gnupg`, matched case-insensitively against every path component before and after symlink resolution. Every refusal, including a root escape, raises the same `PathNotAllowed` message. `walk_tree` never lists, enters, or counts secrets and omits symlinks that leave the root. The qualified `file.read` does not apply the denylist; extending it is a separate decision. New file tools must pass the shared contract in `tests/tool_contract.py` against the `hostile_tree` fixture.
- Extended tools read file contents only through `read_admitted_bytes`, which opens each path component from `/` relative to its parent descriptor with `O_NOFOLLOW` (or opens and re-verifies inode and path where descriptor walks are unavailable), so a symlink swapped in after admission fails closed. Results stay under 48 KiB, show only root-relative paths, and never carry OS error text. `file.search` is literal only; do not add regex without a bounded engine or process boundary.
- Git tools start Git only through `davellm_git.run_git`: a direct argument list, never a shell, with a from-scratch environment, no system or global config, no network protocols, no optional locks, fsmonitor, hooks, pager, credential helpers, or filters, and `--no-ext-diff --no-textconv` on every diff. Repositories are admitted only when the working tree, Git directory, common directory, and object store resolve inside a tool root; alternates are refused. Revisions follow a strict grammar and are resolved with `--end-of-options`; file paths go after `--` as literal pathspecs. Do not add Git writes, network operations, or argument passthrough. `tests/hostile_git.py` plants programs that must never run.
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

DaveHarness `1.0.0-rc.1` adds H8 offline qualification, bounded JSON admission, and Python 3.12–3.14 CI. See [H8 qualification](docs/DAVEHARNESS_H8_QUALIFICATION.md). This candidate does not establish live-model qualification or human acceptance. DaveLLM remains `2.1.0`.

H9 action 55 adds the authorized live-evaluation runner `scripts/evaluate_live_daveharness.py`, which runs DaveLLM's Ollama adapter and file/system tools inside disposable roots and reports the action 56 thresholds per model. See [H9 live evaluation](docs/DAVEHARNESS_H9_LIVE_EVALUATION.md). No target model is qualified yet, and DaveHarness remains `1.0.0-rc.1`.
