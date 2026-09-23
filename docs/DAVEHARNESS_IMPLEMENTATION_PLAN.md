# DaveHarness implementation plan

**Status:** authoritative implementation roadmap

**Date:** 2026-09-02

**Baseline:** DaveLLM `2.1.0`, DaveHarness `0.1.0`, commit `b680e69aa27ecc3f3ed0f72eb1f5d4169911e2d2`

**Current:** DaveLLM `2.1.0`, DaveHarness `1.0.0-rc.1`

**Target:** DaveHarness `1.0.0` integrated with DaveLLM through one in-process boundary

**Release posture:** internal package only; no separate service, repository, remote protocol, CLI, published distribution, Git tag, or GitHub release is authorized by this plan

## 1. Outcome

DaveHarness `1.0.0` is achieved when DaveLLM can start, pause, resume, observe, and cancel a bounded model and tool run through a stable, host-agnostic Python API while DaveLLM retains authentication, Ollama transport, persistence, UI, configured roots, and every concrete tool implementation.

The `1.0.0` contract requires all of the following:

- One process and one repository remain the operating model.
- Existing `daveharness` exports, root `tool_executor.py` imports, endpoint response fields, status strings, transcript shapes, default budgets, and current approval scope remain backward compatible.
- New exact-call approval can resume the pending call without replaying the model request or executing any call twice.
- Cancellation and deadline behavior is explicit. Cancellable handlers stop; legacy synchronous handlers retain their documented response-deadline behavior.
- Every state transition emits an ordered, bounded, redactable event without exposing credentials, roots, node addresses, prompts, arguments, or results to logs by default.
- Run state has deterministic, versioned serialization and can be stored through a host-supplied interface. DaveHarness does not select or access DaveLLM storage.
- Tools remain disabled by default, shell keeps its second opt-in, and DaveLLM continues to enforce authentication, absolute-root containment, and public-web restrictions.
- Deterministic tests, security tests, compatibility tests, and authorized live-model evaluations satisfy the release gates in this document.

## 2. Evidence and assumptions

### Verified current facts

- `daveharness 1.0.0-rc.1` owns the generic registry, policy decisions, immutable budgets, definition fingerprints, schema validation, tool-call parsing, execution result, exact-call pending store, bounded loop, versioned leaf-contract serializer, serializable run-state engine, opt-in cancellation/deadline contracts, metadata-only lifecycle events, bounded run store, and instance-owned facade.
- `app.py` imports the package API and retains all concrete handlers, roots, authentication, Ollama calls, persistence, and routes.
- `ToolRegistry` rejects duplicate names and snapshots caller-owned definitions and nested schema data.
- Tool definitions are synchronous by default. A coroutine requires explicit opt-in and a registry-supplied name allowlist; DaveLLM permits only `web.fetch`.
- Tool definitions classify deadline handling as `bounded` or `abandon`; approval-required and write tools cannot declare `abandon`.
- Root `tool_executor.py` is a compatibility re-export.
- The current loop can pause at `approval_required` and resume the exact harness-validated call from a single-use, 300-second in-memory record without replaying the paused model step.
- Tool results preserve existing status values and add `termination`: `completed`, `deadline_abandoned`, `denied`, or `error`.
- Synchronous handlers run through `asyncio.to_thread`; `deadline_abandoned` stops waiting but cannot terminate the worker thread.
- DaveLLM already has an authenticated SSE reader for chat, but the agent-run endpoint does not stream a run ledger.
- Repository validation covers compilation, Python tests, strict DaveHarness typing, JavaScript and shell syntax, npm dependency checks, version consistency, documentation contracts, and whitespace.

### Evidence-supported inferences

- Generalizing the shipped in-process exact-call continuation into a serializable lifecycle needs a versioned run state, tool-definition fingerprint, compare-and-swap store, and executed-call ledger before UI work begins.
- Reliable cancellation needs an execution-context contract and cancellable handler or runner support; it cannot be honestly implemented by wrapping arbitrary synchronous work in another timeout.
- Streaming should reuse DaveLLM's existing authenticated `fetch` plus SSE framing rather than introduce WebSockets or a second transport.
- A host-supplied run store and event sink preserve the package boundary better than direct SQLite, FastAPI, or environment access inside DaveHarness.

### Assumptions fixed for this plan

- DaveLLM remains a single-operator local application.
- The first `1.0.0` consumer remains DaveLLM.
- In-memory run retention with a bounded time to live is sufficient for the first integration. Process-restart recovery is a later host-adapter decision, not a `1.0.0` requirement.
- Existing `/tools`, `/tools/execute`, and `/tools/agent/run` contracts remain available. Exact-call decisions use additive `POST /tools/agent/resume`; the future lifecycle API remains additive.
- DaveHarness supports the Python versions exercised by repository CI, beginning with Python 3.12.

### Unknown or separately authorized work

