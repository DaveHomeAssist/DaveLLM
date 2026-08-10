# DaveLLM Feature Analysis

Original analysis date: 2026-03-25

Source-aligned review: 2026-08-10 ET

Scope: current `origin/main` plus P0 stabilization, without cluster or hardware access

## Status definitions

- **Verified**: covered by repository tests or direct static/runtime inspection.
- **Implemented, runtime unknown**: code path exists, but real cluster assets were not probed.
- **Partial**: usable implementation exists with a known limitation.
- **Disabled by default**: implementation exists behind an explicit opt-in.

## Feature matrix

| Feature | Status | Evidence and boundary |
|---|---|---|
| FastAPI health and route surface | Verified | `/health` remains public; existing route names are preserved without an `/api` prefix. |
| Fail-closed API authentication | Verified | Tests cover missing server configuration (`503`), missing/wrong caller credential (`401`), and authenticated success. |
| Runtime static UI isolation | Verified | Only `static/` is mounted. Source, Git metadata, JSON, SQLite, and logs return `404`. |
| Electron credential boundary | Verified by source and contract test | Main process injects `X-API-Key` only for the exact loopback origin; preload and renderer contain no key. Full packaged-app inspection is not performed. |
| Electron backend readiness | Implemented, runtime unknown | Spawn error, early exit, `/health` timeout, and post-ready exit have explicit errors. GUI launch proof depends on the local Electron runtime. |
| Browser credential entry | Verified by source and contract test | Key is entered for the current browser session and stored only in `sessionStorage`. |
| Ollama node inventory | Verified with mocked HTTP transport | Router reads configured nodes and queries Ollama `/api/tags`; no model IDs are synthesized. Real inventory remains unknown. |
| Inventory-bound chat selection | Verified with mocked HTTP transport | Unknown nodes, unloaded inventories, and unavailable models are rejected. Valid node/model payload reaches `/v1/chat/completions`. |
| Automatic routing | Partial | Existing catalog remains advisory. The UI applies a suggestion only when it exists in the selected node's loaded inventory; valid manual selection is otherwise preserved. |
| Synchronous chat | Verified with mocked HTTP transport | Valid selection, payload, persistence, title generation, cost/performance paths, and response parsing are covered. |
| Streaming chat | Verified with mocked HTTP transport | Success tokens, terminal events, upstream failure events, persistence, and renderer error propagation are covered. |
| Exact displayed prompt | Verified | A shared prompt contract builds the displayed and transmitted text, including attached text and `[SUPPORT]`. |
| Persistent conversations and projects | Verified for current JSON semantics | Atomic JSON writes remain. Concurrent multi-process scaling and SQLite migration are deferred. |
| First-conversation title | Verified | First user exchange sets the title even when a template system message exists; existing titled conversations are preserved. |
| Vector memory indexes | Verified | Embeddings use persisted unpruned raw-history indexes, including histories beyond the pruning window. |
| Template conversations | Verified | Existing endpoint now accepts a Pydantic JSON body with `template_name` and optional `project_id`. No template CRUD was added. |
| Authenticated Markdown export | Verified | Protected endpoint and renderer Blob download are covered; object URLs are revoked. |
| Project CRUD and instruction resync | Implemented, runtime unknown | Routes and UI remain present. Core ownership logic is inspected; exhaustive project-flow UI testing is not included. |
| Image attachments and vision metadata | Partial | Upload payload and inventory metadata normalization remain. Actual multimodal inference depends on installed models. |
| Text-file attachment | Verified by prompt contract | File text and filename are included in the exact prompt. Browser file API behavior is not separately automated. |
| Audio transcription and dictation | Implemented, runtime unknown | Browser speech, MediaRecorder upload, ffmpeg, and whisper.cpp paths remain. Binaries/assets were not inspected or executed. |
| Monitoring page | Verified by source and contract test | Auth uses the same session/main-process path; dynamic content uses explicit DOM construction. Live metrics depend on nodes. |
| Search and memory rendering | Verified by source and contract test | User-controlled `innerHTML` was removed from search, memory, node, model, project, and monitoring paths. |
| Model downloader | Partial | Existing Hugging Face-only downloader remains protected. Network download and large-file behavior were not exercised. |
| Tool endpoints | Disabled by default | `/tools` and `/tools/execute` return `403` unless explicitly enabled. |
| File tools | Verified when enabled | Read/write/append share `Path.is_relative_to` containment against absolute `DAVE_TOOL_ROOTS`. |
| Shell tool | Disabled by default | Requires both general tool enablement and separate `DAVE_ENABLE_SHELL_TOOL=true`. |
| `web.fetch` | Verified for loopback rejection; implementation inspected | DNS and redirect targets are validated, automatic redirects are disabled, and redirect/response bounds are enforced. Public-network success was not exercised. |
| Data-directory isolation | Verified | Conversations, projects, vectors, feedback, performance, and cost log all resolve under `DAVE_DATA_DIR`. Existing data was not moved. |
| Electron dependency | Constrained risk | `package.json` remains at `^30.0.0`; lockfile resolves 30.5.1. Current npm audit findings require a major upgrade, which is a separate decision. |

## Operational implications

The UI cannot chat until it has successfully loaded a node's real Ollama model inventory. This removes silent fallbacks and invented aliases, but it makes cluster configuration and reachability an explicit prerequisite. A route recommendation that does not match current inventory is informational only.

Authentication now has separate server and client failure states. `/health` can prove process readiness without disclosing protected data. Electron credentials stay outside renderer state; browser credentials end with the tab session.

Serving only `static/` removes the prior possibility of exposing repository source or runtime data through the root static mount. Runtime data placement is predictable under `DAVE_DATA_DIR`, but no legacy data has been imported.

## Remaining decisions and unknowns

1. **Electron major upgrade:** required to clear current audit findings; compatibility work is not part of this P0 package.
2. **Real cluster validation:** node reachability, installed models, stream behavior, latency, and multimodal capability remain unknown without authorized network access.
3. **Whisper validation:** ffmpeg, whisper.cpp binary, model asset, and transcription quality remain unknown.
4. **Persistence migration:** JSON conversation/project storage is retained. SQLite migration remains deferred.
5. **Routing quality:** safety is inventory-bound, but ranking quality still depends on the existing static catalog and feedback heuristics.
