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

The lifecycle facade uses its own host registry snapshot so `shell.exec` can opt into a stoppable process runner while legacy routes keep their handler contract. Shell commands are parsed without a shell, restricted to the safe command list, resolved into a configured root, and run in a process group. Cancellation acknowledges only after the process group exits. Model and tool events contain bounded metadata and redacted identifiers, never prompt or tool argument bodies.

## Verification and rollback

Tests use fake Ollama responses and disposable roots. The H7 gate covers auth, tools-off behavior, invalid inventory, unknown and conflicting runs, exact approval, BRAIN isolation, cursor replay, shell opt-in, process-group cancellation, full repository checks, and the actual desktop surface. Removing the additive lifecycle routes and ledger returns the host to the shipped legacy adapter; DaveHarness public imports remain stable through `1.x`.
