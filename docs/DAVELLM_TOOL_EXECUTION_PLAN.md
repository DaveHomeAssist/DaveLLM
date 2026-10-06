# DaveLLM tool execution plan

Written 2026-10-05 by the tool architect and orchestrator session. It turns the 100 candidates in `docs/DAVELLM_TOOL_ROADMAP.md` into 22 one-shot executor prompts (EX-00 to EX-21), grouped in six phases, with a battle-test harness built first and run against every later tool. Executors are Codex or Claude Code sessions on the agent bus (`~/Code/agent-bus`, lanes L06 to L11). Nothing here approves implementation by itself: Dave queues each phase by pasting the executor kickoff and the orchestrator sends the prompts.

## 1. Phases and gates

| Phase | Lane | Prompts | Tools covered | Gate to the next phase |
|---|---|---|---|---|
| 0 Harness | L06-tools-p0-harness | EX-00 | none (harness, manifest, CI lane) | `scripts/battle_test_tools.py --offline` passes on the 25 existing tools in CI |
| 1 Cluster reliability | L07-tools-p1-cluster | EX-01, EX-03, EX-16, EX-14 | 1, 6, 7, 9, 11, 12, 69–74, 82 | all four merged; `--live` read-only run on the real cluster recorded in the lane |
| 2 Governance and history | L08-tools-p2-governance | EX-13, EX-10, EX-20, EX-06 | 17, 27, 28, 43–46, 55–62, 93, 94, 97–99 | merged; conversation summaries verified on a 12-message chat |
| 3 Approved writes | L09-tools-p3-writes | EX-09, EX-07, EX-15, EX-12 | 21, 22, 25, 26, 31–42, 50–54, 64–68 | merged; every write tool shows verified/failed/unknown in the live run |
| 4 Inference and high risk | L10-tools-p4-inference | EX-04, EX-05, EX-21, EX-02, EX-11 | 2–5, 8, 10, 13–16, 18–20, 47–49, 63, 100 | merged; inference tools stay behind `DAVE_ENABLE_INFERENCE_TOOLS`; git.push decision recorded |
| 5 Large integrations | L11-tools-p5-integrations | EX-08, EX-17, EX-18, EX-19 | 23, 24, 29, 30, 75–81, 83–92, 95, 96 | each merged separately; none required for the others |

Phase 0 must finish before Phase 1. Inside a phase the prompts are independent and may run in parallel lanes if Dave approves the cost; by default the orchestrator sends them one at a time in the order listed.

## 2. Executor contract (applies to every EX prompt)

Every prompt below says "Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2". It means all of this:

