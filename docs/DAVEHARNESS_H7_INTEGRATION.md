# DaveHarness 0.9.0 DaveLLM integration

DaveHarness remains an in-process library. DaveLLM owns authentication, node and model inventory, Ollama transport, project context, tool roots, the run ledger, and the process-backed shell runner. `VERSION` remains DaveLLM `2.1.0`; this phase prepares a future `2.2.0` capability and does not publish or qualify a release.

## Run context boundary

`POST /tools/agent/runs` validates the selected node and model, then assembles the complete model message list once. For a project run, `ProjectContextStore.capture_run_context` reads BRAIN once and binds its revision to that exact BRAIN text during assembly. DaveLLM records the project ID, revision, content digest, and context budget beside the run. DaveHarness receives only canonical message JSON and generic limits. The stored transcript is the immutable starting message snapshot; later BRAIN edits, compaction, restore, deletion, or revision reset cannot change it. The digest disambiguates revisions after purge. Each model step and approval resume uses the captured run binding and transcript; it does not reread project context.

## Additive HTTP contract

All endpoints require the existing `X-API-Key` guard and return `403` while `DAVE_ENABLE_TOOLS` is off. `DAVE_ENABLE_SHELL_TOOL` separately controls `shell.exec`; shell file operands must stay within an explicit `DAVE_TOOL_ROOTS` root.

| Endpoint | Purpose |
|---|---|
| `POST /tools/agent/runs` | Start a bounded run with `messages`, `node_id`, `model`, optional `project_id` and `conversation_id`, and optional model and run limits. A conversation must match its attached project; its current instruction layers are captured. Returns run ID, status, snapshot, and context identity. |
| `GET /tools/agent/runs/{run_id}` | Read status, partial transcript, exact pending call, and terminal reason. Unknown runs return `404`; expired runs return `410`. |
| `GET /tools/agent/runs/{run_id}/events` | Read metadata-only ordered events after the `after` cursor. `stream=true` yields authenticated SSE with `id`, `event`, and `data` fields. `Last-Event-ID` also resumes replay. |
| `POST /tools/agent/runs/{run_id}/decisions` | Approve once or reject the exact pending call using call ID, digest, definition fingerprint, permission, and nonce. Mismatches and replay return `409`. |
| `POST /tools/agent/runs/{run_id}/cancel` | Request cancellation, returning the winning terminal or cancellation state. |

The run ledger appears in the desktop and browser UI only when `/tools` is available. It shows ordered states, pending call and permission, Approve once, Reject, Stop, partial assistant/tool transcript, and terminal reason. It uses authenticated `fetch` for SSE rather than `EventSource`, which cannot carry the existing header. The ledger is in process and expires with the bounded run store; restart requires a new run. The existing `/tools`, `/tools/execute`, `/tools/agent/run`, and `/tools/agent/resume` routes retain their compatibility implementation and stable response fields.

Admission allows at most four active runs and 32 retained run records. A new run is rejected when the ledger reaches its count or byte ceiling, so creating another run cannot evict an active record. Input and model responses are each limited to one million serialized bytes; the default run wall budget is five minutes. The host store has a 256 MiB ceiling and a four million byte admission reserve for the next snapshot.

An operator may now shorten that per-run wall budget with `total_wall_seconds`
(0.1–300, default 300) and opt into a `max_input_tokens` estimate ceiling checked
before **every** provider request, including after tool results accumulate. The
initial limit fails with HTTP 413; a later refusal is a labelled harness report,
not an invented assistant message. Additive `limits` and content-free
`model_usage` fields expose the limits and provider usage when available. See
[tool run limits](TOOL_RUN_LIMITS.md) for the contract, cancellation limitations
and fixture acceptance. These fields do not change generic harness schemas or
normal caller defaults.

The lifecycle facade uses its own host registry snapshot so `shell.exec` can opt into a stoppable process runner while legacy routes keep their handler contract. Shell commands are parsed without a shell, restricted to the safe command list, resolved into a configured root, and run in a process group. Cancellation acknowledges only after the process group exits. On Linux, a killed member that is a zombie waiting for a slow reaper, such as a container PID 1, counts as exited because it can never run again; elsewhere, the group must be gone. Model and tool events contain bounded metadata and redacted identifiers, never prompt or tool argument bodies.

## Verification and rollback

### T20 terminal explanation repair — October 7, 2026

The live T20 case reached its two-error budget after `file.read_lines` returned
`File not found` and `Access denied: path is not allowed`. The correct terminal
stop left no final model answer. The host now adds `terminal_explanation` to
lifecycle responses: a labelled harness report with the stop reason and ordered
errors from executed tool calls. It is also used for the opt-in input-admission
refusal described above, and otherwise remains null. The ledger
shows the report before the unchanged raw transcript and renders error text as
text, not markup. Supplied history is not promoted into new execution evidence.

Acceptance: a deterministic fake model requests both original bounded reads in
disposable roots, with error budget 2. Assert both refusal results, one model
step, two tool calls, unchanged stored transcript/budget/status, no file creation,
stable repeated GET responses, and one terminal event. Renderer checks cover
literal error text, repeated refresh, stale-run isolation, and removal on a
successful run. No extra generation, retry, raised budget, permission bypass,
synthetic assistant turn, or event-schema change is permitted. The generic
DaveHarness state machine and frozen qualification contracts remain unchanged.

The original failed live attempt remains failed. Fixture success is not installed
desktop/browser acceptance or a new live-inference/H9 qualification. Source
delivery does not restart the running backend.

Tests use fake Ollama responses and disposable roots. The H7 gate covers auth, tools-off behavior, invalid inventory, unknown and conflicting runs, exact approval, BRAIN isolation, cursor replay, shell opt-in, process-group cancellation, full repository checks, and the actual desktop surface. Removing the additive lifecycle routes and ledger returns the host to the shipped legacy adapter; DaveHarness public imports remain stable through `1.x`.