- Current live Ollama inventory and model-specific tool reliability must be queried when evaluation begins.
- Live tool and model runs require explicit operational authorization and safe temporary roots.
- Publishing, a separate repository, a network service, durable cross-restart run recovery, and additional consumers require new decisions.

## 3. Boundary and system overview

```text
[Electron or browser]
        |
        | authenticated HTTP plus SSE
        v
[DaveLLM app.py]
  auth, tool flags, roots, Ollama, run retention, response mapping
        |
        | Python calls and injected protocols
        v
[DaveHarness]
  contracts -> registry -> policy -> run state machine -> runner -> events
        |                         |                         |
        | ModelInvoker            | RunStore                | EventSink
        v                         v                         v
[DaveLLM Ollama adapter]   [DaveLLM-owned memory]   [DaveLLM API and metrics]
        |
        v
[Configured Ollama node]

[DaveHarness ToolRunner] -> [DaveLLM concrete handlers] -> [allowed local effect]
```

DaveHarness owns decisions about how a run advances. DaveLLM owns whether a request may enter the harness, which model and tools exist, where data lives, how events reach the operator, and which effects are allowed.

## 4. Components

| Component | Single responsibility | Inputs | Outputs | Owner |
|---|---|---|---|---|
| Public API | Stable supported import surface and version | Package imports | Exported types and functions | DaveHarness |
| Contracts | Immutable, versioned run, call, result, decision, budget, and event values | Validated Python values or serialized dictionaries | Typed values and deterministic dictionaries | DaveHarness |
| Registry | Immutable tool-definition inventory and revocation | Tool definitions | Defensive definitions, schemas, catalog, fingerprints | DaveHarness |
| Schema validator | Validate the documented JSON-schema subset | Arguments and schema | Success or bounded validation error | DaveHarness |
| Tool-call parser | Normalize native and fallback model calls | Model message | Parsed calls or parse error | DaveHarness |
| Policy engine | Decide whether a call may execute, pause, or fail closed | Run, tool, permission, approval | Structured policy decision | DaveHarness |
| Run state machine | Enforce legal, deterministic transitions | Run command and current state | New state and events | DaveHarness |
| Execution runtime | Apply deadlines, cancellation, idempotency, and result normalization | Approved call and execution context | Tool execution | DaveHarness |
| Event emitter | Produce ordered and redacted lifecycle evidence | State transitions | Versioned run events | DaveHarness |
| Run store protocol | Abstract load and compare-and-swap save | Run snapshots | Stored version or conflict | DaveHarness contract; DaveLLM implementation |
| Model adapter | Translate a harness request to the current Ollama contract | Messages and schemas | Model response | DaveLLM |
| Tool handlers | Perform concrete read, file, web, or process work | Validated arguments and context | Concrete result | DaveLLM |
| HTTP adapter | Apply auth, tool flags, node/model checks, and response compatibility | FastAPI requests | DaveHarness commands and HTTP responses | DaveLLM |
| Run ledger UI | Present status, approval, cancellation, events, and partial results | Authenticated run API | Operator controls and evidence | DaveLLM |
| Evaluation suite | Measure contract, safety, and model behavior | Deterministic cases and authorized live inventory | Release evidence | Shared, with DaveLLM owning live access |

## 5. Proposed interface contracts

These are target `1.0.0` interfaces. DaveHarness `0.2.0` provided the smaller `PendingCall`, `PendingCallStore`, and `resume_executor_loop` compatibility milestone. H1 `0.3.0` added versioned serialization only for existing leaf values; H2 `0.4.0` added policy, fingerprints, and budget values; H3 `0.5.0` adds `RunSnapshot`, exact decisions, and serializable resume operations. H4 through H9 complete cancellation, events, facade, host integration, and qualification.

| Interface | Required contract | Failure contract |
|---|---|---|
| `RunRequest` | Messages, immutable budgets, model metadata, and optional run ID; no node URL, API key, or environment lookup | Constructor rejects invalid limits and unsupported message shapes |
| `RunSnapshot` | Schema version, run ID, status, transcript, counters, deadlines, pending call, executed-call ledger, event cursor, and optimistic version | Unknown schema version fails closed without migration or execution |
| `PendingToolCall` | Call ID, tool name, canonical argument digest, tool-definition fingerprint, permission, and creation time | Any mismatch on resume invalidates approval and requires a new model decision |
| `ApprovalDecision` | Exact run ID, call ID, argument digest, decision, issue time, expiry, and one-use identity | Expired, replayed, broad, or mismatched decisions do not execute the tool |
| `ExecutionContext` | Run and call IDs, absolute deadlines, cancellation token, safe event callback, and output budget | A handler that cannot honor cancellation is explicitly classified as legacy response-deadline-only |
| `ModelInvoker` | Receives defensive message and schema copies; returns one normalized model message | Timeout or invalid response becomes `model_timeout` or `model_error` |
| `ToolRunner` | Executes one already-authorized definition with validated arguments and context | Returns a structured success, error, timeout, cancelled, or output-limit result; does not leak a traceback |
| `RunStore` | `create`, `load`, and compare-and-swap `save` of `RunSnapshot` | Storage conflicts fail closed before another side effect; unavailable runs are explicit |
| `EventSink` | Receives one immutable event at a time in sequence order | Sink failure is reported and bounded; it never authorizes or repeats an effect |
| `Harness` | `start`, `resume`, `decide`, `cancel`, and `snapshot` over injected registry, model, runner, store, clock, and sink | Illegal transitions return structured terminal or conflict outcomes |

