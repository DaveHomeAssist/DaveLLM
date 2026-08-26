# Execute the Dave LLM prompt series

## Completion ledger

| Prompt | Required contract | Completion evidence |
|---|---|---|
| P0 | Shared context | Verified repo, runtime, model, and deployment boundaries below |
| P1 | Harness decision | Decision, tradeoffs, flip conditions, and confidence recorded below |
| P2 | Executor design and code | Bounded registry-driven loop, approvals, error budget, and tests implemented |
| P3 | BRAIN plan | Plan delivered, then fully implemented with revisions, compaction, recovery, API, UI, and CLI |
| P4 | Project Homepage plan | Plan delivered, then fully implemented with four components, allocator, preview, and explicit attachment |
| P5 | Instruction UI code | Implemented and covered by exact-payload tests |
| P6 | Copy actions code | Implemented for both message roles with raw-source preservation |
| P7 | Lightweight notepad code | Implemented with scoped persistence and autosave |
| P8 | Icon review | Review delivered, then the recommended licensed Lucide replacement implemented |

## Establish shared context

| Slot | Verified value |
|---|---|
| Front end | Vanilla HTML, CSS, and JavaScript inside Electron |
| Code location | `/Users/daverobertson/Code/dave-llm`, remote `DaveHomeAssist/DaveLLM` |
| Relationship to Open WebUI | Standalone application, not a fork or wrapper |
| Target models | Runtime-discovered Ollama models. Live 2026-08-26 inventory: Dominic `llama3:latest`; Walter `qwen3-coder:30b`, `gpt-oss:20b`, `gpt-oss:120b`; Duncan `qwen3-coder:30b`, `gemma3:27b`, `gpt-oss:20b`, `gpt-oss:120b`. Source does not pin model IDs. |

Assumptions:

- DaveLLM remains a single-user, local-only application.
- FastAPI remains the trusted middleware boundary between the renderer and Ollama.
- Tool access remains disabled unless explicitly enabled in the process environment.
- The brief names Dominic, Ubuntu Server 24.04, and its Docker Compose stack as the deployment target. The verified current launcher runs FastAPI and Electron on macOS and reaches the Ollama nodes over Tailscale, so deployment completion is outside these prompt contracts.

## Decide harness architecture

### DECISION

1. Recommendation: Choose C, a hybrid agentic harness with explicit per-tool permission policy and approval before mutating or execution tools run.
2. Why:
   1. Contain unreliable local tool calls with schema validation, revocation, timeouts, an error budget, and an eight-step ceiling.
   2. Keep file writes and command execution under human control while allowing explicitly scoped read-only tools to complete without repeated babysitting.
   3. Put enforcement in FastAPI so a renderer bug or model prompt cannot bypass the sandbox.
3. Trade offs: Approval pauses add latency, and smaller local models may still waste one or two steps repairing malformed calls.
4. What would change my mind: Move toward B for a specific read-only tool only after at least 500 representative runs on each target model show at least 99.5 percent schema-valid calls, no sandbox escapes, and fewer than 0.5 percent ceiling terminations. Move back to A if malformed or repeated calls exceed 5 percent after one repair attempt.
5. Confidence: Medium. A model-specific evaluation across every model in the live runtime inventory would raise it.

Checkpoint: The recommendation is one option. The flip condition is measurable.

## Design executor loop

### PLAN

