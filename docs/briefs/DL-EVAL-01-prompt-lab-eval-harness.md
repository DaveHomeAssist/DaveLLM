# DL-EVAL-01: Prompt Lab Library Tests against DaveLLM models

Research brief, 2026-10-06. Source revisions inspected: DaveLLM `origin/main` `7dd8a81` (local `main` `6aad6d6`, see Repository state), Prompt Lab `origin/main` `e984b6b` (local `main` `3c2fd10`). No live node, model, Notion page or hosted service was contacted; the only execution was a fake-router spike on 127.0.0.1 (see Spike).

## Problem

Prompt Lab's Library Tests run only against the provider configured in the app, and DaveLLM's `/chat` cannot be used for a clean evaluation: it prepends the DaveLLM persona and every call writes conversation history, embeddings, cost and performance logs. There is no way today to answer "how do the local models on the cluster do on my saved test cases" without contaminating production data or bypassing the router.

## Outcome

A standalone script in Prompt Lab runs selected Library Test cases through the real Enhance system prompt against several DaveLLM models in one go and reports, per model: pass rate, which expected phrases were missed, which banned phrases appeared, invalid-JSON replies, and speed. Scoring is Prompt Lab's own code, imported, so the script cannot drift from the app. DaveLLM gains one stateless endpoint. The Prompt Lab app, extension, site and hosted proxy do not change.

## What the code says (evidence)

| # | Finding | Evidence |
|---|---|---|
| F1 | A Library Test does not run the saved prompt. It builds the Enhance payload for the case input and scores the parsed `enhanced` string (falling back to raw text). | `prompt-lab-extension/src/hooks/useExecutionFlow.js:128-175` (`runTestCaseJob`), `promptUtils.js:399-434` (`checkTraits`) |
| F2 | Scoring is case-insensitive substring; a trait written `/.../` is a regex (invalid regex degrades to literal); at most 12 traits and 12 exclusions; verdict `pass`, `fail`, or `null` when a case has neither. | `promptUtils.js:399-434` |
| F3 | Enhance payload: model `claude-sonnet-4-6` (ignored by local providers), `max_tokens` 4096, `temperature` 0.4, `system` from `buildSystemPrompt(mode, ALL_TAGS)` (2,230 chars for `balanced`), one user message, `responseFormat: 'json'`. | `constants.js:5,22,23,86-96`, `useExecutionFlow.js:115-124` |
| F4 | Only the Gemini adapter honors `responseFormat`; the Ollama adapter sends `model`, `messages`, `options.num_predict`, `options.temperature` and no `format`. A local model that answers in prose fails `parseEnhancedPayload` and the case errors rather than scores. | `lib/providerRegistry.js:277-287, 390`, `promptUtils.js:308-360` |
| F5 | Test cases live in IndexedDB (`experimentStore`) as `{id, promptId, title, input, expectedTraits, expectedExclusions, notes}`; the workspace export/import carries `testCases[]` in that shape, which is the natural input file for a script. | `lib/evalSchema.js:89-107`, `experimentStore.js:269-311`, `lib/workspaceImport.js:30-46,198` |
| F6 | Latent shape mismatch: test cases embedded on a saved prompt use `name` and `exclusions`, and the Library inspector reads `exclusions`, while the runner and store use `title` and `expectedExclusions`. Not in scope here; recorded for Prompt Lab. | `lib/promptSchema.js:210-223`, `LibraryWorkspace.jsx:218` vs `evalSchema.js:99` |
| F7 | Every send runs the PII gate on the payload first; blocked cases are recorded, not sent. The script must do the same. | `useExecutionFlow.js:132-150`, `piiScanner.js:59` |
| F8 | `/chat` requires `conversation_id`, `node_id`, `model`; resolves instructions from the global default (`SYSTEM_PROMPT`, "You are DaveLLM...") plus project and session layers; then saves the conversation, cost log, embeddings, performance row, title and artifacts. The only per-request instruction path is the persisted session override. | `app.py:228-240, 2383-2500, 4624-4830` (local), `4587` on `origin/main` |
| F9 | Precedent for a stateless route: `POST /chat/size-check` (DL-ROUTE-05, in the unpushed local commit `be4d77f`, not yet on `origin/main`) creates and changes no conversation and calls no model. Same guard (`X-API-Key`, fail closed when `DAVE_API_KEY` is unset) and same inventory errors (404 node, 409 unloaded inventory, 400 model). | `app.py:4828-4895`, `app.py:484-500`, `INTEGRATION.md` |
| F10 | Transport to reuse: `ollama_chat(stream=False, timeout=node_complete_timeout(), total_timeout=NODE_TOTAL_TIMEOUT, num_predict, temperature, options=chat_node_options(...), keep_alive=...)`; the reply carries `metrics` (prompt and generation counts and durations) that `stream_stats()` already turns into the UI shape. Tool-call-only replies are detected by `tool_call_only_notice`. | `davellm_ollama.py:585-640, 92-142`, `app.py:2678-2730` |
| F11 | In-flight accounting: `/chat`, the stream, the summary and the tool loop count themselves on `NODE_ACTIVITY`; an eval call must too, or other users' `waiting` status under-reports the queue. | `app.py:4715` (`NODE_ACTIVITY.track`) |
| F12 | New `app.py` code must sit below the tool handlers because handler line numbers feed the tool fingerprints; `origin/main` `app.py` is 5,136 lines with `/chat` at 4587 and the static mount at 5087. Tests mock the node with `respx` on `TEST_NODE_URL/api/chat` through `router_factory`. | `CLAUDE.md`, `tests/conftest.py:95-120`, `tests/test_api_contracts.py:103` |
| F13 | Stale documentation: Prompt Lab `docs/PIPELINE.md` (router as "optional local super-provider", orchestrator on 8001), `docs/DECISIONS.md` D-003 (LAN IPs, 2026-03-29; the launcher now resolves nodes from Tailscale), `EXECUTION_PRD_V1.4.md:343-416` (DLM-1..3 "make DaveLLM speak Ollama's dialect", `/v1/embeddings`), and the Notion page SFT \| DaveLLM \| Local Cluster Architecture (lists an OpenAI-compatible `/v1/chat/completions` proxy and `GET /v1/models`). The router has no `/v1` routes; `tests/test_ollama_transport.py` pins that no `app.py` code posts to `/v1/chat/completions`. | files named; `app.py` route list |
| F14 | Prompt Lab's Ollama adapter sends only `Content-Type`, so pointing it at the router (the DLM-1..3 idea) cannot pass `X-API-Key` and would require the router to drop fail-closed auth. An in-app DaveLLM provider is therefore a separate provider module, not a base-URL swap. | `lib/providerRegistry.js:273-275` |
| F15 | The scoring modules import cleanly from plain Node (no Vite, browser or `chrome.*` globals on the path): `checkTraits`, `parseEnhancedPayload`, `buildSystemPrompt`, `ALL_TAGS`, `scanSensitiveData`. Verified with Node 25.8.1 on the Mac; the repo pins Node 22.x through `scripts/require-node.mjs`. | import probe, 2026-10-06 |
| F16 | Cluster lanes recorded 2026-09-25: walter is the fast GPU lane (llama3 8B about 81 tok/s), duncan the 120B lane (7 to 8.5 tok/s), dominic CPU-only (5.8 tok/s), max constrained. An Enhance reply is JSON up to 4,096 tokens, so one case on duncan can take minutes. | Notion SFT \| DaveLLM \| Local Cluster Architecture, `NODE_PROFILES` |