1. **Repository and isolation.** Target `DaveHomeAssist/DaveLLM`. Fetch origin, create a worktree at `~/Code/_worktrees/dave-llm/<branch>` from fresh `origin/main` on branch `codex/tools-<ex>-<family>`, and write an `.agent-claim` there (agent, session, started, scope). Never edit `~/Code/dave-llm`; other sessions hold it.
2. **Read first.** `CLAUDE.md` (trust boundaries, Ollama integration, required checks), `docs/DAVELLM_TOOLS.md`, `docs/DAVEHARNESS_PREFLIGHT.md`, `docs/DAVELLM_TOOL_ROADMAP.md`, and §3 of this file. Then `tests/tool_contract.py`, `tests/hostile_fs.py`, `tests/fake_notion.py` and `tests/test_notion_tools.py` as the pattern for fakes and contracts.
3. **Shape of a tool family.** One module `davellm_<family>.py` holding the logic; `app.py` handlers below the Notion handlers and preflights below every handler (line numbers above are pinned by `tests/test_tool_catalog_provenance.py`); one opt-in flag `DAVE_ENABLE_<FAMILY>_TOOLS`, honored only with `DAVE_ENABLE_TOOLS`; every outside target (host, page, database, entity, topic, repo, model) from an operator allowlist environment variable, never from the model; permission tier from the existing set (`read`, `read_files`, `read_system`, `public_network`, `write_files`, `write`); `cancellation: bounded`; results under 48 KiB; results and errors carry no URLs, addresses, credentials, OS error text or stack traces, only fixed messages. Secrets come from the environment only and never reach a result, log, `repr` or the capabilities manifest. Writes set `approval_required` with a read-only preflight and end `verified`, `failed` or `unknown`; an unknown write is never repeated in the run. No tool posts to `/v1/chat/completions`. No tool runs inference unless its prompt names `DAVE_ENABLE_INFERENCE_TOOLS`, and nothing touches H9.
4. **Tests.** `tests/test_<family>_tools.py` with a fake for every outside service (no network in tests), schema rejection cases, refusal cases, result-size and leak cases, and approval/preflight cases for writes. Path tools pass `tests/tool_contract.py` against `hostile_tree`. Add the family to `tests/fixtures/davellm/battle_manifest.json` and make `scripts/battle_test_tools.py --offline --family <family>` pass.
5. **Docs.** Add the tools to `docs/DAVELLM_TOOLS.md`, add one trust-boundary bullet to `CLAUDE.md`, update the catalog fixture if the test tells you to, and rerun `scripts/generate_capabilities_manifest.py`.
6. **Checks and runner placement.** Run every required check listed in `CLAUDE.md`. Three SSH runners exist (see `~/Code/ops-hub/90-governance/MACHINE_ACCESS.md`, verified 2026-10-05): **Dominic** (Ubuntu, Python 3.12.3, 8 cores, 15 GB) is the Linux lane and matches CI, so `pytest -q` and the battle harness run there first; **Walter** (Windows 11, `py -3.13` and `py -3.12`, RTX 3070, Node 22) and **Duncan** (Windows 10, `py -3.12`, 80 GB RAM, Node 20) run the same suite as a Windows lane when the Linux lane is green, and they are the targets for every live inference or GPU check. Use a scoped temporary checkout on the runner (never an existing project folder; on Duncan not drive C: for large files), run Python with the explicit `py -3.12` launcher on Windows, and expect POSIX-only tests (symlink and descriptor-walk fixtures) to skip there; report a Windows-only failure under Open rather than patching around it. Never run the full suite on the Mac while Dave is using it; the Mac is for bounded checks only (`py_compile`, `node --check`, `battle --offline --family <family>`, the family's own pytest module), and the report must name the actual host for every check. If no runner answers `ssh -o BatchMode=yes -o ConnectTimeout=8 <runner> hostname`, report the full suite as not run.
7. **Delivery.** Run the overlap check (`gh pr list --repo DaveHomeAssist/DaveLLM --state open --json number,headRefName,files`). One PR per prompt, titled `tools: <family> (<ex>)`, body listing the tools, flags, allowlist variables and the checks with their hosts. One CI round. Merging is a gated action: do it only when the prompt says "merge allowed", with a merge commit, after `checks (3.12)`, `(3.13)` and `(3.14)` pass and after reading any Codex review comments. Never squash, rebase, force-push or delete branches.
8. **Report.** File the bus report in the `AGENTS.md` format with the PR URL, head SHA, CI run URL and conclusion, battle-report path in the lane's `artifacts/`, hosts used, and anything out of scope under Noticed. Write the Agent Communications Log entry. Remove your `.agent-claim` when done.

## 3. Battle-test harness (built by EX-00)

Purpose: every new tool is attacked the same way before it ships, and the attack set grows with the catalog.

`scripts/battle_test_tools.py` (Python, no new dependencies beyond `requirements-dev.txt`; `hypothesis` is already there):

- **Manifest.** `tests/fixtures/davellm/battle_manifest.json` lists each family: its flag, its tools, the fake module that stands in for its outside service, disposable live targets (environment variable names, never values), and per-tool expectations (`refusal`, `mutates`, `inference`, `approval`).
- **Offline mode** (`--offline`, default; runs in CI): builds the registry with every family flag on and the fakes injected, then for each tool runs
  1. *Schema fuzz*: hypothesis generates arguments from the JSON schema plus boundary cases (maxLength+1, minimum−1, wrong types, extra properties, nulls where not allowed); every case must fail validation inside DaveHarness with no exception escaping.
  2. *Hostile inputs*: path tools get `hostile_tree`; Git tools get `hostile_git`; service tools get the fake in hostile mode (429, 500, 529, timeouts, oversized bodies, redirects, malformed JSON, secret-looking strings in responses). Refusals must use the tool's fixed message.
  3. *Leak scan*: every result and error is scanned for planted sentinels, absolute paths outside the tool root, `http(s)://`, IP addresses, the fake's token, and anything matching the secret patterns in the harness; one hit fails the tool.
  4. *Budget*: result bytes ≤ 48 KiB; handler finishes within `timeout_seconds` when the fake stalls, with termination `deadline_abandoned` and no leaked thread holding a lock.
  5. *Approval gate*: an approval-required tool produces no side effect on the fake before approval; its preflight refuses the planted bad case and passes the good case; resume with a wrong digest or expired nonce is refused.
  6. *Write outcome*: a write under a fake that answers 429/529 reports `unknown` or `failed` and is not repeated in the same run; a write under a fake that drops the verification read reports `unknown`.
  7. *Cancellation*: a cancelled run leaves the fake with at most one in-flight call and the ledger clean.
- **Live mode** (`--live`, gated by `DAVE_BATTLE_LIVE=1`, never in CI): read-only tools run against the real targets from the environment; write tools run only against disposable targets named in the manifest (a scratch Notion page, a scratch repo, a scratch notify topic) and verify, then revert where the tool supports it. Inference tools run only with `DAVE_ENABLE_INFERENCE_TOOLS=true` and a token cap passed on the command line, and never on H9 corpora.
- **Output**: `battle-report.json` (per tool: checks run, pass/fail, bytes, elapsed, refusal strings seen) and a Markdown summary, both copied to the lane's `artifacts/`. Exit code non-zero on any failure.
- **CI**: `.github/workflows/ci.yml` gains the step `python scripts/battle_test_tools.py --offline` on all three Python lanes; `tests/test_battle_harness.py` proves the harness itself catches a planted leaking tool, a planted unbounded tool and a planted unguarded write.

## 4. Execution prompts

Each prompt is one-shot and paste-ready for a bus executor. The orchestrator sends it with `bin/bus send <lane> -`.

### EX-00 — Battle-test harness (Phase 0, lane L06)

```text
Build the DaveLLM battle-test harness described in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §3, in DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex00-harness.

Deliver: scripts/battle_test_tools.py with --offline (default), --live (gated by DAVE_BATTLE_LIVE=1), --family, --tool, --report-dir and --token-cap; tests/fixtures/davellm/battle_manifest.json covering the 25 tools already in the catalog (file, git, md, web, shell, system, chat.search, cluster.status, project, notion) with tests/fake_notion.py and the existing hostile fixtures as their fakes; tests/test_battle_harness.py that plants a leaking tool, an unbounded tool and an unguarded write in a throwaway registry and proves the harness fails each one; the CI step on all three Python lanes; a short docs/BATTLE_TESTS.md explaining modes, manifest fields and how a family registers itself.

Rules: no new runtime dependency; hypothesis from requirements-dev.txt only; the harness imports the registry through the same builder app.py uses, with every family flag on and fakes injected by monkeypatching the module-level clients, never by editing tool code; the offline run must finish under 3 minutes on Dominic; the 25 existing tools must pass unchanged, and if one does not, report it under Open with the exact check instead of changing the tool.

Allowed gated actions: none (open the PR; do not merge). Report: PR URL and head SHA, CI run URL and conclusion, the offline summary line per family, the host that ran pytest, and the battle-report path in the lane's artifacts.
```

### EX-01 — cluster.models (Phase 1, lane L07; tools 1, 9, 11, 12)

```text
Add the cluster.models tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex01-cluster-models, flag DAVE_ENABLE_CLUSTER_TOOLS, module davellm_cluster.py.

Tools (all permission read_system, no approval, bounded, 10 s):
- cluster.models: for every configured node, the installed models (GET /api/tags), the loaded models with size, VRAM share and expires_at (GET /api/ps), the context length each loaded model is running with (context_length from /api/ps when the Ollama version reports it, otherwise "unknown"), and the Ollama version (GET /api/version). Nodes that do not answer are listed as unreachable with no error text.
- cluster.model_drift: compares each node's installed models with DAVE_EXPECTED_MODELS (JSON, node id to list of model names) and lists missing and unexpected models per node; with the variable unset it says no expectation is configured.
Reuse the node inventory, the 2 s ollama_loaded_models probe and the DAVE_NODE_TIMEOUT tags probe already in app.py; do not add a second HTTP client. Never return node URLs or addresses; name nodes by id and display name only. Extend cluster.status's tests as the pattern.

Tests: a fake Ollama transport that answers tags, ps, version and show for two nodes, one node that times out, one that returns malformed JSON, one whose version lacks context_length. Register the family in the battle manifest and pass battle --offline --family cluster.

Allowed gated actions: none. Report: PR URL and head SHA, CI conclusion, hosts used, and one real cluster.models result captured read-only from the running router if DAVE_ENABLE_CLUSTER_TOOLS can be set on a throwaway router on a spare port (DAVE_DATA_DIR scratch); otherwise say it was not captured.
```

### EX-03 — node.diagnose and node.wake (Phase 1, lane L07; tools 6, 7, 73)

```text
Add the node.diagnose and node.wake tools to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex03-node-diagnose, flag DAVE_ENABLE_CLUSTER_TOOLS (shared with EX-01; if EX-01's PR is open and unmerged, build on origin/main and keep your module separate: davellm_node_diag.py).

node.diagnose (read_system, bounded, 20 s): for one configured node id, run in order and stop at the first failure: Tailscale peer state from `tailscale status --json` on the router host (online, last seen, OS) if the binary exists, else "tailscale: not available"; a TCP connect to the node's Ollama port with a 3 s limit; GET /api/version; GET /api/tags timed. Return one verdict from a fixed list: online, slow (tags over 5 s), ollama_down (port closed, peer online), asleep_or_unplugged (peer offline), unknown. Include the timings. Never include addresses.
node.wake (write, approval required, bounded, 10 s): sends a Wake-on-LAN magic packet for a node named in DAVE_WAKE_TARGETS (JSON, node id to {"mac": "...", "broadcast": "..."}); the preflight refuses unknown ids and malformed MACs; the result is "sent" plus a 15 s re-probe verdict from node.diagnose. The result never echoes the MAC.
tailscale.status (read_system): the peer list for configured node ids only (online flag and last seen), nothing else.

Tests: fake tailscale output (present, absent, malformed), fake sockets for open/closed/slow ports, fake Ollama answers, fixed verdict strings. Battle manifest entry.

Live check (read-only, allowed): run node.diagnose against dominic, walter and duncan from a throwaway router on the Mac and record the three verdicts with timings in the report. Do not send a wake packet; note under Open whether each node's hardware is known to support Wake-on-LAN (it is unverified today).

Allowed gated actions: none. Report as in §2.
```

### EX-16 — server.ops (Phase 1, lane L07; tools 69, 70, 71, 72, 74)

```text
Add the server.ops tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex16-server-ops, flag DAVE_ENABLE_SERVER_TOOLS, module davellm_server_ops.py.

Tools, all read_system, bounded:
- host.disk: free and used space for the router host's volumes (df), flagging any volume under 10% free, plus whether each path in DAVE_WATCH_MOUNTS (JSON list) is mounted.
- host.pressure: memory used, swap used, load average and the five largest processes by RSS on the router host (vm_stat, sysctl and ps on macOS; /proc on Linux); process names only, no arguments.
- service.health: GET each URL in DAVE_SERVICE_HEALTH (JSON, name to URL) with a 5 s limit through davellm_public_http rules relaxed only for tailnet addresses the operator configured; returns name, status code class and elapsed; never the URL.
- docker.ps and docker.logs: run over SSH to a host in DAVE_SSH_HOSTS (JSON, name to {"host":..., "user":..., "key": path}) using a fixed argument list (ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no, no shell on the remote beyond `docker ps --format json` and `docker logs --tail N <container>`); container names come from an allowlist DAVE_DOCKER_CONTAINERS; docker.logs is capped at 200 lines and 32 KiB and strips anything matching the harness secret patterns.
No tool takes a host, URL or command from the model; the model names things by configured name only.

Tests: fake subprocess runner for df, vm_stat, ps and ssh (success, nonzero exit, timeout, oversized output, secret-looking lines); fake HTTP for service.health; refusal of unknown names. Battle manifest entry.

Live check (read-only, allowed): host.disk and host.pressure on the Mac from a throwaway router; service.health against the SearXNG healthz on dominic if dominic is reachable; docker.ps on dominic if its SSH is reachable. Record results and hosts.

Allowed gated actions: none. Report as in §2.
```

### EX-14 — notify.push (Phase 1, lane L07; tool 82)

```text
Add notify.push to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex14-notify, flag DAVE_ENABLE_NOTIFY_TOOLS, module davellm_notify.py.

notify.push (write, approval required for model-initiated calls, bounded, 10 s): POSTs a title and a message of at most 500 characters to the one topic URL in DAVE_NOTIFY_URL (an ntfy-compatible endpoint; optional bearer in DAVE_NOTIFY_TOKEN) with priority low|default|high. The model never supplies a URL or topic. The result is sent, failed or unknown (timeout after the request was written).

Router hooks (not tools, no approval, same transport): when DAVE_NOTIFY_EVENTS lists them, send a push when an agent run pauses for approval, when a streamed reply longer than DAVE_NOTIFY_SLOW_SECONDS (default 120) finishes, and when a node that was online fails two consecutive health probes. Each hook is rate-limited to one push per event type per 5 minutes and is skipped when the Electron window is focused (reuse the existing focus signal if one exists; otherwise skip that condition and say so). Hooks must never block the stream or the event loop.

Tests: fake HTTP endpoint (200, 401, 429, timeout), message truncation, rate limit, hook firing on a fake slow stream and a fake node drop; no network in tests. Battle manifest entry.

Live check (allowed): if DAVE_NOTIFY_URL is set in the environment Dave provides to the lane, send exactly one test push with the text "DaveLLM notify test" and record the HTTP status; otherwise report it as not run. Do not create an ntfy server; note under Noticed whether one is already reachable on dominic.

Allowed gated actions: none. Report as in §2.
```

### EX-13 — ops.governance (Phase 2, lane L08; tools 57–62)

```text
Add the ops.governance tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex13-governance, flag DAVE_ENABLE_GOVERNANCE_TOOLS, module davellm_governance.py. Paths come from DAVE_GOVERNANCE_ROOT (the ops-hub checkout), DAVE_COMMS_DIR (the daily communications folder) and DAVE_NEXTSTEPS_BOARD (the board HTML path); all three are admitted with resolve_extended_tool_path semantics and the secret denylist.

Tools:
- rules.lookup (read_files): returns the paragraph for one rule id such as COMMS-3 or NEXT-2 from 90-governance/WORKSPACE_OPERATING_RULES.md, or the nearest ids when none matches.
- machine.access.lookup (read_files): the section for one machine name from 90-governance/MACHINE_ACCESS.md, with any line that looks like a credential removed.
- comms.log.read (read_files): today's entries (or a given date's) from DAVE_COMMS_DIR, newest first, limited to N entries and 48 KiB.
- comms.log.append (write_files, approval, preflight): inserts one entry in the required template directly under "# 📡 Recorded Agent Output", then reads the file back and confirms the entry is present; the preflight refuses an entry missing the session title, status or canonical record lines.
- nextsteps.read (read_files): the DaveLLM entry's status, next step and open decisions from docs/NEXT_STEPS.json in a named repo under the tool roots.
- nextsteps.update (write_files, approval, preflight): runs the governed updater documented in ops-hub/tools/next-steps/README.md for one project id with a fixed argument list, then runs it again with --check, and reports both outcomes; it never edits the board HTML directly.

Tests: fixture copies of the rules file, access guide, a comms day file and a board; the comms append must prove placement and readback; the updater is a fake executable. Battle manifest entry.

Allowed gated actions: none. Report as in §2, including whether the real updater path exists on the Mac.
```

### EX-10 — github (Phase 2, lane L08; tools 43, 44, 45, 46, 55, 56)

```text
Add the github tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex10-github, flag DAVE_ENABLE_GITHUB_TOOLS, module davellm_github.py. Transport: GitHub REST over davellm_public_http with DAVE_GITHUB_TOKEN (fine-grained, read-only except issues:write when gh.issue.create is enabled); repositories only from DAVE_GITHUB_REPOS (JSON list of owner/name). Vercel through DAVE_VERCEL_TOKEN and DAVE_VERCEL_PROJECTS (JSON list), read-only.

Tools (public_network, bounded, 15 s):
- gh.pr.status: state, head SHA, mergeable, required checks with conclusions, review decision, for one PR number in one configured repo.
- gh.pr.comments: review comments and issue comments on a PR, newest first, bodies truncated at 1,000 characters, capped at 30; the result flags comments from accounts in DAVE_GITHUB_REVIEW_BOTS (default ["chatgpt-codex-connector"]) so the model reads them before any merge.
- gh.ci.logs: for one failing check run, the last 150 lines of its job log with secret-pattern lines removed.
- gh.pages.status: the latest Pages build status and the deployed commit for a configured repo.
- vercel.deploy_status: the latest production and preview deployment state for a configured project.
- gh.issue.create (write, approval, preflight): title and body in a configured repo; the preflight refuses bodies with secret patterns; the result includes the issue number and is verified by a GET.
Rate limits (403 with X-RateLimit-Remaining 0, or 429) report "rate limited" and a retry-after in seconds; nothing else from the error body reaches the model.

Tests: fake GitHub and Vercel servers covering happy paths, 404, 403 rate limit, oversized logs, comment flagging, issue creation and verification. Battle manifest entry.

Live check (read-only, allowed, only if DAVE_GITHUB_TOKEN is provided to the lane): gh.pr.status on DaveHomeAssist/DaveLLM pull 59 from a throwaway router; record the result shape, not the token.

Allowed gated actions: none. Report as in §2.
```

### EX-20 — data.tools (Phase 2, lane L08; tools 93, 94, 97, 98, 99)

```text
Add the data.tools family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex20-data-tools, flag DAVE_ENABLE_DATA_TOOLS, module davellm_data_tools.py. All tools are permission read (or read_files when they open a file in a tool root), bounded, no approval.

- calc.eval: evaluates one arithmetic expression through an AST whitelist (numbers, + - * / // % **, parentheses, unary minus, and the functions sqrt, abs, round, min, max, floor, ceil, log, log10, sin, cos, tan, pi, e); exponent bounded to keep results under 10^308; rejects names, attributes, calls outside the list and strings. Unit conversion for length, mass, temperature, data size, time and speed from a fixed table; no library.
- time.convert: converts an ISO timestamp or "now" between IANA zones with zoneinfo; lists offsets; refuses zones not in zoneinfo.
- data.query: runs one read-only SQL statement over CSV, TSV, JSON or JSONL files inside a tool root using sqlite3 in memory (load the file with a bounded reader, 50 MiB cap, 200,000 rows cap); only SELECT and WITH statements are accepted (parse the first keyword after stripping comments; refuse ATTACH, PRAGMA, semicolons, and any write); results capped at 200 rows and 48 KiB.
- json.validate: validates a JSON document (inline or a file in a tool root) against a JSON Schema (inline or a file), using the validator DaveHarness already uses; returns the first 20 errors with paths.
- diagram.render: renders Mermaid text to SVG with the local `mmdc` binary if DAVE_MMDC_PATH is set and the file exists, with a 15 s limit and a 256 KiB output cap, writing the SVG into DAVE_RENDER_DIR inside a tool root and returning the root-relative path; otherwise returns "renderer not configured" (no network fallback).

Tests: expression attacks (dunder access, huge exponents, import tricks), zone edge cases, SQL injection and write attempts, oversized files, schema errors, renderer absent/present/timeout with a fake binary. Battle manifest entry.

Allowed gated actions: none. Report as in §2.
```

### EX-06 — chat.history (Phase 2, lane L08; tools 17, 27, 28)

```text
Add the chat.history family and fix conversation summaries in DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex06-chat-history, flag DAVE_ENABLE_HISTORY_TOOLS, module davellm_history.py.

Tools:
- chat.read (read): one of the run user's own conversations by id, as numbered turns with role, timestamp and text, paged at 400 lines, images and attachments described by type and size only. Reuse the ownership check chat.search uses.
- chat.export (write_files, approval, preflight): writes one conversation as Markdown into DAVE_EXPORT_DIR inside a tool root with a name derived from the title and date; refuses to overwrite; returns the root-relative path and byte count.
- chat.summarize (read; inference, so it is registered only when DAVE_ENABLE_INFERENCE_TOOLS=true as well): requests a fresh summary for one conversation and returns the job id and, if already finished, the summary.

Summary fix (the main deliverable): generate_conversation_summary currently runs a blocking 10 s call on the first node with models, on the event loop, for every chat past 10 messages, and has never produced a summary on the real cluster. Replace it with a background job (asyncio task per conversation, deduplicated) that picks a node where the conversation's model is already loaded according to ollama_loaded_models, falling back to the smallest configured model on any online node, with a 120 s budget, native /api/chat through davellm_ollama with num_ctx from chat_num_ctx and keep_alive from chat_keep_alive, retrying once on failure and recording the failure reason in GET /monitoring/health as summary_failures. prepare_history_for_prompt must use the last successful summary (or the placeholder) without waiting. Keep every existing contract test green; the SSE and /chat contracts do not change.

Tests: fake transport proving the stream never waits on the summary, deduplication, node choice, retry, failure accounting, chat.read paging and ownership, export refusal cases. Battle manifest entry.

Live check (allowed, inference): on a throwaway router against walter with llama3:latest, create a 12-message scratch conversation (DAVE_DATA_DIR scratch) and confirm a summary appears within 120 s; record the elapsed time and token counts. No other inference.

Allowed gated actions: none. Report as in §2.
```

### EX-09 — Notion adapter v2 (Phase 3, lane L09; tools 33–42)

```text
Extend the Notion adapter in DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex09-notion-v2. Same flag DAVE_ENABLE_NOTION_TOOLS, same module davellm_notion.py, same transport, ledger and verification rules as the existing three tools; new databases only from DAVE_NOTION_DBS (JSON, configured name to database id). Build on origin/main after PR #58 is merged; if it is not merged, stop and report needs-decision.

New tools:
- notion.search (read): title search limited to results whose parent is a configured page or database; returns configured names and titles, never Notion ids.
- notion.db.query (read): rows of a configured database with up to three property filters (equals, contains, checkbox, date before/after) and one sort, paged at 50, properties flattened to plain text.
- notion.db.create_row (write, approval, preflight): one row with plain-text, select, multi-select, checkbox, date and relation-by-configured-page properties; verified by a GET; unknown on timeout and never repeated in the run.
- notion.db.update_props (write, approval, preflight): changes named properties of one row returned by notion.db.query in this run; the preflight re-reads the row and refuses if any targeted property changed since it was read.
- notion.page.create (write, approval, preflight): a child page under a configured page with up to 50 plain blocks, verified by reading it back.
- notion.comment.add (write, approval): one comment on a configured page.
- notion.page.diff (read): compares a configured page's current blocks with the copy this run read earlier and lists added, removed and changed block refs; this is the guard the write preflights share.
- notion.log_session (write, approval, preflight): creates one row in the database configured as "session-log" with title, status, canonical record and summary, then reads it back.
- notion.routines.read (read): rows of the database configured as "routines", flattened.
- notion.inbox.triage (read): rows of the database configured as "inbox" with an empty routing property, oldest first, capped at 25.

Tests: extend tests/fake_notion.py with databases, query filters, comments, child pages and the hostile cases (429, 529, 500 once, changed-since-read, oversized rows); keep the 203 existing Notion tests green; add adversarial cases in the style of tests/test_notion_adversarial.py. Battle manifest entry with the scratch page and a scratch database as disposable live targets.

Allowed gated actions: none, and no live Notion writes in this prompt. Report as in §2 and list which tools need a live acceptance run next.
```

### EX-07 — memory (Phase 3, lane L09; tools 21, 22, 25, 26, 31, 32)

```text
Add the memory tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex07-memory, flag DAVE_ENABLE_MEMORY_TOOLS, module davellm_memory.py. Roots from DAVE_MEMORY_ROOTS (JSON, name to absolute folder: for example "davai" for the executive memory checkout, "claude" for the agent memory folder, "glossary" for a folder holding glossary.md); every path is admitted through resolve_extended_tool_path and the secret denylist; everything under these roots is read-only except the proposals file below.

Tools:
- memory.recall (read_files): literal search across the configured roots and the run's project BRAIN, returning root name, root-relative path, line and a 200-character snippet, capped at 30 hits; honors the frontmatter `description` line as a match field when present.
- memory.propose (write_files, approval, preflight): appends one proposed memory (name, description, type, body) to DAVE_MEMORY_PROPOSALS (a Markdown file inside one of the roots) with a timestamp; never writes a memory file directly; the preflight refuses duplicates by name.
- project.notepad.write (write, approval, preflight): replaces or appends plain text in the run's project notepad; the preflight shows the before and after and refuses when the notepad changed since the run started (compare with the frozen run context).
- project.brain.pin (write, approval, preflight): pins or unpins one BRAIN item by its id from project.brain.read in this run, through the existing BRAIN revision functions in project_context.py; never edits the revision text.
- glossary.lookup (read_files): the definition lines for one term from glossary.md, case-insensitive, with up to three nearest terms when none matches.
Native read tools take user and project only from HOST_RUN_CONTEXT, as the existing project tools do.

Tests: fixture roots with planted secrets and symlinks under tool_contract; proposals append and duplicate refusal; notepad and pin changed-since-read refusals through the lifecycle fixture; the memory roots never listed outside their names. Battle manifest entry.

Allowed gated actions: none. Report as in §2.
```

### EX-15 — home.assistant (Phase 3, lane L09; tools 65, 66, 67, 68)

```text
Add the home.assistant tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex15-home-assistant, flag DAVE_ENABLE_HA_TOOLS, module davellm_home_assistant.py. Transport: the Home Assistant REST API at DAVE_HA_URL with a long-lived token in DAVE_HA_TOKEN, through davellm_public_http rules relaxed only for that one configured tailnet address, 10 s limit, no redirects. Entities only from DAVE_HA_ENTITIES (JSON, friendly name to entity id); services only from DAVE_HA_SERVICES (JSON list of domain.service).

Tools:
- ha.state.get (read): state and the attributes the operator lists per entity (default: friendly_name, unit_of_measurement, last_changed) for one or all configured entities.
- ha.history (read): state changes for one configured entity over the last N hours (max 168), capped at 200 points.
- ha.config.check (read_system): POST /api/config/core/check_config and return valid or the first 10 error lines with paths removed.
- ha.service.call (write, approval, preflight): one configured service on one configured entity with a fixed set of data keys (brightness_pct, color_temp, temperature, value); the preflight refuses unknown services or entities and any extra key; the result re-reads the entity state after 2 s and reports verified when it changed as expected, unknown otherwise.
Entity ids never appear in results; friendly names do.

Tests: fake HA server with the four endpoints, 401, 404, slow responses, an entity that does not change after a call, and oversized histories. Battle manifest entry.

Live check (read-only, allowed, only if DAVE_HA_URL and token are provided to the lane): ha.state.get for the configured entities from a throwaway router; no service calls.

Allowed gated actions: none. Report as in §2.
```

### EX-12 — code.check (Phase 3, lane L09; tools 50, 51, 52, 53, 54, 64)

```text
Add the code.check tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex12-code-check, flag DAVE_ENABLE_CODE_TOOLS, module davellm_code_check.py. Runners from DAVE_RUNNERS (JSON, name to {"host", "user", "key", "cpu", "gpu", "tags"}); heavy jobs run over SSH with the fixed-argument pattern from davellm_server_ops.py (EX-16); nothing runs an arbitrary command.

Tools:
- runner.select (read_system): picks the runner whose tags match a requested capability (pytest, node, gpu, whisper) and whose SSH answers `true` within 5 s, preferring remote runners over the router host; returns the runner name and why.
- test.run (read_files for the repo, execution on a runner; approval required because it executes): runs `python -m pytest -q <selector>` in the admitted repository (synchronized to the runner with a fixed rsync argument list, excluding .git, venv, node_modules and the secret denylist) with a 15-minute cap, returning the summary line and the last 100 lines; refuses when no remote runner is available unless DAVE_CODE_TOOLS_ALLOW_LOCAL=true.
- lint.run (read_files, local, bounded 60 s): ruff for Python and biome or eslint for JavaScript when the binaries exist in the repo's venv or node_modules, output capped at 200 lines.
- dep.audit (read_files, local, bounded 120 s): `pip-audit` on requirements files when installed, and `npm audit --json` when package-lock.json exists; returns counts by severity and the top 20 advisories.
- code.symbols (read_files): functions, classes and methods with line numbers for a Python file via ast, and top-level functions, classes and exports for a JavaScript file via a bounded regex scan.
- code.grep_regex (read_files): regular-expression search inside a tool root, run in a subprocess with a 5 s wall limit and 256 MiB address-space limit, pattern length capped at 200, hits capped at 200; the subprocess is killed on timeout.

Tests: fake ssh and rsync runners, fake lint and audit binaries, regex catastrophic patterns under the subprocess limit, symbol extraction fixtures, runner selection with one dead runner. Battle manifest entry.

Allowed gated actions: none. Report as in §2 and note under Open whether the Mac has ruff, pip-audit and biome installed.
```

### EX-04 — route.plan (Phase 4, lane L10; tools 13, 16)

```text
Add route.plan and token.count to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex04-route-plan, flag DAVE_ENABLE_CLUSTER_TOOLS, module davellm_route_plan.py.

- token.count (read): estimates prompt tokens for a given text, or for the run's current conversation plus a draft, using the same estimator the stream's waiting status uses (prompt_size_estimate), and compares it with each configured node profile's limit.
- route.plan (read_system): for a draft (text) and an optional preferred model, returns a ranked list of node and model pairs with a one-line reason each, built from the DL-ROUTE node profiles, the loaded models from ollama_loaded_models, in-flight counts from NODE_ACTIVITY, the warm-prefix rule (reads_only_new_message) and the model's context window from chat_num_ctx. It never reroutes a chat; the UI keeps the explicit node and model. The result names nodes by id and display name only.
Do not duplicate estimation or profile code: import the functions app.py already uses, and if they live above the pinned handlers leave them there and import by name.

Tests: fixture profiles for three nodes, loaded-model variations, in-flight counts, warm and cold cases, an oversized draft, a model not loaded anywhere. Battle manifest entry.

Live check (read-only, allowed): one route.plan call from a throwaway router against the real cluster with a 1,500-token draft; record the ranking and timings.

Allowed gated actions: none. Report as in §2.
```

### EX-05 — model.ask (Phase 4, lane L10; tools 14, 15)

```text
Add model.ask and model.consensus to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex05-model-ask, flags DAVE_ENABLE_CLUSTER_TOOLS and DAVE_ENABLE_INFERENCE_TOOLS (both required), module davellm_model_ask.py.

- model.ask (write permission tier because it spends compute; approval required; bounded; 180 s): sends one question (max 4,000 characters) with an optional system line to one configured node and model, through davellm_ollama.ollama_chat_bounded on native /api/chat with num_ctx from chat_num_ctx, keep_alive from chat_keep_alive, num_predict capped by DAVE_INFERENCE_TOOL_MAX_TOKENS (default 1,024); returns the answer, prompt and completion tokens and elapsed time. The preflight refuses when the run's cumulative inference tokens would exceed DAVE_INFERENCE_TOOL_BUDGET (default 8,000 per run) or when the model is not in that node's inventory.
- model.consensus (same tier, approval, 300 s): the same question to two or three configured node and model pairs in parallel, returning each answer, a disagreement flag computed by a cheap token-overlap ratio, and the budget consumed; the preflight applies the same budget across all pairs.
The run's inference ledger lives beside the Notion ledger (per run id, dropped with the run) and is reported in the run's events. Nothing here touches H9 corpora, qualification scripts or /v1.

Tests: fake native transport with answers, tool-call-only replies, timeouts, 400 errors; budget accounting across calls and across the two tools; approval and preflight refusals; cancellation leaves at most one in-flight call. Battle manifest entry with inference: true so the harness skips it offline and caps it live.

Live check (allowed, inference, cap 3,000 tokens total): one model.ask to walter llama3:latest and one model.consensus across walter llama3:latest and duncan llama3:latest, from a throwaway router; record tokens and elapsed time.

Allowed gated actions: none. Report as in §2.
```

### EX-21 — quality (Phase 4, lane L10; tools 10, 18, 19, 20)

```text
Add the quality tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex21-quality, flag DAVE_ENABLE_QUALITY_TOOLS (inference tools additionally require DAVE_ENABLE_INFERENCE_TOOLS), module davellm_quality.py.

- perf.read (read_system): read-only queries over performance.db and cost_log.jsonl through fixed named views (latency by node and model over N days, tokens per second by model, cost by day, failure counts by reason); no free SQL; capped at 200 rows.
- prompt.lint (read): checks a prompt against a rules file DAVE_PROMPT_RULES (Markdown with one rule per heading, shipped with a default covering placeholders, unstated target, missing deliverable, options instead of defaults, and secrets) and returns rule id, line and a fix hint per finding.
- cluster.benchmark (inference, approval, 300 s): sends a fixed 200-token prompt to one node and model three times with num_predict 128 and returns prompt and generation tokens per second, cold versus warm, through the same transport and budget ledger as model.ask (EX-05; if EX-05 is unmerged, build the ledger here under the same module name and say so).
- eval.run (inference, approval, 600 s): runs a reference set DAVE_EVAL_SET (JSON file in a tool root: id, prompt, expected substrings or regex, max tokens) against one node and model, capped at 25 cases per run and by the inference budget; returns pass counts and per-case verdicts; writes the run as a JSON record into DAVE_EVAL_DIR inside a tool root. It never reads H9 corpora, and the preflight refuses any set path containing "h9" or "qualif".

Tests: fixture performance.db and cost log, rules file cases, fake transport for benchmark and eval, budget refusals, forbidden set paths. Battle manifest entry.

Live check (allowed, inference, cap 4,000 tokens): cluster.benchmark on walter llama3:latest once; no eval.run.

Allowed gated actions: none. Report as in §2.
```

### EX-02 — cluster.model.manage (Phase 4, lane L10; tools 2, 3, 4, 5, 8)

```text
Add the cluster.model.manage family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex02-model-manage, flag DAVE_ENABLE_CLUSTER_WRITE_TOOLS (requires DAVE_ENABLE_CLUSTER_TOOLS), module davellm_model_manage.py. Models only from DAVE_MODEL_ALLOWLIST (JSON list of exact names); nodes by configured id.

Tools (all write tier, approval, preflight, bounded):
- model.unload: POST /api/generate with the model and keep_alive 0 on one node; verified by /api/ps.
- model.warm: the same with keep_alive from chat_keep_alive and an empty prompt; verified by /api/ps within 60 s, unknown after.
- model.delete: DELETE /api/delete on one node; the preflight refuses when the model is loaded, when it is the only copy across the configured nodes (checked with /api/tags on every node), or when the node is the one named in DAVE_MODEL_DELETE_PROTECT; verified by /api/tags.
- model.pull: POST /api/pull streamed as a cancellable background job (reuse the run events for progress; one pull per node at a time; 90-minute cap); the preflight refuses when node.disk (EX-16's host.disk when the node is the router host, otherwise "unknown, continue only if DAVE_MODEL_PULL_ALLOW_UNKNOWN_DISK=true") shows less than 1.5 times the model's known size free; the result is verified by /api/tags.
- node.disk: free space on the model store path for the router host (local) and for each node in DAVE_SSH_HOSTS (over SSH with the fixed-argument pattern from EX-16: `df` on Linux, `wmic logicaldisk` or PowerShell Get-PSDrive on Windows); nodes without an SSH entry report "not measurable". Listed here so the model can ask before a pull.
Results never include digests longer than 12 characters, addresses or error text.

Tests: fake Ollama for pull streams (progress, error mid-stream, cancellation), ps, tags, delete; only-copy and loaded refusals; allowlist refusals; disk refusals. Battle manifest entry with the disposable live target "a model under 1 GB on walter".

Live check (allowed): model.warm then model.unload of llama3:latest on walter from a throwaway router; no pull or delete.

Allowed gated actions: none. Report as in §2.
```

### EX-11 — git.write (Phase 4, lane L10; tools 47, 48, 49, 63, 100)

```text
Add the git.write family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex11-git-write, flag DAVE_ENABLE_GIT_WRITE_TOOLS (requires DAVE_ENABLE_EXTENDED_TOOLS), module davellm_git_write.py built on davellm_git.run_git (same from-scratch environment, no hooks, no credential helpers, no shell).

Tools:
- git.worktrees (read_files): worktrees of an admitted repository with branch, head and whether a fresh .agent-claim (under 2 hours old) exists, showing root-relative paths only.
- agent.claim.check (read_files): the .agent-claim content of one admitted checkout with its age, and a fixed verdict: free, claimed-fresh, claimed-stale.
- secret.scan (read_files): scans a diff or a file in a tool root with the harness secret patterns plus private-key headers and AWS, GitHub, Notion and Slack token shapes; returns matches by line with the secret itself masked.
- git.commit (write_files, approval, preflight): stages the named paths (never -A) and commits with the given message in an admitted repository; the preflight refuses when secret.scan finds anything in the staged diff, when another session's fresh .agent-claim exists, when the branch is main or master, or when the message is empty; the result is the new short SHA, verified with git log -1.
- git.push: do not implement the network operation. Instead write docs/decisions/0002-git-push-from-tools.md presenting the options (A: refuse pushes from tools permanently; B: push over SSH only, with an explicit DAVE_GIT_SSH_KEY and known_hosts file passed to run_git, to remotes in DAVE_GIT_PUSH_REMOTES, never force, never to main; C: a router-side "push requested" event that a human or the launcher completes) with your recommendation and the attack surface each one opens. Register no git.push tool.

Tests: hostile_git fixtures must still never run anything; commit refusal cases; claim age cases; secret masking; branch refusals. Battle manifest entry.

Allowed gated actions: none. Report as in §2 and with status needs-decision for the push options, recommendation first.
```

### EX-08 — docs.ask (Phase 5, lane L11; tools 23, 24, 29, 30, 96)

```text
Add the docs.ask family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex08-docs-ask, flag DAVE_ENABLE_DOCS_TOOLS, module davellm_docs.py. Index roots only from DAVE_DOCS_ROOTS (JSON, name to folder), admitted with the secret denylist; the index lives in dave_vectors.db beside the existing vectors with a separate table prefix; embeddings come from the embedding path app.py already uses for conversations (do not add a model).

Tools:
- vector.index (write_files to the database only; approval; background job, cancellable, 30-minute cap): walks one root with walk_tree, extracts text from .md, .txt, .csv, .json, .py, .js, .html and .pdf (pypdf, already a transitive dependency; if not, add it to requirements.txt and say so) and from images via `tesseract` when DAVE_TESSERACT_PATH is set (ocr.image is exposed as its own read tool with the same limit), chunks at about 800 tokens, skips files over 5 MiB, and records per-file hashes so re-indexing only touches changed files; progress through run events.
- vector.search (read): top-k chunks (k ≤ 10) for a query with root name, root-relative path, line range and a 400-character snippet.
- docs.ask (read): vector.search plus a fixed prompt assembling the top chunks as a citations block for the calling model; it does not run inference itself; it returns the chunks and the suggested citation format.
- pdf.read (read_files): numbered pages of one PDF in a tool root, 400 lines per page window, text only.
Indexing jobs run where the router runs; the prompt must measure one index of a 200-file fixture on the Mac and report the time, and recommend in the report whether a runner-hosted indexer is needed next.

Tests: fixture roots with mixed types, a planted secret file that must never be indexed, a symlink escape, an oversized file, hash-based skip on reindex, search result caps, fake tesseract. Battle manifest entry.

Allowed gated actions: none. Report as in §2.
```

### EX-17 — google (Phase 5, lane L11; tools 75–81)

```text
Add the google tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex17-google, flag DAVE_ENABLE_GOOGLE_TOOLS, module davellm_google.py. First deliverable is docs/decisions/0003-google-oauth-storage.md: how the router holds the OAuth refresh token (recommend the macOS launcher's Keychain pattern with a new item name, read once at startup into memory, never written to disk by the router), the minimal scopes (gmail.readonly, gmail.compose, calendar.readonly, drive.readonly, contacts.readonly), and the consent flow Dave runs once from the launcher. Then implement the tools against the REST APIs through davellm_public_http with the access token refreshed in memory.

Tools (public_network, bounded, 15 s):
- gmail.search (read): threads matching a query, newest first, capped at 20, with subject, sender name, date and a 200-character snippet; no bodies.
- gmail.thread.read (read): one thread's messages as plain text, capped at 48 KiB, attachments listed by name and size only.
- gmail.draft (write, approval, preflight): creates a draft reply or new draft; never sends; the preflight shows recipients, subject and body and refuses external recipients not in the thread unless DAVE_GMAIL_ALLOW_NEW_RECIPIENTS=true; verified by reading the draft back.
- calendar.list (read): events in a window (max 14 days) across calendars in DAVE_GCAL_CALENDARS; calendar.suggest_time (read): free slots of a given length in working hours from free/busy.
- drive.search and drive.read (read): files by name or full text within folders in DAVE_GDRIVE_FOLDERS, and the text export of one Google Doc, Sheet (CSV) or text file, capped at 48 KiB.
- contacts.lookup (read): name, email and phone for a name query, capped at 10.
Results never include raw message ids beyond what the run's ledger needs; the ledger maps run refs to ids as the Notion adapter does.

Tests: fake Google endpoints for each API including 401 refresh, 403 insufficient scope, 429, oversized bodies; draft creation and verification; recipient refusals. Battle manifest entry with inference false and no disposable live targets (live checks wait for Dave's consent run).

Allowed gated actions: none, and no live Google calls in this prompt. Report as in §2 with status needs-decision on the OAuth storage decision.
```

### EX-18 — av.show (Phase 5, lane L11; tools 83–88)

```text
Add the av.show tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex18-av-show, flag DAVE_ENABLE_AV_TOOLS, module davellm_av.py. Fixture profiles, MIDI maps and the gear inventory are files in DAVE_AV_ROOT (a tool root); network targets only from DAVE_OSC_TARGETS (JSON, name to host:port) and DAVE_RESOLUME_URL.

Tools:
- dmx.patch_check (read_files): validates a patch sheet (CSV or JSON: fixture, mode, universe, address) against fixture profiles in DAVE_AV_ROOT/profiles (JSON, mode to channel count), reporting footprint overflow past 512, overlaps, duplicate addresses and unknown modes, with proposed corrections; pure arithmetic, no network.
- artnet.discover (read_system, 5 s): sends one ArtPoll broadcast on the interface named in DAVE_ARTNET_INTERFACE and lists responding node names, short names, universes and firmware; never returns IP addresses, only the configured interface name and node short names.
- osc.send (write, approval, preflight): one OSC message (address pattern and up to 8 typed arguments) to one configured target; the preflight refuses address patterns not matching DAVE_OSC_ALLOWED_PATTERNS (JSON list of globs); the result is sent or failed (UDP, so never verified; say so in the result).
- resolume.status (read): composition name, layer count, active clips and the current BPM from the Resolume REST API at DAVE_RESOLUME_URL, 5 s limit.
- midi.map.lookup (read_files): the mapping rows for one control or one target from DAVE_AV_ROOT/midi-maps/*.csv.
- av.inventory.lookup (read_files): rows of DAVE_AV_ROOT/inventory.csv matching a name or category, capped at 50.

Tests: patch sheets with every failure class and correct corrections; fake UDP socket for ArtPoll replies and for osc.send; fake Resolume server; pattern allowlist refusals; inventory and map fixtures. Battle manifest entry; no disposable live targets.

Allowed gated actions: none, and no live UDP or Resolume calls in this prompt. Report as in §2.
```

### EX-19 — media (Phase 5, lane L11; tools 89, 90, 91, 92, 95)

```text
Add the media tool family to DaveHomeAssist/DaveLLM. Apply the executor contract in docs/DAVELLM_TOOL_EXECUTION_PLAN.md §2 with branch codex/tools-ex19-media, flag DAVE_ENABLE_MEDIA_TOOLS (image.describe additionally requires DAVE_ENABLE_INFERENCE_TOOLS), module davellm_media.py. Media files live in tool roots; binaries from DAVE_FFPROBE_PATH and the whisper runtime installed by scripts/macos/install-whisper-runtime.sh (read that script first and reuse its paths and model location).

Tools:
- video.probe (read_files, 20 s): ffprobe JSON for one file, reduced to container, duration, streams (codec, resolution, frame rate, channels, sample rate), creation time and timecode; file size; no absolute paths.
- video.gap_find (read_files): given a folder of takes, orders them by creation time or embedded timecode and lists gaps longer than N seconds between the end of one take and the start of the next, with the two file names and the gap length; pure metadata, no decoding.
- audio.transcribe (read_files; background job, cancellable, 30-minute cap; approval because it is heavy): transcribes one audio or video file with the whisper runtime, writing the transcript as .txt and .srt beside DAVE_TRANSCRIPT_DIR inside a tool root; runs on the router host unless DAVE_RUNNERS (EX-12) names a runner tagged whisper, in which case it uses that runner with the fixed rsync and ssh pattern; the result is the root-relative paths and the word count.
- setlist.parse (read_files): a setlist text or CSV into ordered songs with optional durations and notes, flagging duplicates and total running time.
- image.describe (inference, approval, 120 s): one image in a tool root sent to a vision model named in DAVE_VISION_MODEL on a configured node through the native /api/chat images field already used by plain chat, with num_predict 256 and the EX-05 inference ledger; returns the description and tokens.

Tests: fake ffprobe binary and outputs (normal, corrupt, missing), gap cases, fake whisper runner (success, timeout, cancellation), setlist edge cases, fake vision transport. Battle manifest entry with inference flagged for image.describe.

Live check (allowed): video.probe and video.gap_find on a folder Dave names in the lane environment as DAVE_MEDIA_SAMPLE, if set; one image.describe with gemma3:27b on duncan (cap 600 tokens) if DAVE_ENABLE_INFERENCE_TOOLS is set for the lane; no transcription.

Allowed gated actions: none. Report as in §2 and note under Open whether the whisper runtime is installed on the Mac.
```

## 5. Coverage matrix (100 tools to prompts)

| Tools | Prompt |
|---|---|
| 1, 9, 11, 12 | EX-01 |
| 2, 3, 4, 5, 8 | EX-02 |
| 6, 7, 73 | EX-03 |
| 13, 16 | EX-04 |
| 14, 15 | EX-05 |
| 17, 27, 28 | EX-06 |
| 21, 22, 25, 26, 31, 32 | EX-07 |
| 23, 24, 29, 30, 96 | EX-08 |
| 33–42 | EX-09 |
| 43, 44, 45, 46, 55, 56 | EX-10 |
| 47, 48, 49, 63, 100 | EX-11 (49 as a decision, not a tool) |
| 50, 51, 52, 53, 54, 64 | EX-12 |
| 57–62 | EX-13 |
| 82 | EX-14 |
| 65, 66, 67, 68 | EX-15 |
| 69, 70, 71, 72, 74 | EX-16 |
| 75–81 | EX-17 |
| 83–88 | EX-18 |
| 89, 90, 91, 92, 95 | EX-19 |
| 93, 94, 97, 98, 99 | EX-20 |
| 10, 18, 19, 20 | EX-21 |

Every number from 1 to 100 appears exactly once.

## 6. Orchestration notes

- Lanes L06 to L11 exist in `~/Code/agent-bus/lanes/`. L06 holds EX-00 as its first prompt. L07 to L11 are on hold until the previous phase's gate is met; their prompts are sent from this file in the order of §1.
- Codex executors in loop mode are started by Dave with the README kickoff; the orchestrator sends prompts and verifies reports. Claude one-shot executors need Dave's yes on a cost estimate first (AGENT rules). Codex usage is not counted against the Claude budget line in `BOARD.md`; record it separately.
- Merges: no prompt above allows merging. After a report, the orchestrator verifies CI and Codex review comments, then either merges with a merge commit itself (Dave's standing delivery rule) or sends a follow-up prompt that says "merge allowed".
- Live checks that need credentials (EX-10, EX-14, EX-15, EX-17) run only when Dave supplies the variable to that executor's shell; prompts say so and report "not run" otherwise.
- After each phase, refresh the DaveLLM entry on the Next Steps Board through the governed updater and record the phase in the communications log.
