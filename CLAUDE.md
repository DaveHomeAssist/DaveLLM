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
- The static mount (`RevalidatedStaticFiles` in `app.py`) sends `Cache-Control: no-cache` on every file and 304, so a browser or Electron tab revalidates `app.js` and the other fixed-URL assets on each load instead of running a stale bundle against a newer router. API routes keep their own headers; do not widen it to them.
- Tools default off in the router; the macOS launcher sets `DAVE_ENABLE_TOOLS=true` and `DAVE_ENABLE_EXTENDED_TOOLS=true` (both overridable) and sets `DAVE_SEARCH_URL` only when the SearXNG on Dominic answers `/healthz`. File tools require explicit absolute roots. Shell execution requires a second opt-in, which the launcher never sets.
- `DAVE_ENABLE_EXTENDED_TOOLS` is a separate opt-in, honored only when `DAVE_ENABLE_TOOLS` is also on, that adds exactly `file.list`, `file.search`, `file.read_lines`, `md.outline`, `md.section`, `git.status`, `git.diff`, `git.log`, `git.show`, `project.notepad.read`, `project.brain.read`, `project.artifacts`, `chat.search`, `cluster.status`, `file.edit`, `web.search`, and `web.read`. All but `file.edit` are read-only, need no approval, and are bounded; the native tools use `read`, or `read_system` for `cluster.status`, and the web tools use `public_network`. `file.edit` is the only extended tool that writes: `write_files`, exact-call approval on every call, bounded. With it off, the qualified catalog and its definition fingerprints are unchanged. The file tools live in `davellm_files.py`; the Markdown tools live in `davellm_markdown.py`, share the one heading parser there, and reuse the `davellm_files.py` path, read, and budget helpers rather than their own. The Git tools live in `davellm_git.py`; the native read tools live in `davellm_native_tools.py`; `file.edit` lives in `davellm_edit.py`; `web.search` and `web.read` live in `davellm_web.py`. Keep new `app.py` code below the existing tool handlers, qualified and extended, because handler line numbers are part of the fingerprints (`tests/test_tool_catalog_provenance.py` pins them and the extended definitions). The tool catalog is `docs/DAVELLM_TOOLS.md`. `docs/DAVEHARNESS_CAPABILITIES.json` and `docs/DAVEHARNESS_CAPABILITIES.md` are generated from the registry by `scripts/generate_capabilities_manifest.py`; never edit them by hand, and rerun the script after changing a tool, flag, budget, host limit, `/tools` route, or tool-module constant (`tests/test_capabilities_manifest.py` fails when they are stale).
- Public web tools must connect through `davellm_public_http`: every redirect resolves and validates all DNS answers, then connects only to those numeric addresses with the original Host and TLS identity. Environment proxies and connection reuse are disabled for these requests. Never restore an independent hostname lookup between validation and connection.
- Extended tools admit every path through `resolve_extended_tool_path`, which anchors a relative path at the single tool root (refusing it when several roots are configured) and wraps `resolve_tool_path` with the secret denylist: `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `*.p12`, `.ssh`, `.aws`, and `.gnupg`, matched case-insensitively against every path component before and after symlink resolution. Every refusal, including a root escape, raises the same `PathNotAllowed` message. `walk_tree` never lists, enters, or counts secrets and omits symlinks that leave the root. The qualified `file.read` does not apply the denylist; extending it is a separate decision. New file tools must pass the shared contract in `tests/tool_contract.py` against the `hostile_tree` fixture.
- Extended tools read file contents only through `read_admitted_bytes`, which opens each path component from `/` relative to its parent descriptor with `O_NOFOLLOW` (or opens and re-verifies inode and path where descriptor walks are unavailable), so a symlink swapped in after admission fails closed. Results stay under 48 KiB, show only root-relative paths, and never carry OS error text. `file.search` is literal only; do not add regex without a bounded engine or process boundary.
- Git tools start Git only through `davellm_git.run_git`: a direct argument list, never a shell, with a from-scratch environment, no system or global config, no network protocols, no optional locks, fsmonitor, hooks, pager, credential helpers, or filters, and `--no-ext-diff --no-textconv` on every diff. Repositories are admitted only when the working tree, Git directory, common directory, and object store resolve inside a tool root (any other location answers "Not a Git working tree", never a different message), the Git directories hold no symlink or special file outside `hooks` (checked by a bounded walk that never follows links), and `objects/info/alternates` is missing or holds only comments and empty lines (read through `read_admitted_bytes`, never pathlib); alternates are refused. Revisions follow a strict grammar and are resolved with `--end-of-options`; file paths go after `--` as literal pathspecs. Do not add Git writes, network operations, or argument passthrough. `tests/hostile_git.py` plants programs that must never run.
- `file.edit` replaces exact text only when `old_text` occurs exactly `expected_count` times, and writes nothing otherwise. Occurrences are counted overlapping ones included (`occurrences` in `davellm_edit.py`, never `str.count`, which skips them), the scan stops past `EDIT_MAX_REPLACEMENTS`, and occurrences that overlap are refused even when the count matches. It admits the path through `resolve_extended_tool_path`, edits only existing regular UTF-8 files, and never creates files or folders. It refuses files with more than one hard link. It writes a temporary file (`O_CREAT|O_EXCL|O_NOFOLLOW`) in a parent folder opened by descriptor walk, then re-reads the target and compares device, inode, and bytes just before a descriptor-relative rename. If anything changed, it writes nothing and removes the temporary file. It keeps CRLF files CRLF and keeps a byte order mark. The approval card preview (`approvalPreview` in `static/app.js`) builds the before and after text with `createElement` and `textContent` only; `tests/test_approval_preview.mjs` pins that.
- Native read tools take user, project, and BRAIN revision only from `HOST_RUN_CONTEXT`; no schema may accept a user, project, owner, or node. Project reads repeat `get_project`'s route ownership check, `project.brain.read` reads the run's captured revision and verifies its digest (never the latest), `chat.search` reuses `search_conversation_messages` limited to the run user's user and assistant messages, and `cluster.status` never returns node URLs, addresses, credentials, or error text.
- An approval-required `ToolDefinition` may set `preflight(arguments, context) -> str | None` ([contract](docs/DAVEHARNESS_PREFLIGHT.md)). DaveHarness runs it after schema validation and before the approval pause; a nonempty string refuses the call as status `error`, termination `denied`, with no pending call. Anything else, an exception, or a timeout pauses as before, so a preflight can spare an approval but never authorize an effect, and handlers must still check everything after approval. A preflight must only read. Its provenance is in the fingerprint only when set, so definitions without one keep their fingerprints; keep preflights below every tool handler in `app.py` (`extended_preflight_code` in the catalog fixture pins them). `file.edit` uses `preflight_file_edit` → `davellm_edit.check_edit`.
- The agent loop defaults to eight model steps, returns partial transcripts, and requires exact-call approval for mutating or execution tools. Pending calls use canonical arguments, a SHA-256 digest, transcript revision, single-use nonce, and a 300-second in-memory expiry.
- `web.search` queries only the operator-configured `DAVE_SEARCH_URL` (a SearXNG instance, usually on the tailnet), so it skips the public-address check; the model never chooses that address. `web.read` repeats `web.fetch`'s rules (public-address check on every redirect hop, redirect limit, byte cap), accepts only HTML and text content types, and returns visible text rather than markup. Neither changes `web.fetch`, which stays a qualified built-in. Only `WebToolError` messages reach the model.
- First-party tool handlers are synchronous except for explicitly opted-in, registry-allowlisted `web.fetch`, `web.search`, `web.read`, `notion.page.read`, `notion.page.append`, and `notion.block.update`. Every tool definition declares `cancellation` as `bounded` or `abandon`; approval-required and write tools must be `bounded`.
- `DAVE_ENABLE_NOTION_TOOLS` is a separate opt-in, honored only with `DAVE_ENABLE_TOOLS`, that adds exactly `notion.page.read` (`read`), `notion.page.append`, and `notion.block.update` (`write`, exact-call approval on every call, bounded). They live in `davellm_notion.py`; their `app.py` handlers sit below the web handlers. The secret comes only from `DAVE_NOTION_TOKEN` in the environment; the launcher does not store or set it, and it must never reach a result, error, log, `repr`, or the capabilities manifest. Pages come only from `DAVE_NOTION_PAGES` (configured name to page ID); the model names pages by configured name and blocks by run refs, never by Notion ID. Refs live in a per-run ledger keyed by `HostRunBinding.run_id` and dropped with the run, so the tools refuse outside lifecycle runs. Before every write the adapter re-reads the block, walks its parents to the configured page, and compares the content the run saw; text edits replace inside one rich text run and send every other run back unchanged. Writes report `verified`, `failed`, or `unknown`; an unknown append is never repeated in the run, and the write plus its check run shielded from cancellation under a per-page in-process guard. Transport is fixed to `https://api.notion.com/v1` with a pinned `Notion-Version`, no redirects, no environment proxies, and a response cap; only fixed messages and Notion error codes reach the model.
- Preserve existing tool status strings. The additive result `termination` is `completed`, `deadline_abandoned`, `denied`, or `error`. `deadline_abandoned` means DaveHarness stopped waiting and does not prove an underlying synchronous worker stopped.
- `POST /tools/agent/resume` must execute only stored canonical arguments for the matching run, call, digest, transcript revision, unexpired nonce, and current registry definition. It must not replay the paused model step or reuse a consumed decision.
- Global, project, and session instruction layers are visible in the UI and resolve into one exact primary system message.
- Project notepads are plain text in project persistence. Do not add rich text, history, collaboration, or browser note storage.
- Replies render through `appendMarkdown` in `static/console.js`, which builds DOM nodes and text only (headings, lists, tables, quotes, rules, code, emphasis, links). Only `http(s)` and `mailto` links become anchors, and `desktop/main.js` opens them in the default browser and denies other windows and navigation. `tests/test_markdown_render.mjs` pins the output.
- The sidebar's sort, compact view and unread marks live in this browser's `localStorage` (`dave_convo_sort`, `dave_convo_density`, `dave_unread_conversations`). Unread holds conversation IDs only, never message content.
- Existing-chat project changes must use the explicit attachment endpoint and apply only to future messages.
- DaveLLM and DaveHarness are separate product concepts with one dependency direction: DaveLLM consumes DaveHarness through the in-process `daveharness` package. DaveHarness owns generic registry, validation, permission, approval, budget, terminal-state, and transcript contracts; it must not directly own DaveLLM HTTP, Ollama inventory, persistence, project context, UI, environment, or product-specific tool implementations. Preserve `tool_executor.py` only as the legacy compatibility import.
- Do not create a DaveHarness network service or separate repository without a second production consumer, independent deployment cadence, incompatible dependency requirement, or required remote execution boundary.
- Keep the default pending-call store in-process. Do not move approval state into DaveLLM persistence or claim process-restart durability without a separately approved lifecycle design.
- Root `VERSION` is the DaveLLM release-version authority. FastAPI metadata, startup output, `GET /health`, `package.json`, and root lockfile metadata must match it.