1. Goal: Run bounded, schema-validated Ollama tool turns inside FastAPI while preserving a complete transcript and requiring approval at the configured trust boundary.
2. Steps:
   1. Register each tool in a runtime registry with its JSON schema, handler, permission class, approval policy, and timeout. This unblocks safe schema publication and individual revocation.
   2. Build the Ollama request in FastAPI and include the current registry schema list on every model invocation. This unblocks model-selected tool calls without trusting renderer state.
   3. Normalize native `tool_calls` first, then accept one exact JSON or fenced-JSON fallback. Reject malformed arguments before execution and add one corrective system message. This unblocks recovery for weaker local models.
   4. Validate the tool name and arguments, stop at the approval gate when required, execute inside its configured scope and timeout, and return a structured error instead of raising. This unblocks contained execution.
   5. Append the assistant tool call and a standard tool result to the transcript, then invoke Ollama again with all schemas. This unblocks reasoning over results and follow-up calls.
   6. Stop on a final assistant answer, the configurable step ceiling, model timeout, approval gate, or error budget. Return the entire partial transcript in every terminal state. This prevents silent loops.
   7. Render an ordered run ledger with pending, approval required, running, success, and error states. Keep a visible Stop control and preserve partial progress when a ceiling is hit. This gives the operator truthful mid-flight state.
3. Dependencies:
   - Step 2 depends on the registry in step 1.
   - Steps 4 and 5 depend on normalization in step 3.
   - Step 6 depends on every prior step returning structured state.
   - Step 7 depends on a streaming or resumable transport for loop events.
4. Message array shape:

| Order | Role | Required fields | Purpose |
|---|---|---|---|
| 1 | `system` | `content` | Effective global, project, and session instructions |
| 2..n | `user` or `assistant` | `content` | Existing conversation history |
| next | `assistant` | `content`, `tool_calls[]` | Model-selected call with ID, name, and JSON arguments |
| next | `tool` | `tool_call_id`, `name`, `content` | JSON result carrying status, output or error, timestamps, and duration |
| repeat | `assistant` | `content` or `tool_calls[]` | Final answer or another bounded step |

5. Risks:
   - A model can emit malformed JSON repeatedly. One corrective turn is allowed, every failure consumes the error budget, and no malformed call executes.
   - A tool can hang. Each handler has a timeout and returns `timeout` in the transcript.
   - A broad file root can make a correct containment check unsafe. Tool roots must remain absolute and narrowly configured.
   - Preapproving mutating tools weakens the hybrid gate. The UI must distinguish approval for one run from a durable policy change.
   - A model may reach the ceiling without prose. The terminal result must still show the last content, all calls, all results, and the ceiling reason.
6. Definition of done: Done when a registered read-only tool completes a two-turn model exchange, an out-of-root file write is refused, malformed arguments are repaired or stop at the error budget, a mutating tool pauses for approval, every call and result carries timing data, and eight repeated tool turns return a partial transcript without hanging.

Checkpoint: The message array is a primary system message, prior turns, an assistant `tool_calls` message, a matching `tool` result, and the next assistant turn.

## Implement executor loop

### CODE

1. What changed:

| File | Diff summary |
|---|---|
| `tool_executor.py` | Add a runtime registry, JSON schema validation, structured dispatcher results, timeouts, timestamps, duration logs, native and fallback tool-call parsing, approval gates, error budget, and configurable loop ceiling. |
| `app.py` | Register the existing tools with schemas and permissions, route manual execution through the shared dispatcher, expose the schema catalog, and add authenticated `POST /tools/agent/run` for bounded Ollama runs. |
| `tests/test_tool_executor.py` | Cover registration, revocation, validation, timeout, malformed-call repair, approval pause, full transcript shape, repeated schema publication, final answers, and ceiling termination. |

Plain English summary: FastAPI now owns one validated execution path for both manual and model-selected tool calls. The model receives only the currently registered schemas. A disabled, revoked, malformed, out-of-scope, timed-out, or failed call becomes transcript data instead of an unhandled exception.

2. Why:
   - Keep the model outside the trust boundary.
   - Make individual tools loadable and revocable without changing the loop.
   - Preserve enough timing and transcript evidence to diagnose a local model run.
   - Guarantee termination under repeated calls or repeated errors.
3. Risk and blast radius:
   - Ollama tool-call behavior remains model-dependent. Native OpenAI-compatible `tool_calls` and one exact JSON fallback are supported.
   - The endpoint returns a complete result rather than streaming ledger events. A renderer event transport remains separate UI work.
   - Approval is scoped to the submitted run. Durable approval policy is deliberately not persisted.
   - Existing manual `/tools/execute` callers keep their prior success and error shape, with timing fields added.