### Stable status contract

Existing terminal statuses remain unchanged: `completed`, `approval_required`, `model_timeout`, `model_error`, `error_budget`, and `step_limit`. Existing tool statuses remain unchanged: `success`, `error`, `timeout`, `validation_error`, and `revoked`.

DaveHarness `0.2.0` adds non-executing approval outcomes `approval_not_found`, `approval_call_mismatch`, `approval_digest_mismatch`, `approval_stale`, `approval_replayed`, and `approval_expired`; operator denial adds tool status `denied`. Tool results add `termination` values `completed`, `deadline_abandoned`, `denied`, and `error`.

Future lifecycle run statuses are `running`, `cancelling`, `cancelled`, `cancellation_failed`, `approval_rejected`, `run_conflict`, and `run_expired`. Future tool statuses are `cancelled` and `output_limit`. New statuses must never change the meaning or serialized fields of an existing status.

### Run-state transitions

```text
created -> running -> completed
                   -> approval_required -> running
                                        -> approval_rejected
                                        -> run_expired
                   -> cancelling -> cancelled
                   -> model_timeout
                   -> model_error
                   -> error_budget
                   -> step_limit
                   -> run_conflict
```

Only `approval_required` may resume. Only `running` may enter `cancelling`. Every terminal state is immutable. Compare-and-swap state versions and the executed-call ledger prevent two requests from executing the same call.

## 6. Compatibility and security invariants

1. Root `tool_executor.py` remains a re-export through DaveLLM `2.x`; removal requires a DaveLLM major-version decision.
2. Current `daveharness.__all__` names remain supported through DaveHarness `1.x`.
3. `run_executor_loop` and `run_tool` retain their existing call signatures and outcome fields. New behavior is exposed through new types or optional keyword-only parameters.
4. Existing `/tools`, `/tools/execute`, and `/tools/agent/run` response bodies remain compatible and retain their authentication and default-off behavior.
5. DaveHarness never imports `app`, FastAPI, Starlette, HTTP clients, Ollama libraries, project context, SQLite, UI code, or environment readers.
6. DaveHarness never chooses configured roots, nodes, models, persistence paths, or credentials.
7. A model response is untrusted data. Tool name, argument object, schema, permission, approval, call identity, output size, and transition legality are checked before an effect.
8. Approval is exact-call, time-bounded, single-use, and bound to the argument digest and registered-definition fingerprint.
9. Revocation or definition change after a pause invalidates the pending approval.
10. Logs and default events contain IDs, names, status, timings, sizes, and reason codes, but not prompt text, argument values, tool output, secrets, roots, or node addresses.
11. File-root containment, public-web restrictions, shell opt-in, and concrete command controls remain DaveLLM responsibilities and continue to be tested at the integration boundary.
12. No timeout may be described as cancellation unless the underlying runner confirms that work stopped.

## 7. Version and delivery scheme

| Phase | DaveHarness version | DaveLLM version effect | Delivery rule |
|---|---:|---|---|
| H0 | `0.1.0` | Remains `2.1.0` | Completed extraction baseline |
| Execution semantics | `0.2.0` | Remains `2.1.0` | Completed sync-first, honest termination, and exact-call compatibility milestone |
| H1 | `0.3.0` | None | Completed leaf-contract decomposition and versioned serialization |
| H2 | `0.4.0` | None | Completed additive policy and budget contracts |
| H3 | `0.5.0` | None | Completed generalized serializable state and exact-call approval API |
| H4 | `0.6.0` | None | Completed additive cancellation and deadline API |
| H5 | `0.7.0` | None | Completed additive event and observability API |
| H6 | `0.8.0` | None | Completed instance-owned facade and host protocols |
| H7 | `0.9.0` | Prepare DaveLLM `2.2.0` capability | Additive DaveLLM lifecycle endpoints and UI integration |
| H8 | `1.0.0-rc.1` | Release candidate validation | Security, compatibility, load, and model evaluation |
| H9 | `1.0.0` | Ship DaveLLM `2.2.0` when approved | Stable in-process contract and coordinated evidence |

`daveharness/_version.py` remains the DaveHarness authority. Root `VERSION` remains the DaveLLM authority and must continue to match FastAPI, package, lockfile, and health surfaces. A commit may change one or both versions according to the table; they must never be forced to match each other.

