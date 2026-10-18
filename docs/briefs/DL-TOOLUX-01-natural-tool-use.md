# DL-TOOLUX-01: Natural tool use

Feature brief, 2026-10-12. Decision: Dave chose option A (make tools succeed), then B (make it feel like chat), on 2026-10-12. Source revision: `origin/main` `3700674` plus the DL-SELFEDIT-01 records branch.

## Problem

A probe of ten plain-language requests (no tool names) against `qwen3-coder:30b` on walter with 21 tools selected answered five well. The failures came from the surroundings more than the model:

| # | Request | What happened | Cause |
|---|---|---|---|
| P1 | Which machines are up? | No answer | Guessed node names in parallel with `cluster.status`; two `Node alias is not configured` errors spent the error budget of 2 |
| P2 | 18% of 2,340 plus 125 | No answer | `calc.eval` requires `from_unit` and `to_unit` even for plain arithmetic |
| P3 | Outline the tools setup guide | Asked the user | Guessed `tools/setup-guide.md`; `File not found` gave no next step |
| P4 | What's new in Ollama? | Invented details | Answered from search snippets |
| P5 | Secrets in the docs folder? | Scanned an invented sentence | `secret.scan` takes text only |
| P6 | Rename the setup guide's title | Wrong file, rejected at approval | `file.search` matches file contents, not names |
| P7 | Tokyo time at 3 pm New York | Correct, but used a 2023 date | No current date in context |
| P8 | Four-step request | Stopped after the reads with an empty reply | No continuation after tool results |

The UI adds friction: tools must be picked by hand; a tool-run answer is not saved to the conversation, so follow-ups lose it; the sent text stays in the box; the ledger shows raw event kinds with doubled numbers and raw tool JSON.

## Outcome

Rerunning the ten probe requests answers more of them without new failure modes, and a tool run reads like a chat turn: the question and answer land in the conversation, and the panel says in plain words what was looked at.

## Slice A: make tools succeed (server)

| # | Change | Fixes |
|---|---|---|
| A1 | `calc.eval`: units optional; plain arithmetic returns only `value` | P2 |
| A2 | Errors that say what to do next, still fixed text with no paths: `File not found` and `Path not found` point to `file.list`/`file.search`; an unknown node alias points to `cluster.status` | P1, P3 |
| A3 | Default error budget for lifecycle runs (`POST /tools/agent/runs`) raised from 2 to 4. DaveHarness still counts every failed call; changing which failures count would change its contract, so the product default moves instead. Explicit `error_budget` values behave as before | P1, P2 |
| A4 | A tool-run context system message after the primary system message: current local date, time and zone; for a single tool root, its top-level entries and their children (bounded, secrets and ignored folders never listed, root-relative only); configured node ids (never URLs); and working rules: look before guessing, act without announcing a plan, answer directly | P1, P3, P6, P7 |
| A5 | `file.search` also returns `path_matches`: files whose root-relative path contains the query, from the same admitted walk | P6 |
| A6 | `secret.scan` accepts `path` (file or folder in a tool root) instead of `text`: bounded walk through `walk_tree`, reads through `read_admitted_bytes`, results by root-relative path and line, never the matched text; protected files are never opened | P5 |
| A7 | When a model step after tool results returns no text and no tool call, ask once more in the same step with a short nudge to continue or answer | P8 |

P4 (web answers) is option D and stays out of scope; A4's rules do not change `web.read`.

## Slice B: make it feel like chat (renderer plus one route)

| # | Change |
|---|---|
| B1 | A tool run always belongs to a conversation (created like a plain chat when none is open). `POST /tools/agent/runs/{run_id}/record` appends the run's user message and, for a `completed` run with visible text, its final answer to that conversation, once per run, from server-held values only. The renderer calls it when the run ends and reloads the conversation |
| B2 | The message box clears when a run starts; the draft returns if the run cannot be created |
| B3 | The ledger lists steps in plain words ("Searched files for “approvalPreview” · 2 matches"), without doubled numbers; raw tool output stays one click away; the approval card and harness report are unchanged |

## Exclusions

Automatic tool selection and presets (option C), web source handling (option D), DaveHarness contract changes, persisting tool calls or tool output in the conversation, Notion tools, approval semantics.

## Effects and risks

- Tool definitions change for `calc.eval`, `secret.scan` and `file.search`, so their fingerprints and the capabilities manifest change; the catalog fixture records the new `file.search` description.
- The context message costs tokens on every step; it is bounded (about 120 entries) and counted by the existing fit check.
- Never persist an empty assistant turn; failed runs record only the user turn.
- The record route trusts only the binding and snapshot, never request text.

## Acceptance

1. Unit and contract tests cover A1–A7 and B1–B3; the full suite, mypy and the renderer tests pass on Python 3.12–3.14 CI.
2. The ten probe requests rerun against walter answer at least P1, P2, P3 and P7, and none regresses.
3. A tool run's question and answer appear in the conversation and a follow-up plain chat sees them.

## Project file destination repair (2026-10-18)

Problem: the run prompt recommends root-relative filenames, but qualified file tools resolve
against the backend process directory. Approval therefore precedes a predictable root refusal.
Projects previously had no filesystem destination. Dave chose explicit assignment of an existing
allowed folder per project on October 18.

Smallest slice: Project Home saves an optional absolute `output_directory` with Project Instructions using **Save project settings**.
Create/update rejects nonexistent folders, relative paths and folders outside `DAVE_TOOL_ROOTS`.
A lifecycle run captures that directory before its first model step; its folder map and qualified
file tools use the captured directory. Approval displays that folder. Project changes during a
pause apply only to new runs. No folder is inferred from a project name or model output.
Projects without an assigned folder refuse file access; nonproject runs accept relative filenames
only with a single allowed root. Absolute paths still require global root admission.

Acceptance: a relative file write creates nothing before exact-call approval and creates the
expected bytes in the captured project directory afterward, even after changing the project
setting. Traversal, sibling-project absolute paths, escaping symlinks, revoked roots, missing
assignments and ambiguous nonproject relative paths fail. Existing BRAIN approval behavior remains
covered. No changes to approval permissions, automatic folder creation, shell execution or roots.