4. How to verify:

```bash
source venv/bin/activate
python -m pytest -q tests/test_tool_executor.py tests/test_security_and_frontend.py
```

5. Not done:
   - Do not auto-enable tools. `DAVE_ENABLE_TOOLS=true` remains required.
   - Do not auto-enable shell execution. `DAVE_ENABLE_SHELL_TOOL=true` remains a second gate.
   - Do not persist broad approvals or expose a renderer-side bypass.
   - Do not claim a particular live model is reliable until the model evaluation named in P0 runs.

Checkpoint: The allowlist test refuses a sibling path, and the ceiling test returns the full partial transcript after three forced steps.

## Design BRAIN context keeper

### PLAN

1. Goal: Preserve project facts, decisions, constraints, and open work across sessions while compacting replaceable working context before it crowds out the current task.
2. Ordered steps:
   1. Store BRAIN in SQLite. Flat JSON is simple but weak under concurrent compaction and recovery; a vector store helps retrieval but is not the durable authority. Pick SQLite because DaveLLM already operates SQLite stores, transactions protect updates, and revision history stays inspectable in the router data directory. On Dominic, persist that directory in a named Docker volume beside the existing stack services rather than inside an Ollama or Open WebUI volume.
   2. Define four retention tiers. Pinned facts and decisions survive verbatim. Active goals, contracts, and open risks survive in structured form. Recent working context is summarized. Duplicate chatter, superseded drafts, resolved transient troubleshooting, and raw tool logs are dropped after a recoverable snapshot is written.
   3. Trigger compaction primarily at a configurable token threshold, with explicit Compact now as a secondary trigger. Message count and wall clock are useful checks but poor proxies for context pressure. Expose `DAVE_BRAIN_COMPACT_TOKENS`, a per-project override, and an explicit API action.
   4. Run compaction outside the P2 executor loop. An in-loop tool call gives the model control but spends agent steps and can be skipped; a scheduled middleware job is predictable and independent. Pick an asynchronous FastAPI compaction queue plus a daily reconciliation job, with one compaction lock per project.
   5. Write an immutable pre-compaction revision, generate the candidate compacted state locally, validate required pinned IDs and open decisions, then atomically promote it. This unblocks rollback and prevents partial replacement.
   6. Inject BRAIN as a dedicated system-context message after the global, project, and session instruction layers and before file context, artifacts, conversation summary, and recent turns. This keeps durable context below instructions but above evidence selected for the current request.
   7. Expose Read, Edit, Pin, Delete, Compact now, and Restore revision controls in the Project Homepage. Provide matching authenticated API and local CLI operations. Delete is a soft delete until the configured recovery window expires.
3. Dependencies:
   - The project identity and authenticated API boundary must exist before steps 1 and 7.
   - Step 3 needs the existing token estimator and a configured absolute threshold. The P4 allocator can later derive a project-aware default, but it does not block BRAIN storage or compaction.
   - The local summarizer must pass structured-output validation before step 5 can promote a compaction.
4. Risks:
   - A summary can omit a critical detail. Pinned content is copied verbatim, every promotion keeps a prior revision, and Restore revision reverses the mistake.
   - A small model can produce invalid structured output. Validation rejects the candidate and leaves the active BRAIN unchanged.
   - Concurrent chat and compaction can race. Use project-scoped locking and a compare-and-swap source revision.
   - BRAIN can become a second chat transcript. Enforce tiers, size budgets, deduplication, and manual pinning instead of retaining everything.
   - Sensitive project data can outlive its purpose. Manual deletion, soft-delete visibility, and expiry are required.
5. Definition of done: Done when a project crosses its configured threshold, a background compaction creates a validated new revision, pinned facts remain byte-for-byte, transient material is absent, a new session receives the active BRAIN in the documented position, and the operator can inspect, edit, delete, and restore it without database tooling.