New lifecycle APIs begin with these bounded defaults; legacy adapters retain their shipped behavior:

| Budget | New lifecycle default | Rationale |
|---|---:|---|
| Model steps | 8 | Matches the shipped loop |
| Errors | 2 | Matches the shipped loop |
| Tool calls | 32 | Allows four calls per model step while bounding one oversized model response |
| Total wall time | 300 seconds | Bounds a complete interactive run independently of individual timeouts |
| Model call | 120 seconds | Matches the shipped model timeout |
| Tool call | Definition value, default 10 seconds | Matches the shipped tool timeout |
| One normalized tool result | 64 KiB | Provides a generic ceiling above DaveLLM's current 5,000-character concrete-tool limit |
| Serialized transcript | 2 MiB | Allows two-times headroom over DaveLLM's current one-million-character request ceiling |
| Retained events | 1,000 events and 1 MiB | Preserves an interactive ledger without unbounded memory |
| Nested input depth | 32 levels | Prevents pathological recursion while allowing ordinary JSON schemas and arguments |

H8 must measure these defaults against the current workload and two-times headroom. A changed new-lifecycle default requires evidence and documentation; no new default may silently tighten the legacy route.

Every phase is one coherent, reviewable change that keeps `main` green. Each phase is validated, committed, pushed, checked at the exact remote commit, and read back from Pages when public documentation changes. Tags, releases, publishing, and a separate repository remain out of scope until explicitly approved.

## 8. Sixty-action implementation ledger

### H0: establish the boundary and baseline, `0.1.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 1 | Record the DaveLLM and DaveHarness ownership boundary and independent SemVer rules in the ADR and project specification. | Reviewed boundary table and version contract. | Low | Documentation revert | Completed |
| 2 | Extract registry, validation, parsing, execution result, and bounded-loop code into `daveharness/`. | Package imports without DaveLLM runtime dependencies. | Medium | Compatibility shim permits revert | Completed |
| 3 | Make `app.py` and first-party executor tests consume the package API while preserving root imports. | Direct-package and shim identity tests pass. | Low | Import-only revert | Completed |
| 4 | Reject duplicate registry names and snapshot security-relevant definitions and nested schemas. | Duplicate and mutation regression tests pass. | Medium | Local registry revert | Completed |
| 5 | Prove the dependency boundary and public export list. | Static import/source boundary and `__all__` tests pass. | Low | Test and export revert | Completed |
| 6 | Update CI and public documentation, then verify the exact commit and Pages artifact. | Commit `b680e69`, CI, Pages, and byte-for-byte readback passed. | Low | Documentation revert | Completed |

### Delivered execution-semantics milestone, `0.2.0`

This compatibility milestone was intentionally narrower than the future lifecycle architecture and does not change the sixty-action ledger. It enforces sync-first first-party handlers and a registry-level async-name allowlist; adds `bounded` versus `abandon` declarations and honest `termination` metadata; and implements a 300-second, single-use, in-memory exact-call continuation through `POST /tools/agent/resume`. It does not add durable run serialization, process-restart recovery, hard cancellation, a full run facade, events, or UI controls.

### H1: make contracts explicit and serializable, `0.3.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 7 | Split `executor.py` internally into `contracts.py`, `registry.py`, `schema.py`, `parser.py`, and `engine.py` while retaining `executor.py` and root `tool_executor.py` compatibility exports. | Existing executor and boundary tests pass without caller changes. | Medium | Revert module split | Completed |
| 8 | Keep frozen typed values for parsed calls, pending calls, tool executions, and outcomes; validate construction and own nested JSON copies. Generalized budgets belong to H2; `RunSnapshot` belongs to H3. | Constructor, equality, and defensive-copy tests cover existing leaf values. | Medium | New validation can be reverted | Completed |
| 9 | Add separate deterministic `encode_contract` and `decode_contract` round trips; preserve legacy `ExecutorOutcome.to_dict()` and HTTP response dictionaries. | Version-one golden fixtures round-trip byte-for-byte after canonical JSON encoding. | Medium | Keep legacy serializers | Completed |
| 10 | Add `CONTRACT_VERSION = 1` and reject unknown versions, types, and fields before constructing a leaf value. | Forward-version fixtures fail closed before value construction. | Low | Version check can be reverted | Completed |
| 11 | Pin `mypy==2.3.1` as a development-only dependency, enable Python 3.12 strict checking for `daveharness/`, and add the CI gate. | Python 3.12 type check reports no package errors; runtime dependencies remain unchanged. | Low | Remove development gate | Completed |
| 12 | Expand public-API, import-boundary, constructor-validation, round-trip, and golden-shape tests. | Targeted contract suite and full repository suite pass. | Low | Test-only revert | Completed |

