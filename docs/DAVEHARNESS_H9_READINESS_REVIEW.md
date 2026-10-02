# H9 readiness review

Evidence date: 2026-10-01. Source baseline: `19eb774e75cb9e6f1de33c450342084004c8b07e` on local and remote main. This is an evidence reconciliation, not qualification or human acceptance. DaveHarness remains `1.0.0-rc.1`; DaveLLM remains `2.1.0`. Same-day follow-up corrections (context-size and access evidence, CI enforcement) start from `8822ce0d383d3dff2c00f7fb6fd533ab8f40a195`.

## Delivery and authorization

H2–H8 are complete; do not repeat them. [PR #14](https://github.com/DaveHomeAssist/DaveLLM/pull/14) merged September 25 at `ebcd823eb7f0b43fb164c629ad7e46b3031ef9f4`. It delivered H9 engineering and failed-attempt evidence, not a passing live-model corpus. No open PR was returned during the October 1 reconciliation.

The September 23 Walter authorization expired at 18:53:04 UTC that day. Preserve its **17,139 reserved tokens across three dispatches**; these are conservative charges, not measured generated tokens. A missing temporary ledger is not renewed permission. No new inference, mutation corpus, release, tag or package is authorized by this review.

Baseline CI [36837819168](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/36837819168) passed Python 3.12, 3.13 and 3.14, each with 758 tests passed and 89% package coverage. Required typing, offline qualification/load, syntax, Node and dependency-audit gates passed. Pages run `36837818542` and the Pages build API identified the same baseline commit; the published implementation plan matched source bytes before this reconciliation. These are baseline engineering proofs, not evidence for the documentation candidate's later delivery or model qualification.

## Two evaluation paths

| Property | `scripts/qualify_live_daveharness.py` | `scripts/evaluate_live_daveharness.py` |
|---|---|---|
| Boundary | Generic DaveHarness, independent HTTP client and case-confined handlers | DaveLLM's `invoke_harness_model`, registered handlers and host context, with an evaluation-owned Harness |
| Transport at baseline | OpenAI-compatible `/v1/chat/completions` | Native Ollama `/api/chat` through DaveLLM |
| Context size | None sent; the node's own Ollama setting decides | `num_ctx` from `chat_num_ctx`: model window capped at `DAVE_CHAT_NUM_CTX` (default 16,384, floor 8,192) |
| Target | Fixed Walter `qwen3-coder:30b`, pinned digest | Explicit `--target NODE_ID:MODEL_ID` from configured inventory |
| Corpus | 500 fixed cases: 20 families × five variants × five repeats | Eleven baseline cases repeated; selectable cases and optional extended cases |
| Qualification | Four plan thresholds, full corpus and fixed task/effect assertions | Quota, four thresholds, no runner errors and valid lifecycle accounting; task success reported separately |
| Budget | Six-hour / one-million-token cumulative ledger, pre-dispatch reservations | Per-run limits; no aggregate wall/token ledger |
| Authorization control | `--authorized`; absent ledger creates a fresh accounting window | `--authorize-live`, explicit targets; configurable `--quota` |
| Exit status | Zero only when report is qualified | Zero when models exist and every model has zero unauthorized effects, even if qualification fails |

Source anchors: `Resources`, `main` and summary in the fixed runner; `summarize`, `run_evaluation` and `main` in the adapter runner. The CLI flags express operator intent; neither is a substitute for a current human authorization record. Do not interpret adapter exit zero as action 56 acceptance or a reduced development quota as the required 500 representative evaluations.

The governing runner, corpus, tool catalog and qualification definition remain a decision. This package changes neither runner nor thresholds. Retain the plan's 100% unauthorized-effect prevention, at least 99.5% schema validity after one repair, less than 0.5% step-limit termination and less than 5% malformed/repeated calls after repair. Sandbox CPU-model results in [live evaluation](DAVEHARNESS_H9_LIVE_EVALUATION.md) count toward no target quota.

## Integration and UX mapping

This maps verified merged work to the original H7/H9 requirements and available UX artifacts. The original request mentions a separately listed UX follow-through, but that exact complete checklist is not present in the implementation plan or supplied request. The design manifest is useful later evidence, not proof that it is the original list. Exhaustive UX closeout remains unknown until the list is recovered and mapped.

| Requirement / area | Merged source evidence | Acceptance still required |
|---|---|---|
| H7 lifecycle, immutable BRAIN context, exact approval and ledger | H7 guide and historical action 47 record; `project_context.py`, lifecycle routes and run-ledger tests | Preserve frozen revisions, root/auth boundaries and status compatibility on the final candidate; historical H7 acceptance is not current-console acceptance |
| Model transport and context alignment | PR #32 plain-chat native transport; #37 context/keep-alive; #38 native tool loop | Actual authorized inference on the chosen target; generic `/v1` results cannot stand in for native-adapter evidence |
| Deadlines and failure behavior | PR #39/#41 deadline work, #44 blocked-read total-deadline enforcement | Candidate cancellation, timeout and process-restart failure proofs under action 58 |
| Tool capabilities and exact mutation scope | `docs/DAVELLM_TOOLS.md`, generated capabilities manifest, extended file/Markdown/Git/native/edit/web handlers; PR #31 edit and #34 web work | Explicit evaluated catalog and model restrictions; sandbox development evidence is not qualification of all enabled tools |
| Waiting, stats and node guidance | PR #40 DL-UX-01; #42/#43 node profiles, residency and warm-follow-up handling | Current desktop behavior and clear distinction between transport timings, model usage and estimates |
| Console, composer, navigation and approval surface | PR #45; `docs/design/CONSOLE_IMPLEMENTATION.md`; `tests/browser_console.cjs` | Full action 58 native approval/resume/cancel/accessibility evidence tied to final candidate; physical-phone keyboard behavior remains unproven |
| Original post-H9 UX follow-through | Later design manifest and console implementation overlap substantially | Recover exact original list, map each item and preserve action 60 human-acceptance dependency before declaring follow-through complete |

PR #45 records fixture-backed browser checks at seven widths and native startup/navigation/credential isolation. It explicitly does not establish live inference or physical-phone keyboard behavior. Its local 757-passed/one-skipped record and baseline CI's 758-passed result are distinct environments, not contradictory qualification counts. The October 1 workspace WEB-2 viewport checks at 1440×900 and 375×812 belong in the later UI gate; this Markdown-only package does not change application layout or claim those checks passed.

## Read-only access observations — 2026-10-01

- Supported Computer Use app inventory succeeded. Finder's native accessibility tree was readable. No permissions, helper state or shared databases were changed. The initial request to inspect Codex was denied by the tool's app safety restriction; it was not bypassed. Finder was used only to test general native accessibility availability.
- DaveLLM Launcher was listed as not running; no DaveLLM window was inspected or launched. Native input actions and application-specific desktop acceptance remain unverified. General native read access does not prove recovery persists across restart.
- Walter resolved from existing Tailscale inventory and reported online. Bounded GET `/api/tags` succeeded and included `qwen3-coder:30b` at the previously pinned digest `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`.
- GET `/api/ps` returned an empty model list. A five-second TCP connection probe to SSH port 22 timed out. No authentication or service-log access was established. No completion request was sent.
- Tailscale emitted a client/server version mismatch warning. This is an observation, not an established cause of SSH or inference failure.
- Addresses and credentials are omitted. Current inference health remains unknown. The September 23 cause is not established; see [context-size and access evidence](#context-size-and-access-evidence) for later evidence that bears on it.

## Context-size and access evidence

Added 2026-10-01 after the reconciliation above. Each item carries its own date and source.

- **Remote-shell ports unreachable from the tailnet (Confirmed 2026-10-01).** Tailscale lists Walter as a Windows host, so Tailscale's integrated SSH server is not available. Five-second TCP probes from the router host to SSH (22), RDP (3389) and WinRM (5985, 5986) all timed out; only the Ollama API port answered. A timeout shows only that the probe got no answer: a firewall rule, interface binding or stopped service would look the same, and Walter's service configuration was not inspected. The September 23 and October 1 SSH timeouts are therefore one standing condition, not a new fault. Until it changes, SSH cannot supply the diagnostic access the [results](DAVEHARNESS_H9_RESULTS.md) resume condition asks for; node-side diagnosis goes through the read-only Ollama API or through Dave at Walter (Ollama app settings and server log).
- **Context-size out-of-memory on the pinned target (2026-09-25).** The local cluster benchmark (`perf-runs/2026-09-25/summary.md` in DaveLLM's application-support folder, outside this repository) loaded `qwen3-coder:30b` on Walter at Ollama's default context of 131,072 tokens and got HTTP 500, `llama-server reported out-of-memory during startup`. With `num_ctx` 8,192 the same model loaded (18.26 GiB resident, 32.4% on GPU) and generated at 21.56 tokens per second. Walter has 32 GB of RAM and an 8 GB laptop GPU.
- **Server setting lowered (2026-09-26, not re-verified).** On Windows the Ollama app's Context length setting overrides `OLLAMA_CONTEXT_LENGTH`. Walter's was at 128k; Dave lowered it to 16k on 2026-09-26, and an operator check through `/v1` then loaded `qwen3-coder:30b` and generated normally. This comes from that day's operator session record. It was not re-checked on 2026-10-01: no inference was authorized, and `/api/ps` cannot show the setting while no model is loaded.
- **Bearing on September 23 (Likely contributor, not established).** The September 23 attempts used the same node, model and `/v1` transport with no context option, before the setting changed, so a context-size out-of-memory is a likely contributor to the first `model_error`. It does not by itself explain the later 120-second `model_timeout`, and the September 23 record holds no node-side error text. The [results](DAVEHARNESS_H9_RESULTS.md) record's "not established" finding stands.
- **Runner consequence.** The fixed runner sends no context option, so its context size is whatever Walter's server setting is at dispatch time. The adapter runner sets `num_ctx` itself (at most 16,384 by default). The two runners can therefore run the same target at different context sizes, which the governing-runner decision should account for.

## CI enforcement

Until 2026-10-01, `main` had no branch protection or rulesets. CI ran on every push and pull request but did not gate merges, so earlier "required check" statements, including this review's baseline paragraph, describe practice rather than enforcement; the cited checks did pass. On 2026-10-01 branch protection on `main` began requiring `checks (3.12)`, `checks (3.13)` and `checks (3.14)` from GitHub Actions before a pull request merges. Branches need not be up to date, and administrators are not enforced, so an administrator can still push directly or bypass the gate.

## Remaining gates and handoff

Completed by this package: current-state documentation and bounded access observations. H9 actions 55–60 remain incomplete. SSH TCP timeout: the same unreachable condition seen on September 23, not a new failure (see [context-size and access evidence](#context-size-and-access-evidence)). Skipped by scope: inference, model loading, service changes, runtime mutations and full desktop acceptance.

Next decision: define which qualification path/corpus/catalog governs action 56 and explicitly renew target, disposable roots, scope and budget before any live run. Resource enforcement and exit semantics must be reviewed against that decision before dispatch. Before any fixed-runner dispatch, record Walter's context setting in the run report (confirmed with Dave, since it cannot be read remotely without loading a model), or choose the adapter runner, which sets the context itself. Then collect the required per-model evidence and restrictions, complete actions 57–59 on the exact candidate and obtain action 60 human acceptance. Public release/tag/package/service/repository remains separately authorized. Preserve the historical attempt and do not reset its ledger.