Checkpoint: BRAIN drops duplicate chatter, superseded drafts, resolved transient troubleshooting, and raw tool logs after a recoverable snapshot. It never drops pinned facts or open decisions automatically.

Implementation closeout (2026-08-26): Completed in `project_context.py`, authenticated FastAPI routes, the Project Homepage BRAIN panel, the daily/queued compaction worker, and `scripts/project_context_cli.py`. Automated coverage proves protected-tier preservation, deterministic transient-line removal, optimistic revisions, explicit compaction, soft delete, restore, and recovery-window expiry.

## Specify Project Homepage container

### PLAN

1. Goal: Give each project one landing surface that owns instructions, reference files, prior artifacts, and BRAIN with a deterministic context budget.
2. Component data model:

| Component | Required data | Lifecycle | Request-time representation |
|---|---|---|---|
| Project Instructions | `project_id`, Markdown text, revision, updated timestamp | Editable, revisioned, never silently truncated | System message immediately after the global default |
| File Context Uploads | File ID, project ID, display name, media type, size, hash, extracted text, chunk index, status, created timestamp | Upload, reindex, detach, delete | Ranked bounded context message with file and chunk citations |
| Artifact History | Artifact ID, project ID, source conversation ID, title, kind, body or storage pointer, token count, created timestamp, pinned state | Retain, retrieve, pin, archive, delete | Selected or ranked bounded context message after files |
| BRAIN | Project ID, active revision, structured tiers, token count, compaction config, last compacted timestamp | Edit, pin, compact, restore, soft delete | Dedicated system-context message after project instructions |

Project record: `project_id`, owner ID, name, description, preferred node and model policy, context budget policy, created timestamp, updated timestamp, and archive state. Component bodies remain in their owned tables rather than one expanding project JSON blob.

3. Ordered steps:
   1. Add the normalized project and four component stores with stable IDs, revision fields, and ownership checks. This unblocks safe independent lifecycle operations.
   2. Add upload extraction and chunk-status boundaries without making a vector store the authority. This unblocks visible file readiness and recoverable reindexing.
   3. Add artifact capture and explicit pin or archive actions. This unblocks retrieval without replaying whole conversations.
   4. Apply the context allocator before each request. For model window `C`, first reserve output `R`, safety margin `S`, current user input `U`, and recent conversation `H`; the four components share `P = C - R - S - U - H`.
   5. Assemble messages in this order: global default, Project Instructions, session override, BRAIN, File Context Uploads, Artifact History, conversation summary, recent turns, current user message. Later instruction layers win conflicts, while evidence layers never override instructions.
   6. Render the empty homepage with the project name and four visible empty states. Make Add instructions the primary action, Upload files secondary, and show Artifact History and BRAIN as empty but explained.
   7. Attach a chat through an explicit `project_id` at creation. Allow General sessions with no project. Reattachment requires an explicit action, recomputes future context, and never rewrites past messages.
4. Token budget:

When all four components are full, divide `P` exactly as follows:

| Component | Share of `P` | 16,384-token example |
|---|---:|---:|
| Project Instructions | 25 percent | 4,096 |
| BRAIN | 25 percent | 4,096 |
| File Context Uploads | 30 percent | 4,915 |
| Artifact History | 20 percent | 3,277 |
| Total | 100 percent | 16,384 |

Project Instructions cannot silently overflow their share. The editor shows the limit and blocks activation until the text fits or the operator chooses a larger-context model. If a component is under budget, reallocate unused tokens in order to BRAIN, files, then artifacts. Never borrow output reserve or safety margin.

5. Dependencies:
   - P3 defines the BRAIN schema and revision behavior.
   - Runtime model inventory supplies `C`; the request supplies `R` and `U`.
   - File extraction must finish before uploaded content is eligible for injection.
   - Artifact and BRAIN retrieval need project ownership checks.
