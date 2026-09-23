# H9 qualification blocked: model transport

Recorded 2026-09-23. **Not qualified. H9 actions 55–60 remain incomplete.** DaveHarness remains `1.0.0-rc.1`; DaveLLM remains `2.1.0`. No release, tag, published package, human acceptance, or UX follow-through is claimed.

## Authorized target and observed result

- Model: `qwen3-coder:30b`, node label Walter.
- Digest: `06c1097efce0431c2045fe7b2e5108366e43bee1b4603a7aded8f21689e90bca`.
- Scope: 500 fixed evaluations, disposable roots, exact approval for expected writes, shell disabled, six hours or one million tokens, no paid APIs.
- Inventory endpoint succeeded. The loaded-model endpoint subsequently returned an empty list.
- First attempt ended `model_error` after 49.614 seconds with no captured response. Its detailed transport error was not preserved by the initial runner; the underlying cause is unknown.
- The next request was interrupted to prevent repeated blind dispatch. A diagnostic retry with explicit transport capture timed out after 120.011 seconds, ending `model_timeout` with `transport_TimeoutError`.
- Read-only SSH probes through both the configured host name and current inventory address timed out on port 22. Service logs and server resource state could not be inspected. A memory shortage or model-load crash is **not established** by this evidence.

There were three dispatched requests, zero usable model responses, zero tool proposals, zero tool effects and zero writes. These are failed/aborted transport attempts, not 500 representative completed evaluations. The model's schema validity, reliability and safety qualification rates are unknown. Zero observed unauthorized effects is not sufficient safety qualification without the required corpus.

## Resource and provenance accounting

The authorization ledger conservatively charges 17,139 tokens: a full 5,713-token reservation for each failed/interrupted dispatch. No usage response was available to refund those reservations. This is a charged upper bound, not a claim that 17,139 tokens were generated. The original wall budget began at 12:53:04 UTC and ends at 18:53:04 UTC on 2026-09-23; restarting a runner does not reset it. No inference process from this batch remains running locally.

| Evidence | SHA-256 |
|---|---|
| Original run `20260923T125304Z/report.json` | `d2974987db35c812f882f66d2058babd5a8c376ece7009d2fdbd9daa3219e1a1` |
| Diagnostic run `20260923T125533Z/report.json` | `3cb9e1b4a8bedf8e6da3811813e35924c9888c3d6f85d119b7996ad016f7d8fc` |

Raw synthetic response/event evidence remains under the authorized local disposable root. Public artifacts omit node addresses and credentials. The initial runner was committed as `307cd90eb39214ec497182fb99d3ed30b0cb0748`; the fail-fast transport diagnostic and durable reservation fix is `a3163190512dd27462d03805aefff31aaeb368b1`. Report digests provide integrity, not human sign-off.

The diagnostic fix retains unknown-usage reservations, persists before dispatch, stops the batch on HTTP/timeout failures and records sanitized errors. Tests cover that behavior with fake transport. The original incomplete report is preserved; the authorization ledger accounts for its interrupted request separately.

## Restriction and next gate

`qwen3-coder:30b` on this deployment is excluded from a qualified support claim until transport works and the full authorized corpus passes. No safety threshold or tool policy was relaxed. Other installed models were not evaluated because the authorization names this model only.

Resume requires reachable inference on Walter and access sufficient to diagnose its failure, or explicit approval of a different target model. The current six-hour/token allowance must remain cumulative; renewal after expiry requires a new budget. Keep PR #14 draft while the live gate is blocked. Convergence documentation, version changes, native candidate acceptance and final human approval follow a passing qualification gate.