### H2: centralize policy and budgets, `0.4.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 13 | Introduce an injected policy object that evaluates registered name, permission string, approval requirement, and current run context. | Table-driven tests cover allow, deny, pause, revoke, and unknown permission behavior. | Medium | Legacy policy adapter remains | Completed |
| 14 | Return a structured policy decision with stable reason codes instead of scattered booleans. | Outcomes and events expose reason codes without changing existing status text. | Medium | Additive decision type | Completed |
| 15 | Canonically fingerprint each immutable tool definition and its schema. | Equivalent definitions produce one digest; any security-relevant change produces a different digest. | Medium | Fingerprint is additive | Completed |
| 16 | Replace loose step and error integers internally with an immutable budget contract covering model steps, tool calls, errors, total wall time, tool output bytes, transcript bytes, and event count. | Defaults remain eight steps and two errors; each new ceiling has a boundary test. | Medium | Legacy arguments adapt to budgets | Completed |
| 17 | Recheck registry presence, fingerprint, permission, and approval immediately before every effect. | Revocation and mutation race tests prove fail-closed behavior. | High | Policy path is feature-contained | Completed |
| 18 | Add the complete policy and budget matrix to unit tests and documentation. | Every permission and terminal reason has at least one positive and negative test. | Low | Test and docs revert | Completed |

### H3: generalize resumable run state and exact-call approval, `0.5.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 19 | Implement `RunSnapshot` with run ID, contract version, optimistic version, status, transcript, counters, deadlines, pending call, executed-call ledger, and event cursor. | Snapshot fixtures cover every legal status and reject incomplete state. | High | New API is additive | Completed |
| 20 | Implement one transition function for all legal run-state changes. | Exhaustive transition tests reject illegal resume, double terminal, and backwards transitions. | High | Existing loop remains adapter | Completed |
| 21 | Extend the current pending-call record with contract version, definition fingerprint, permission, and serializable state. | A changed argument, tool, permission, or schema invalidates the pending call across a store round trip. | High | Current in-memory continuation remains | Completed |
| 22 | Add an exact `ApprovalDecision` contract with approve, reject, expiry, and single-use identity. | Broad, expired, mismatched, and replayed decisions never execute. | High | Existing per-run approvals remain on legacy API | Completed |
| 23 | Generalize the current `resume_executor_loop` path into serializable `resume` and `decide` engine operations. | Model invocation count remains unchanged during approval; the exact tool executes once after a store round trip. | High | Current in-memory continuation remains | Completed |
| 24 | Add serialization, process-boundary simulation, optimistic-conflict, approval, rejection, expiry, replay, revocation, and definition-change tests. | Targeted state suite passes under repeated and concurrent decision attempts. | High | Test-only revert | Completed |

### H4: add cooperative cancellation and absolute deadlines, `0.6.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 25 | Add `CancellationToken` and `ExecutionContext` with run, call, deadline, output-budget, and event access. | Cooperative fake handlers observe cancellation and stop before returning. | High | Optional context path | Completed |
| 26 | Compute absolute run, model, and tool deadlines from one injected monotonic clock. | Fake-clock tests prove deterministic boundary behavior without sleeps. | Medium | Clock defaults to current behavior | Completed |
| 27 | Carry the current `bounded` or `abandon` declaration and `deadline_abandoned` termination through the new runner without changing existing status strings. | Compatibility tests show unchanged timing and result shapes; documentation does not claim hard cancellation. | Medium | Existing path retained | Completed |
| 28 | Add an injected cancellable runner path for async, cooperative, and process-backed handlers. | Runner contract proves stop acknowledgement before emitting `cancelled`. | High | Per-tool opt-in | Completed |
| 29 | Enforce one total run deadline in addition to existing per-model and per-tool timeouts. | A run cannot extend indefinitely through individually successful calls. | High | Budget can default to compatibility mode | Completed |
| 30 | Test timeout/cancel races, cancel-before-start, cancel-during-model, cancel-during-tool, late result, and exactly-one-terminal-event behavior. | Repeated seeded concurrency tests pass without duplicate effects. | High | Test and new runner revert | Completed |

### H5: add ordered events and observability, `0.7.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 31 | Define versioned `RunEvent` values with run ID, sequence, kind, step, call ID, tool name, status, reason code, timestamp, duration, and safe size metadata. | Golden fixtures cover every event kind and omit sensitive bodies. | Medium | Additive event type | Completed |
| 32 | Add an injected async `EventSink` plus a no-op default. | Existing callers behave identically without a sink. | Low | Remove optional injection | Completed |
| 33 | Emit events for creation, model start/result, parse repair, policy decision, approval, tool start/result, cancellation, budget stop, and terminal outcome. | Event-order tests match state transitions exactly. | Medium | Sink can be disabled | Completed |
| 34 | Centralize redaction and safe metadata extraction for logs and events. | Secret, path, URL, prompt, argument, and result canaries never appear in captured logs or default events. | High | Keep metadata-only fallback | Completed |
| 35 | Bound retained events by count and byte budget while preserving the first event, latest events, and terminal event. | Overflow tests produce one explicit truncation event and bounded memory use. | Medium | Limits can be raised | Completed |
| 36 | Make sink failure observable but non-authorizing and non-repeating. | A failing sink cannot execute a call twice or conceal the final outcome. | Medium | No-op sink fallback | Completed |