## Flow

1. The user exports the Prompt Lab workspace (or any JSON with `testCases[]` and optionally `library[]` for titles).
2. The script checks `GET /nodes` and `GET /nodes/{id}/models`, refusing a node or model that is not in the loaded inventory (the same rule the app enforces).
3. For each model, for each case: build the Enhance payload exactly as the app does, run the PII gate, `POST /eval/chat`, parse with `parseEnhancedPayload`, score with `checkTraits`, record latency and node stats.
4. Write `report.json` and `report.md` beside the input: per model, pass rate, missed phrases with counts, banned hits, invalid-JSON count, blocked count, errors, median latency.

## Smallest useful slice

- DaveLLM: `POST /eval/chat`, stateless. Request `{node_id, model, messages[], max_tokens?, temperature?, format?}` where `messages` is the full list including the caller's system message. The router validates the node and model against the loaded inventory with the `/chat` error codes, sends through `ollama_chat` with the same deadlines, `num_ctx` and `keep_alive` hooks as plain chat, and returns `{response, node, model, done_reason, stats, latency_ms}` plus `reason: "tool_call_only"` and `notice` when the reply had no visible content. `format: "json"` passes through to Ollama's `format` field; omitted otherwise.
- Prompt Lab: `prompt-lab-source/scripts/eval-davellm.mjs` as in the Flow, Node 22 through `require-node.mjs`, key from `DAVE_API_KEY`, no browser storage touched.
- Docs: `INTEGRATION.md` entry for the route; the stale notes in F13 corrected after the route exists.

## Exclusions