6. Risks:
   - Large instructions can starve evidence. Block over-budget activation instead of truncating silently.
   - Retrieval can select stale files or artifacts. Show source, revision, and inclusion state before send.
   - Reattaching a session can create context discontinuity. Record a visible attachment event and apply it only to future turns.
   - Empty projects can look broken. Every component needs a clear empty-state action and explanation.
7. Definition of done: Done when an empty project renders all four components, each component can be populated and retrieved independently, the 100 percent budget holds with all four full, the assembled message order matches the plan, a chat can attach or remain General, and the exact included context is inspectable before sending.

Checkpoint: At a 16,384-token project budget, 4,096 + 4,096 + 4,915 + 3,277 equals 16,384.

Implementation closeout (2026-08-26): Completed with normalized stores for all four components, exact baseline arithmetic, unused-token rollover in BRAIN/files/artifacts order, ranked bounded request injection, automatic assistant-output capture, explicit future-only chat attachment events, and an authenticated no-send context preview. The responsive Project Homepage renders all four empty states and their independent lifecycle controls. A locally vendored GSAP timeline adds staged control-deck motion while `prefers-reduced-motion` bypasses it completely.

## Expose system instructions in UI

### CODE

1. What changed:
   - Add persisted runtime global instructions with a source-controlled reset.
   - Resolve and return global default, project instructions, session override, precedence, merge rule, counts, and exact effective text from authenticated endpoints.
   - Add an accessible native dialog that edits all available layers, previews the exact merged text, counts characters and estimated tokens live, saves without restart, reverts the session override, and resets the global default.
   - Preserve legacy conversation snapshots in exact replace mode until the operator saves or reverts them.
   - Send one effective system message to Ollama, with global then project then session precedence.
2. Why: The active instructions are now inspectable and editable from the application instead of being hidden in source or stale conversation metadata.
3. Risk and blast radius:
   - Project instructions now augment the global default rather than replacing it. Existing tests assert the new exact payload.
   - Token estimates use the existing approximate character-based estimator, not a model tokenizer.
   - A legacy custom snapshot stays exact until explicitly moved into layered mode.
4. How to verify:

```bash
source venv/bin/activate
python -m pytest -q tests/test_api_contracts.py
```

Open Instructions, edit all available layers, confirm the effective preview, choose Save and apply, send one message, and inspect that the first Ollama message matches the preview exactly.

5. Follow-on note: The broader Project Homepage and configurable model-context windows were subsequently implemented by the P3/P4 closeout above.

Checkpoint: The API test saves all three layers, sends the next message, and proves the first Ollama message equals the on-screen precedence result.

## Add copy message on both roles

### CODE

1. What changed: Add a Copy button to each completed user and assistant message header. It copies the raw source string, reports Copied in a polite live region, and restores the label after two seconds. Pointer devices reveal actions on message hover or focus. Coarse-pointer and no-hover devices keep them visible.
2. Why: Raw source preserves Markdown fences and syntax that rendered text would lose.
3. Risk and blast radius: The fallback uses the browser copy command only when the Clipboard API is unavailable. Image-only content copies a neutral `[image]` marker. The message-level action remains in the header and does not overlap fenced code content.
4. How to verify: Copy one user message and one assistant message containing fenced Markdown, paste both into a plain-text field, confirm the fences survive, then tab to each Copy control and confirm the two-second state reset.
5. Not done: No per-code-block copy control is added.

## Add lightweight notepad

### CODE

1. What changed:
   - Add one plain-text `notepad` field per project and authenticated read and autosave endpoints.
   - Add a Notepad toggle inside Chat, a plain textarea, save status, Close, and Send to chat.
   - Add Add to notepad on completed user and assistant messages. It appends only text selected inside that message.
   - Debounce autosave at 500 milliseconds and keep note content out of local preference storage.
2. Why: The notepad stays a small project scratch surface instead of becoming a second editor or artifact system.
3. Risk and blast radius: A project must be selected or attached. Autosave failures stay visible and leave the current text in the textarea for retry. Sending a note does not clear it.
4. How to verify: Save different notes in two projects, reload, confirm isolation, append a selection from each message role, then send the note and confirm it appears as one user message.
5. Not done: Version history, collaboration, formatting controls, and export remain explicitly out of scope.