## Ollama integration

`DAVE_NODES` is the only runtime node inventory override. Do not add real addresses or fabricate aliases in source. Model discovery uses `/api/tags`. A chat must name a node returned by `/nodes` and a model returned for that node. Automatic routing is advisory and may replace a manual model only when the suggestion exists in the currently loaded inventory.

Inference transport (DL-TRANSPORT-01a):

- Plain chat (`chat`, `chat_stream`, `generate_conversation_summary`) uses native `POST /api/chat` through `davellm_ollama.ollama_chat`. The SSE token, notice, error and terminal events, `/chat` JSON, persistence, and error strings are unchanged; the contract tests pin them. DL-UX-01 adds `status` events (`waiting` at the start, `thinking` once when reasoning arrives before any token) and one `stats` event before a successful terminal event (`stream_stats()` from the done-line metrics plus `ttft_s`); the renderer keeps the stats in memory only. DL-ROUTE-01/02: `davellm_node_profiles.py` reads the optional `profile` from each `DAVE_NODES` entry into `NODE_PROFILES`, outside `NodeConfig`, which sits above the pinned tool handlers. `GET /nodes` adds it through `node_listing()`, and `prompt_size_warning()` adds `prompt_tokens` and `prompt_token_limit` to the stream's `waiting` status when the estimated prompt is over the limit. The limits are advisory and never reroute. DL-ROUTE-03/04: `waiting_status()` builds that first event. It adds `model_loaded` from `ollama_loaded_models()` (`GET /api/ps`, 2 s, never raises) and `others_in_flight` from `NODE_ACTIVITY`. `NODE_ACTIVITY` is a `NodeActivity` keyed by node URL, and the stream, `/chat`, the summary and both tool-loop invokers count themselves on it; the stream releases in a `finally` that also covers client disconnects. `NodeActivity` also records each node's last prompt key (`chat_prompt_key()`: model plus conversation, `None` for project chats, conversations past the 10-message history window, summaries and tool runs). A warm follow-up (same key, model loaded, nothing queued) counts only its new message toward the prompt limit, because Ollama reuses the cached prefix. Deadlines (DL-TIME-01) come from `node_complete_timeout()` (non-streaming chat and the tool loop: `DAVE_NODE_TOTAL_TIMEOUT` 600 s, connect `DAVE_NODE_CONNECT_TIMEOUT` 10 s) and `node_stream_timeout()` plus `OllamaChatStream`'s per-line `first_chunk_timeout` (`DAVE_NODE_FIRST_CHUNK_TIMEOUT` 300 s from the request start) and `idle_timeout` (`DAVE_NODE_IDLE_TIMEOUT` 120 s between lines). httpx timeouts are per operation, so the total is enforced as a wall clock: `ollama_chat_complete(total_timeout=...)` runs cancellable async I/O under one `asyncio.wait_for` deadline covering headers and the entire body on `/chat` (the public helper stays synchronous), and the tool loop passes `NODE_TOTAL_TIMEOUT` as the DaveHarness model-step timeout (its `asyncio.wait_for`; the harness default is 120 s). A streamed non-2xx error body is read within the first-chunk budget. An expired deadline raises `httpx.ReadTimeout`, so callers keep reporting `Node timed out`. The summary keeps its 10 s budget.
- `max_tokens` and `temperature` travel as `options.num_predict` / `options.temperature` (`chat_temperature` keeps the `/v1` substitution of 1.0 for an explicit null). Extra `options` such as `num_ctx`, and `keep_alive`, come only from the `chat_node_options` / `chat_keep_alive` hooks in `app.py`. DL-CTX-01: `chat_node_options` sends `num_ctx = chat_num_ctx(model)`, the model's configured window capped at `DAVE_CHAT_NUM_CTX` (default 16384), and the plain-chat and preview call sites pass the same number as `window` to `build_project_messages_for_node`, so the router never assembles more than it asked Ollama to hold. Agent runs omit `window` and keep `get_model_context_window`, because their `/v1` requests send no `num_ctx`. `DAVE_CHAT_NUM_CTX` below `CHAT_NUM_CTX_FLOOR` (8192) is raised to it. DL-KEEP-01: `chat_keep_alive` sends `DAVE_CHAT_KEEP_ALIVE` (default `30m`) for chats in a conversation and nothing for the summary; an empty value sends nothing. Every plain-chat site, including the summary, goes through those hooks so one model never alternates `num_ctx` (a Runner-level change forces a model reload).
- The helper exposes `message.thinking` and the done-line metrics; nothing forwards them to the UI yet (DL-UX-01). An in-band mid-stream `{"error": ...}` line keeps the partial reply and is recorded as `stream_node_error` in `GET /monitoring/health`; see `INTEGRATION.md`.
- It also surfaces `message.tool_calls` (chunks, results, and the stream's accumulated `tool_calls`) but never runs them. A reply with blank content and a tool call is "tool-call-only": `tool_call_only_notice` in `app.py` builds the notice, `chat` and `chat_stream` persist no assistant message, cost, embedding, artifact, or title for it (the user turn stays, as after a node error), `/chat` returns the notice with `reason: "tool_call_only"`, and the stream emits one `notice` event before the unchanged terminal event. Never persist an empty assistant turn: the model reads it back and repeats it. Replies with visible content keep the byte-identical contract; the contract tests pin both.
- The tool loop (`invoke_harness_model`, `run_agent_endpoint`) also uses native `/api/chat` through `davellm_ollama.ollama_chat_bounded` (DL-TRANSPORT-01b): tool schemas in `tools`, `num_ctx = chat_num_ctx(model)`, `keep_alive = CHAT_KEEP_ALIVE`, and a `MAX_HARNESS_MODEL_RESPONSE_BYTES` read cap. It returns Ollama's native reply unchanged; DaveHarness reads the top-level `message` and native tool calls (which carry an `id` and object `arguments`). `native_tool_messages` converts only the two fields DaveHarness writes back OpenAI-style: string `arguments` (native rejects them with HTTP 400) and a tool result's `name`, copied to `tool_name`. `create_agent_run` budgets with the same `chat_num_ctx`. No `app.py` code may post to `/v1/chat/completions` (`tests/test_ollama_transport.py` pins it); `scripts/qualify_live_daveharness.py` keeps its own `/v1` client for library-level qualification.
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

GitHub Actions runs these checks on every push to `main` and every pull request via `.github/workflows/ci.yml`. Branch protection on `main` (since 2026-10-01) requires `checks (3.12)`, `checks (3.13)` and `checks (3.14)` to pass before a pull request merges; administrators are not enforced and can bypass it.

Required checks after relevant changes:

```bash
python -m py_compile app.py project_context.py davellm_shell.py davellm_files.py davellm_markdown.py davellm_git.py davellm_native_tools.py davellm_edit.py davellm_ollama.py davellm_web.py davellm_public_http.py davellm_node_profiles.py davellm_notion.py davellm_toolpack.py davellm_toolpack_catalog.py davellm_toolpack_cluster.py davellm_toolpack_http.py davellm_toolpack_jobs.py davellm_toolpack_local.py davellm_toolpack_notion.py davellm_toolpack_runner.py scripts/tool_job_runner.py scripts/project_context_cli.py tool_executor.py
python -m compileall -q daveharness
python -m mypy daveharness
python -m pytest -q
node --check static/app.js static/anticipation.js static/prompt-contract.js static/vendor/gsap/gsap.min.js desktop/main.js desktop/preload.js
node --test tests/test_run_ledger_watch.mjs tests/test_approval_preview.mjs tests/test_console.mjs tests/test_markdown_render.mjs
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