- Running the saved prompt itself as a Library Test (would redefine what a test means; later milestone).
- A DaveLLM provider inside the app, extension or hosted web shell (F14 makes it a new provider module; later milestone, and the hosted shell must stay cost-reviewed).
- OpenAI-compatible routes on the router, or calling Ollama directly (does not test the router).
- Any write by `/eval/chat`: no conversation, project, embedding, cost, performance, title, artifact or monitoring row. In-memory `NODE_ACTIVITY` only.
- Streaming, images, tools, project context and history on the eval route.
- Fixing F6 (test-case field mismatch) in Prompt Lab.

## Rule, data, API and persistence effects

- New protected route `POST /eval/chat`; no new persistence, environment variable or flag. Same auth and rate limit as `/chat`.
- `/eval/chat` does not charge the user budget and does not feed `track_model_failure`, so eval load cannot change routing advice or cost totals.
- Prompt Lab gains one script and no runtime change. The script reads a JSON file and writes two report files.

## Dependencies

Loaded model inventory on the router; `DAVE_API_KEY` in the caller's environment; reachable node over Tailscale for the live milestone; a workspace export with test cases.

## Risks

- Local models often ignore "return only JSON". Without `format: "json"` the invalid-JSON column will dominate; with it, Ollama constrains output but some models produce empty or truncated objects. The script reports both so the choice is measurable.
- A 4,096-token JSON reply at 7 tok/s is about ten minutes per case on duncan. The first live run must stay small (see D-EVAL-04).
- Reasoning models spend `max_tokens` on reasoning first. `/eval/chat` sends no `think` and returns no reasoning text, so a gpt-oss reply can stop at `done_reason: "length"` with cut-off JSON, which would look like a JSON-compliance failure when the budget ran out. Ollama applies the `format` constraint only after the reasoning ends, and that behavior varies by version (ollama/ollama#14645); walter's recorded version (0.33.3 on 2026-10-04) has not been checked with `format`.
- Plain `format: "json"` guarantees JSON, not an `enhanced` field. `parseEnhancedPayload` throws for a valid object without one, so the app scores nothing for it (corrected 2026-10-06; an earlier line here said such a reply reached the raw-text fallback, which the parser never allows). The runner counts these replies in their own column so they are not mistaken for prose.
- `/eval/chat` sends no `keep_alive`, so Ollama's default applies to eval calls; a model Dave is chatting with drops from the 30-minute plain-chat residency to that default.
- `/eval/chat` lets any key holder send an arbitrary system prompt to a node. That is the same trust `/chat` already grants through the session override; the route adds no new capability beyond skipping persistence.
- Prompt Lab local Node is 25.x; `npm run` scripts refuse anything but 22.x. Run the script with a Node 22 binary or let `require-node.mjs` report the mismatch.

## Spike (fake router, 2026-10-06)

Scratch files only, in the session scratchpad (`spike/fake_router.py`, `spike/eval-davellm.mjs`, `spike/cases.json`); nothing committed, no model called. A stdlib Python server stood in for the router with `/health`, `/nodes`, `/nodes/{id}/models` and the proposed `/eval/chat`, fail-closed on `X-API-Key`, with three canned models: one answering valid JSON with the expected phrases, one valid JSON missing phrases and containing a banned one, and one answering prose. The runner imported Prompt Lab's `checkTraits`, `parseEnhancedPayload`, `buildSystemPrompt`, `ALL_TAGS` and `scanSensitiveData` from the source tree.

| model | cases | pass | fail | blocked | invalid JSON | errors | pass rate | median ms | missed phrases | banned hits |
|---|---|---|---|---|---|---|---|---|---|---|
| good-model | 3 | 2 | 0 | 1 | 0 | 0 | 100% | 57 | - | - |
| weak-model | 3 | 0 | 2 | 1 | 0 | 0 | 0% | 57 | concise (1), /you are a/ (1), task (1) | I'm just an AI (1), filler (1) |
| broken-model | 3 | 0 | 0 | 1 | 2 | 0 | -% | 128 | - | - |

The blocked column is the PII gate catching the case with an email address and phone number before any send. Pass and fail accounting, missed-phrase and banned-phrase reporting, invalid-JSON separation and latency capture all work with the app's own functions. A wrong key is refused by the fake router; the spike runner does not yet turn that into a readable message (it must check the `/nodes` status before reading the body).

## Milestones

1. `/eval/chat` in DaveLLM with contract tests (mocked node: happy path, format passthrough, tool-call-only, each error code, no file in `DAVE_DATA_DIR` changes, `NODE_ACTIVITY` counted), `INTEGRATION.md` entry. Effort S. Done: merged in PR #69 (`4dc477c`), CI green on Python 3.12, 3.13 and 3.14.
2. `scripts/eval-davellm.mjs` in Prompt Lab with a `node --test` suite against a fixture router, plus an `eval:davellm` npm script. Effort S. Built 2026-10-06 in DaveHomeAssist/prompt-lab#131 (12 tests; an end-to-end run through this router with a fake Ollama node left `DAVE_DATA_DIR` byte-identical). Requirements from the 2026-10-06 readiness review of D-EVAL-04 A and D-EVAL-05 A, each covered by the suite:
   - Send `format: "json"` on every call unless a flag turns it off. The router's own default is unconstrained, so a runner that omits the field silently turns D-EVAL-05 A into B.
   - Split invalid JSON by the router's `done_reason`: `length` (the reply ran out of `max_tokens`) and `stop` (the reply finished but does not parse).
   - Score exactly as the app does, and count replies that are valid JSON without a non-empty `enhanced` string in their own column; the app's parser rejects them, so they get no verdict.
   - Record `done_reason` and `stats.gen_tokens` per case in `report.json`.
   - Head `report.md` with a note that replies were JSON-constrained, which the app's own Ollama adapter does not send (F4), so pass rates are not what the app's Ollama provider would get.
3. First live run per D-EVAL-04, results recorded in both projects' next-steps records. Effort S, Dave-gated: accepting D-EVAL-04 A fixed the run's shape and does not by itself authorize live inference. The models on record for walter are `llama3:latest` (8B) and `gpt-oss:20b`; re-read the inventory before dispatch. Worst case is about 40 minutes: llama3 at about 81 tok/s takes at most about 51 s per 4,096-token reply; gpt-oss:20b has no recorded walter speed, and qwen3-coder:30b's 21.56 tok/s on the same GPU (H9 readiness review) gives at most about 190 s. If a model's first case ends with `done_reason: "length"` or empty content, stop that model and report rather than finishing its pass.
4. Doc truth: correct F13 in Prompt Lab and the Notion architecture page. Effort XS. Partly done in prompt-lab#131: `docs/PIPELINE.md` no longer calls the router an in-app super-provider or the cluster blocked on LAN IPs. `docs/DECISIONS.md` D-003, `EXECUTION_PRD_V1.4.md` DLM-1..3 and the Notion page remain.

## Decisions

Accepted 2026-10-06 (Dave, "Go with your picks"):

- D-EVAL-01: Library Tests stay Enhance-only in v1.
- D-EVAL-02: stateless `/eval/chat` on the router; not OpenAI compatibility, not direct Ollama.
- D-EVAL-03: eval calls log nothing in v1.

Accepted 2026-10-06 (Dave, "complete all of these NOW", recommended options):

- D-EVAL-04: A. First live run on walter only, two models from its inventory (one 8B-class, one 20B-class), 10 cases, one pass, `max_tokens` 4,096. This sets the run's shape; the run itself stays Dave-gated (milestone 3).
- D-EVAL-05: A. Send `format: "json"` by default and report invalid-JSON counts. The runner sends it; `/eval/chat` leaves `format` out unless asked.

Options as originally presented (kept for the record):

- D-EVAL-04 (first live run). A: walter only, two models from its inventory (one 8B-class, one 20B-class), 10 cases, one pass, `max_tokens` kept at the app's 4,096 (recommended; about 20 calls, under an hour). B: add duncan with gpt-oss:120b for the same 10 cases (adds roughly an hour or more). C: cap `max_tokens` at 1,500 for the first run to halve time at the cost of parity with the app.
- D-EVAL-05 (JSON mode). A: send `format: "json"` by default and report invalid-JSON counts (recommended; matches the intent of `responseFormat: 'json'`). B: never constrain, measure raw compliance. C: run both and report side by side (doubles calls).

## Acceptance

- `pytest` and the Node suites pass; `DAVE_DATA_DIR` has no new or changed file after an `/eval/chat` call in tests.
- The script's report for a fixture router matches the verdicts the app records for the same cases through a fixture provider.
- A live run produces `report.md` with per-model pass rate, missed phrases and latency, and the router's conversation list, cost log and performance store are byte-identical before and after.

## Repository state found during research

- DaveLLM local `main` is 2 ahead and 18 behind `origin/main`: `be4d77f` (Stop button, size check, closable tool card) and `6aad6d6` (next-steps projection) were never pushed, and a 2026-10-05 `.agent-claim` asked that local `main` not be reset or rebased. This brief is on branch `claude/dl-eval-01-brief` from `origin/main`; local `main` is untouched and still needs Dave's push or rebase.
- Prompt Lab local `main` is 12 behind `origin/main` with a clean tree; the Library Tests tab (`a9a6a07`, PR #126) and PR #127/#130 are upstream only.