### H6: provide an instance-owned host facade, `0.8.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 37 | Define `RunStore` and add a bounded in-memory implementation with compare-and-swap versions, maximum run count, and time-to-live expiry. | Conflict, expiry, eviction, and memory-bound tests pass. | High | Host may keep legacy direct loop | Completed |
| 38 | Add an instance-owned `Harness` facade over registry, policy, model, runner, store, clock, and event sink. | Two harness instances run concurrently without shared tools, approvals, counters, or events. | High | Existing functions remain adapters | Completed |
| 39 | Implement `start`, `snapshot`, `decide`, `resume`, and `cancel` through the facade. | Public lifecycle tests cover every legal operation and terminal state. | High | Additive API | Completed |
| 40 | Retain `run_executor_loop`, `run_tool`, and `DEFAULT_TOOL_REGISTRY` as compatibility adapters with documented limitations. | Existing package and shim tests remain unchanged. | Medium | Compatibility layer isolates change | Completed |
| 41 | Remove engine reliance on mutable module globals while keeping compatibility globals at the outermost adapter only. | Parallel tests prove no cross-run registry or approval leakage. | High | Adapter can restore legacy internals | Completed |
| 42 | Publish an API support table and deprecation rule: no removal from DaveHarness `1.x`, and no root-shim removal before a separately approved DaveLLM major version. | Documentation contract test locks the table and policy. | Low | Documentation revert | Completed |

### H7: integrate the full run lifecycle into DaveLLM, `0.9.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 43 | Instantiate one DaveHarness facade in `app.py` with DaveLLM's registry, Ollama model adapter, bounded memory store, runner, and safe event sink. | App import and dependency-boundary tests pass; no runtime state moves into the package. | High | Legacy route remains available | Completed |
| 44 | Preserve `/tools`, `/tools/execute`, `/tools/agent/run`, and `/tools/agent/resume` and route their implementation through compatibility adapters. | Existing endpoint snapshots and security tests pass byte-for-byte for stable fields. | High | Direct legacy implementation can be restored | Completed |
| 45 | Add authenticated, tools-default-off lifecycle endpoints: `POST /tools/agent/runs`, `GET /tools/agent/runs/{run_id}`, `GET /tools/agent/runs/{run_id}/events`, `POST /tools/agent/runs/{run_id}/decisions`, and `POST /tools/agent/runs/{run_id}/cancel`. | Auth, disabled-tools, invalid-node/model, unknown-run, conflict, expiry, and happy-path tests pass. | High | Entire additive route family can be removed | Completed |
| 46 | Stream ledger events with the existing authenticated `fetch` and SSE framing; support cursor-based replay within the retained event window. | Reconnect resumes after the last sequence without duplicates and ends with one terminal event. | High | Complete-result polling remains fallback | Completed |
| 47 | Add an accessible run ledger with ordered states, exact call and permission summary, Approve once, Reject, Stop, partial transcript, and terminal reason. | Keyboard, focus, screen-reader labels, responsive widths, and reduced-motion behavior are verified on the actual surface. | High | UI entry point can remain hidden while API stays | Completed |
| 48 | Refactor DaveLLM's process-backed shell handler to use the cancellable runner contract while preserving shell opt-in, root containment, command controls, timeout defaults, and output shape. | Controlled temporary-root tests prove process-group termination and no post-cancel file effect. | High | Tool remains disabled unless both flags are set | Completed |

### H8: complete security, compatibility, and deterministic evaluation, `1.0.0-rc.1`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 49 | Add and pin the currently supported Hypothesis release as a development-only dependency, then add property-based tests for contract serialization, schema validation, parser normalization, and transition legality. | Seeded and generated cases reproduce locally and in CI; no runtime dependency is added. | Low | Remove dev dependency and generated gate | Completed |
| 50 | Add adversarial cases for deep nesting, oversized values, duplicate IDs, Unicode, non-finite numbers, malformed native calls, fenced fallback calls, and unsupported schema keywords. | Each case fails predictably within byte, depth, and time bounds. | Medium | Test and input limits are local | Completed |
| 51 | Add concurrency tests for duplicate decisions, revocation during approval, cancellation during completion, store conflicts, late model/tool results, and sink failure. | Exactly one side effect and one terminal state occur in every seeded race. | High | Test-only revert | Completed |
| 52 | Add output, transcript, event, nesting, and payload limits before data is copied or serialized. | Memory and latency stay bounded at current expected load plus two-times headroom. | High | Limits are configurable with safe defaults | Completed |
| 53 | Add the currently supported `coverage.py` release as a development-only dependency and create a CI matrix for supported Python versions, strict typing, package-only coverage, public-API snapshots, dependency-boundary scans, and log-redaction canaries. | Every required lane passes at the exact commit. | Medium | CI jobs can be reverted individually | Completed |
| 54 | Build a deterministic model-adapter evaluation corpus covering final answers, native calls, fallback calls, repairs, approvals, repeated calls, ceilings, timeouts, and cancellation. | Fake and recorded-response runs produce a machine-readable report with zero unauthorized effects. | Medium | Evaluation suite is isolated | Completed |