## Neutralize chat option icon set

### REVIEW

1. Verdict: Replace the composer emoji mix with a small vendored subset of Lucide outline SVGs, and keep visible text where icons cannot distinguish the action reliably. Lucide uses the ISC license, with some Feather-derived icons covered by MIT. Preserve the required license notices when vendoring files. Source: [Lucide license](https://github.com/lucide-icons/lucide/blob/main/LICENSE).
2. Findings by severity:

Blocking findings: None. Every current option has an accessible label or visible text, so the emoji do not block keyboard or screen-reader operation.

Important findings:

Audit boundary: the composer and attachment options in Chat. Message feedback and global navigation controls are separate surfaces. All emoji rows below carry platform-specific rendering and personality weight; the transcribe sequence carries the strongest novelty weight.

| Action | Current glyph | Rating | Finding |
|---|---|---|---|
| Attach image | `📷` plus Image | Clear | The action is understandable, but emoji rendering varies by platform. |
| Attach audio file | `🎤` plus Audio | Ambiguous | It resembles live dictation and competes with the separate Dictate control. |
| Transcribe selected audio | `🗣️→✍️` | Misleading | The novelty sequence is visually noisy and can imply speech generation or note writing. |
| Start dictation | `🎙️` | Ambiguous | It is too similar to the Audio upload glyph. |
| Stop dictation | `⏹️` | Clear | Meaning is clear during recording, but the emoji style still shifts across platforms. |
| Attach generic file | `📎` plus File | Clear | Familiar meaning and a visible label already make this effective. |
| Enable Support mode | No icon, Support text | Clear | Text is the correct treatment for an application-specific mode. |
| Reveal attachments | No icon, Attach text | Clear | Keep the explicit text rather than adding a generic menu glyph. |
| Send message | No icon, Send text | Clear | Keep the explicit text because it is faster to scan and translate than an arrow glyph. |

Nice to have: Apply the same family later to project, template, history, search, and model controls so the surrounding surface stops mixing outline SVG, emoji, and text-only actions.

3. What is already working and should be preserved:
   - Keep Image, Audio, File, and Support as visible labels.
   - Keep native button and label elements, accessible names, keyboard focus, and 44-pixel touch targets.
   - Keep recording state as an explicit visual change rather than an animation.
   - Keep message Copy and Add to notepad as text because their meanings are clearer than standalone glyphs.
4. Proposed mapping:

| Action | Current | Proposed Lucide icon | Rationale |
|---|---|---|---|
| Attach image | `📷` | `Image` | Neutral depiction of a file image rather than a camera action. |
| Attach audio file | `🎤` | `FileAudio` | Separates file upload from live microphone capture. |
| Transcribe selected audio | `🗣️→✍️` | Text label `Transcribe` with optional `Captions` | No standalone icon reliably communicates local speech-to-text. Keep the text label. |
| Start dictation | `🎙️` | `Mic` | Standard live microphone action. |
| Stop dictation | `⏹️` | `Square` plus accessible Stop dictation label | Standard stop-state geometry without emoji personality. |
| Attach generic file | `📎` | `Paperclip` | Familiar attachment metaphor in the same stroke family. |
| Enable Support mode | Support text | Keep Support text | The mode is product-specific and has no universal icon. |
| Reveal attachments | Attach text | Keep Attach text | A generic menu or plus icon would hide the control's purpose. |
| Send message | Send text | Keep Send text | The explicit verb is clearer than a direction-dependent arrow. |

5. Implementation closeout (2026-08-26): Vendored the six-icon Lucide sprite and ISC/Feather MIT notices under `static/vendor/lucide/`, replaced the audited composer emoji, retained visible Image, Audio, Transcribe, and File text, and kept accessible names and 44-pixel mobile targets without a runtime package or CDN request.
