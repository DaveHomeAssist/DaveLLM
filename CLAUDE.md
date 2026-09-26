# DaveLLM Router

## Product

DaveLLM is a FastAPI router with an Electron and browser UI for authenticated chat against configured Ollama nodes. The repository preserves conversation and core project JSON semantics, normalized SQLite project context plus vector/feedback/performance stores, streamed chat, attachments, export, monitoring, and optional local tools.

## Source layout

- `app.py`: FastAPI routes, persistence, inventory, chat, tools, monitoring
- `project_context.py`: normalized Project Homepage storage, BRAIN revisions, file/artifact retrieval, and bounded request assembly
- `daveharness/`: headless in-process `1.0.0-rc.1` library for typed leaf contracts, versioned serialization, policy, budgets, registry, schema validation, timing, exact-call approval/resume, and bounded executor loop
- DaveHarness `0.2.0` was the execution-semantics milestone; `0.3.0` added the internal module split and leaf-contract envelope; `0.4.0` added policy, fingerprints, and budgets; `0.5.0` adds snapshot and exact decision operations without changing DaveLLM endpoints. `0.6.0` adds opt-in cooperative cancellation and absolute deadlines; `0.7.0` adds metadata-only ordered events; `0.8.0` adds a bounded store and instance-owned facade; `0.9.0` integrates the DaveLLM lifecycle and frozen BRAIN run context.
- `davellm_ollama.py`: native Ollama `POST /api/chat` transport for plain chat (payload, per-message images, NDJSON parsing, metrics); the tool loop does not use it
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
- `DAVE_ENABLE_EXTENDED_TOOLS` is a separate opt-in, honored only when `DAVE_ENABLE_TOOLS` is also on, that adds exactly `file.list`, `file.search`, `file.read_lines`, `md.outline`, `md.section`, `git.status`, `git.diff`, `git.log`, `git.show`, `project.notepad.read`, `project.brain.read`, `project.artifacts`, `chat.search`, `cluster.status`, and `file.edit`. All but `file.edit` are read-only, need no approval, and are bounded; the native tools use `read`, or `read_system` for `cluster.status`. `file.edit` is the only extended tool that writes: `write_files`, exact-call approval on every call, bounded. With it off, the qualified catalog and its definition fingerprints are unchanged. The file tools live in `davellm_files.py`; the Markdown tools live in `davellm_markdown.py`, share the one heading parser there, and reuse the `davellm_files.py` path, read, and budget helpers rather than their own. The Git tools live in `davellm_git.py`; the native read tools live in `davellm_native_tools.py`; `file.edit` lives in `davellm_edit.py`. Keep new `app.py` code below the existing tool handlers, qualified and extended, because handler line numbers are part of the fingerprints (`tests/test_tool_catalog_provenance.py` pins them and the extended definitions). The tool catalog is `docs/DAVELLM_TOOLS.md`. `docs/DAVEHARNESS_CAPABILITIES.json` and `docs/DAVEHARNESS_CAPABILITIES.md` are generated from the registry by `scripts/generate_capabilities_manifest.py`; never edit them by hand, and rerun the script after changing a tool, flag, budget, host limit, `/tools` route, or tool-module constant (`tests/test_capabilities_manifest.py` fails when they are stale).
- Extended tools admit every path through `resolve_extended_tool_path`, which anchors a relative path at the single tool root (refusing it when several roots are configured) and wraps `resolve_tool_path` with the secret denylist: `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `*.p12`, `.ssh`, `.aws`, and `.gnupg`, matched case-insensitively against every path component before and after symlink resolution. Every refusal, including a root escape, raises the same `PathNotAllowed` message. `walk_tree` never lists, enters, or counts secrets and omits symlinks that leave the root. The qualified `file.read` does not apply the denylist; extending it is a separate decision. New file tools must pass the shared contract in `tests/tool_contract.py` against the `hostile_tree` fixture.
- Extended tools read file contents only through `read_admitted_bytes`, which opens each path component from `/` relative to its parent descriptor with `O_NOFOLLOW` (or opens and re-verifies inode and path where descriptor walks are unavailable), so a symlink swapped in after admission fails closed. Results stay under 48 KiB, show only root-relative paths, and never carry OS error text. `file.search` is literal only; do not add regex without a bounded engine or process boundary.
- Git tools start Git only through `davellm_git.run_git`: a direct argument list, never a shell, with a from-scratch environment, no system or global config, no network protocols, no optional locks, fsmonitor, hooks, pager, credential helpers, or filters, and `--no-ext-diff --no-textconv` on every diff. Repositories are admitted only when the working tree, Git directory, common directory, and object store resolve inside a tool root (any other location answers "Not a Git working tree", never a different message), the Git directories hold no symlink or special file outside `hooks` (checked by a bounded walk that never follows links), and `objects/info/alternates` is missing or holds only comments and empty lines (read through `read_admitted_bytes`, never pathlib); alternates are refused. Revisions follow a strict grammar and are resolved with `--end-of-options`; file paths go after `--` as literal pathspecs. Do not add Git writes, network operations, or argument passthrough. `tests/hostile_git.py` plants programs that must never run.
- `file.edit` replaces exact text only when `old_text` occurs exactly `expected_count` times, and writes nothing otherwise. It admits the path through `resolve_extended_tool_path`, edits only existing regular UTF-8 files, and never creates files or folders. It refuses files with more than one hard link. It writes a temporary file (`O_CREAT|O_EXCL|O_NOFOLLOW`) in a parent folder opened by descriptor walk, then re-reads the target and compares device, inode, and bytes just before a descriptor-relative rename. If anything changed, it writes nothing and removes the temporary file. It keeps CRLF files CRLF and keeps a byte order mark. The approval card preview (`approvalPreview` in `static/app.js`) builds the before and after text with `createElement` and `textContent` only; `tests/test_approval_preview.mjs` pins that.
- Native read tools take user, project, and BRAIN revision only from `HOST_RUN_CONTEXT`; no schema may accept a user, project, owner, or node. Project reads repeat `get_project`'s route ownership check, `project.brain.read` reads the run's captured revision and verifies its digest (never the latest), `chat.search` reuses `search_conversation_messages` limited to the run user's user and assistant messages, and `cluster.status` never returns node URLs, addresses, credentials, or error text.
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

`DAVE_NODES` is the only runtime node inventory override. Do not add real addresses or fabricate aliases in source. Model discovery uses `/api/tags`. A chat must name a node returned by `/nodes` and a model returned for that node. Automatic routing is advisory and may replace a manual model only when the suggestion exists in the currently loaded inventory.

Inference transport (DL-TRANSPORT-01a):

- Plain chat (`chat`, `chat_stream`, `generate_conversation_summary`) uses native `POST /api/chat` through `davellm_ollama.ollama_chat`. The SSE events, `/chat` JSON, persistence, error events, and the 120 s chat / 10 s summary timeouts are unchanged; the contract tests pin them.
- `max_tokens` and `temperature` travel as `options.num_predict` / `options.temperature` (`chat_temperature` keeps the `/v1` substitution of 1.0 for an explicit null). Extra `options` such as `num_ctx`, and `keep_alive`, come only from the `chat_node_options` / `chat_keep_alive` hooks in `app.py`, which return `None` in 01a. Every plain-chat site, including the summary, goes through those hooks so one model never alternates `num_ctx` (a Runner-level change forces a model reload).
- The helper exposes `message.thinking` and the done-line metrics; nothing forwards them to the UI yet (DL-UX-01). An in-band mid-stream `{"error": ...}` line keeps the partial reply and is recorded as `stream_node_error` in `GET /monitoring/health`; see `INTEGRATION.md`.
- The tool loop (`invoke_harness_model`, `run_agent_endpoint`) stays on `/v1/chat/completions` until DL-TRANSPORT-01b because it sends tool schemas and reads OpenAI-shaped `tool_calls`. Do not route it through the native helper, and do not add `num_ctx` or `keep_alive` to a `/v1` request: that endpoint ignores both.
- Sampling parity: `/v1` sent `top_p` 1.0, so the native payload sends `top_p` 1.0 as an overridable baseline (pinned by contract tests).

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
python -m py_compile app.py project_context.py davellm_shell.py davellm_files.py davellm_markdown.py davellm_git.py davellm_native_tools.py davellm_edit.py davellm_ollama.py scripts/project_context_cli.py tool_executor.py
python -m compileall -q daveharness
python -m mypy daveharness
python -m pytest -q
node --check static/app.js static/anticipation.js static/prompt-contract.js static/vendor/gsap/gsap.min.js desktop/main.js desktop/preload.js
node --test tests/test_run_ledger_watch.mjs tests/test_approval_preview.mjs
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

## Status naming

Name work with one string everywhere (chat status title, session title, Notion
Status Check Runs "Human Name"):

`Project | 🚦 | Phase | Title → state, reason | MM-DD`

- 🚦: 🟢 complete and verified · 🟡 partial · 🔴 not started, blocked or failed · ⚪ unverifiable.
  Add ⏳ scheduled, 🙋 awaiting Dave or 🚧 blocked to 🟡/🔴/⚪, never to 🟢.
- Phase: Research, Design, Build, Audit or Scheduled. MM-DD: date of the latest light change.
- Every light change gets a new name: a `RENAME:` line in chat and the Notion row updated.
- Canonical source: https://github.com/DaveHomeAssist/skills/blob/master/status-naming.md