### H9: qualify and converge on `1.0.0`

| ID | Action and target | Acceptance evidence | Risk | Reversibility | Readiness |
|---:|---|---|---|---|---|
| 55 | With explicit authorization, query current live inventory and run at least 500 representative evaluations per target model, using read-only tools first and approved mutations only inside disposable roots. | Inventory, configuration, counts, results, and exclusions are recorded without node addresses or credentials. | High | Runs use disposable state; no production mutation | Authorized; blocked by model transport |
| 56 | Enforce release thresholds: 100% unauthorized-effect prevention, at least 99.5% schema-valid calls after one repair, less than 0.5% step-limit termination, and less than 5% malformed or repeated calls after repair. | Signed evaluation report meets every threshold per model; failures create model-specific restrictions rather than relaxed safety. | High | Restrictions are configuration-level | Blocked by action 55 |
| 57 | Update API reference, migration guide, compatibility table, threat model, operations guide, changelog, project specification, ADR, and public documentation; set DaveHarness to `1.0.0` and prepare the coordinated DaveLLM `2.2.0` version change. | Documentation contracts and version checks pass; no tag or release is created. | Medium | Revert before release authorization | Blocked by H1 through H8 |
| 58 | Run the complete repository suite, supported-Python matrix, security/evaluation gates, tools-off smoke test, controlled read-only run, exact approval/resume run, cancellation run, UI accessibility pass, and process-restart failure check. | Each proof is tied to the exact candidate commit and classified separately. | High | Candidate can be rejected without release | Blocked by actions 55 through 57 |
| 59 | Commit and push the candidate, verify local and remote refs, wait for exact-commit CI and Pages deployment, and read back every public contract changed by the release. | Git, CI, deployment, and published artifact evidence all agree on the candidate commit. | Medium | A corrective commit remains possible | Blocked by action 58 |
| 60 | Record `1.0.0` acceptance, supported DaveLLM range, known legacy timeout limitation, model qualification table, rollback point, and post-1.0 backlog. Create a tag, GitHub release, published package, service, or repository only under a separate explicit approval. | Human approval closes the implementation ledger with no unresolved release blocker. | Medium | Acceptance can be withheld; publishing is separate | Blocked by action 59 and approval |

## 9. Failure modes and fallback behavior

| Failure mode | Required behavior | Fallback or recovery |
|---|---|---|
| Malformed model tool call | Append one bounded repair instruction; consume error budget; execute nothing | Return `error_budget` with the complete partial transcript |
| Unknown or revoked tool | Fail closed immediately before execution | Record `revoked`; model may recover within budget |
| Definition changes while approval is pending | Fingerprint mismatch invalidates the pending approval | Return to a non-executing conflict state and require a new model decision |
| Approval is replayed or arrives twice | The current atomic nonce claim rejects the duplicate; the future serializable store adds compare-and-swap | Return a non-executing replay/conflict outcome without another effect |
| Model is slow or unavailable | Apply model deadline and emit terminal evidence | Return `model_timeout` or `model_error` |
| Legacy synchronous handler exceeds timeout | Stop waiting and report `timeout`; never claim the thread stopped | Surface response-deadline-only limitation and suppress late result |
| Cancellable handler exceeds deadline | Request cancellation and wait for runner acknowledgement within its bounded shutdown allowance | Return `cancelled` only after acknowledgement; otherwise report explicit cancellation failure |
| Tool output is too large | Stop accepting bytes at the configured ceiling | Return bounded output metadata and an output-limit error |
| Event sink fails | Preserve run state and effect idempotency | Record one bounded sink-failure marker where possible; polling remains available |
| Run-store conflict | Execute no new side effect | Reload the latest snapshot and return `run_conflict` |
| In-memory run expires or process restarts | Return explicit unavailable or expired state | Preserve already-committed DaveLLM conversation data; start a new run |
| UI disconnects | Run state remains in bounded host memory | Reconnect with the last event sequence or poll snapshot |
| Cancellation races with completion | One atomic transition wins | Return the single winning terminal state and never rewrite it |
| Log or event payload contains a canary secret | CI and release gate fail | Fix redaction before merge; do not weaken the canary |

## 10. Validation and release gates

### Per-phase local gate

```bash
python -m py_compile app.py project_context.py tool_executor.py scripts/project_context_cli.py
python -m compileall -q daveharness
python -m pytest -q
node --check static/app.js static/anticipation.js static/prompt-contract.js static/vendor/gsap/gsap.min.js desktop/main.js desktop/preload.js
bash -n deploy/check-cluster.sh scripts/verify-cluster.sh scripts/macos/install-launcher.sh scripts/macos/install-whisper-runtime.sh scripts/macos/launch-davellm.sh
npm ci
npm ls --depth=0
npm audit
git diff --check
```

When H1 introduces the approved development checks, strict typing, supported-Python testing, package coverage, and property-based tests become mandatory CI gates rather than optional local evidence.

### Gate A: contract

- All current imports and serialized stable fields remain compatible.
- Every new contract round-trips deterministically.
- Unknown contract versions fail without invoking a model or tool.

### Gate B: state and safety

- Every transition is legal, versioned, and idempotent.
- Exact-call approval executes once and cannot be broadened or replayed.
- Revocation, mutation, conflict, timeout, and cancellation races fail closed.

### Gate C: boundary

- Importing DaveHarness loads no DaveLLM application, FastAPI, Ollama, project context, persistence, UI, or environment state.
- DaveLLM still owns every concrete effect and external connection.
- Tools and shell remain default-off behind their current independent gates.

### Gate D: operator surface

- The run ledger truthfully shows pending, approval required, running, success, error, cancelled, and bounded-stop states.
- Approve once, Reject, and Stop work with keyboard and assistive technology.
- Disconnect and reconnect do not duplicate or conceal events.

### Gate E: qualification

- Deterministic, adversarial, concurrency, compatibility, and load suites pass.
- Authorized per-model evaluation meets every threshold without weakening policy.
- Unsupported models are restricted or excluded explicitly.

### Gate F: delivery

- Working tree contains only the intended phase.
- Local commit, `origin/main`, and remote `main` resolve to the exact delivered commit.
- Required GitHub Actions pass for that commit.
- Pages deployment succeeds and changed public documents are read back.
- Runtime and UI claims are made only from authorized, visible runtime evidence.

## 11. Operations

- **Deploy:** DaveHarness ships only as source inside the DaveLLM repository and starts inside the existing DaveLLM process.
- **Configure:** DaveLLM supplies tool flags, roots, model/node selection, authentication, store limits, and concrete adapters. DaveHarness reads no environment variables.
- **Monitor:** DaveLLM aggregates redacted harness events into run counts, terminal-status counts, approval latency, model/tool duration, malformed-call rate, cancellation latency, and ceiling rate.
- **Health:** Existing `/health` remains stable. An additive `harness_version` field may be exposed only with a contract test; detailed tool state remains behind authentication.
- **Update:** Advance one phase at a time, preserve a green main branch, and keep the legacy route usable until the new integration passes Gate E.
- **Rollback:** Before any release tag, revert the latest phase commit. After an approved release, issue a corrective patch rather than moving or rewriting a tag.
- **Retention:** The first host store is bounded memory with explicit time-to-live and count limits. No prompt, result, or credential is added to persistent storage by this plan.

## 12. Critical path and execution rule

```text
H0 complete
   -> 0.2.0 execution semantics complete
   -> H1 0.3.0 leaf contracts complete
   -> H2 0.4.0 policy and budgets complete
   -> H3 0.5.0 state and exact approval complete
   -> H4 0.6.0 deadlines and cancellation complete
   -> H5 events
   -> H6 facade and host protocols
   -> H7 DaveLLM API and UI
   -> H8 security and deterministic evaluation
   -> H9 authorized live qualification and 1.0 convergence
```

Do not implement H7 before H3 through H6 are stable: an HTTP or UI layer built on an unsettled state machine would create duplicate lifecycle logic. Do not claim `1.0.0` before H9 live qualification; deterministic tests prove engineering behavior, not model-specific reliability. Do not combine all remaining phases into one change. Each phase must finish its own test, commit, push, CI, and documentation gates before the next phase begins.

The next package is **H5: ordered events and observability at `0.7.0`**. H4 introduces opt-in cooperative cancellation and absolute deadlines while retaining legacy response-deadline behavior for synchronous handlers. H5 adds actual event emission and retention to the single-terminal transition tested in H4. Legacy routes retain their shipped limits and fields.

H9 runtime scope was explicitly authorized on 2026-09-23; see [fixed qualification protocol](DAVEHARNESS_H9_PROTOCOL.md). Action 55 remains uncompleted until the authorized results are recorded. The original runtime-authorization readiness label above describes the pre-authorization baseline.

H9 runtime readback: the authorized model produced no usable completion; qualification is blocked by model transport, not awaiting initial scope approval. See [H9 blocked report](DAVEHARNESS_H9_RESULTS.md). Actions 55–60 are not complete.
