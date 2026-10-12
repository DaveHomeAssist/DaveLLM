# Source excerpt line identity

Scope: additive source presentation and offline regression fixtures for file.read_lines and md.section. Baseline: b34e69b7c132ce9d3b49d79d66ee852a86c666ba.

## Problem and smallest correction

The saved T10/T11 pilot retrieved source successfully but produced incorrect citations, a changed paraphrase and two bullets where three were requested. T10's one-search/one-read constraint passed. These are retained model-quality failures, not proof of a broken file reader or an exhausted budget.

Add `numbered_lines`, an ordered array of `{line, text}` records, to successful source excerpt results. `line` is the absolute one-based file line, including blank lines. `text` is identical to the corresponding existing `lines` string. Keep existing fields, path admission, permissions, pagination and error behavior. Charge both representations against the existing result-byte limit, so large pages can contain fewer lines; their continuation must still make progress. Existing long-line clipping remains marked by `cut_lines` and `truncated`; numbering must not imply a complete line when it is clipped. Empty excerpts return an empty array. Ambiguous headings still return candidates, not invented source lines.

## Acceptance

- Real files with nonzero offsets, blank lines, Unicode, fences, repeated text, CRLF and an unterminated final line retain exact returned text and correct absolute line identities.
- Markdown section and file paging agree; Setext heading offsets, ambiguity, empty bodies and byte-budget continuation remain correct.
- Focused offline fixtures detect relative rather than absolute citations, out-of-range reads, and wrong requested bullet count. Accept a supported three-bullet control, reject historical-pattern controls. These assertions grade fixture structure; they do not silently rewrite an answer, add model calls or claim semantic truth.
- Existing file/Markdown path-security, output-budget, provenance and generated-manifest checks remain passing. No new dependencies.

## Boundaries and remaining proof

Numbered lines remove avoidable arithmetic, not interpretation risk. Neither numbering nor a range/count assertion guarantees that a claim is supported by its citation or that a live model will use the format. No generic runtime answer validator, automatic retry, system-prompt change, answer repair, UI change or increased budget is introduced. Offline assertions stay in tests, not a second runtime policy layer.

No inference, live pilot retry, H9, Notion write, model/service/settings change or installed activation belongs to this change. Preserve the original 12 Pass / 5 Fail batch and subsequent two-pass/two-fail pilot. Source delivery and offline acceptance do not change either score. A later live-quality check requires its own bounded authorization.

## Offline verification

On Walter, in an isolated Windows Python 3.12 environment, the nine new fixtures fail on baseline b34e69b because `numbered_lines` is absent. With the correction, the focused source/file/Markdown/provenance run has 151 passes, nine platform skips and five failures in 61.51 seconds. All five failures reproduce on the unchanged baseline: the CRLF byte-boundary fixture, unsupported `utime(..., follow_symlinks=False)`, directory error classification and two FIFO fixtures. No assertions were weakened; Linux CI remains the required regression gate. The macOS-generated capabilities manifest is unchanged and its small local `--check` passes.

## Delivered source and remaining acceptance

Reconciled October 12, 2026 from existing receipts; no tests or model requests were repeated to establish these historical results. [PR86](https://github.com/DaveHomeAssist/DaveLLM/pull/86) delivered candidate `96416b11e73b93940f46e57f78ad492777fdb167` as merge `3e0b76b11972b223b7f27484d60d05e12e12f2d8`, with identical source trees. [Candidate CI37903530377](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/37903530377) and [merge CI37904366101](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/37904366101) passed Python 3.12/3.13/3.14: 1394 regressions, 207 mocked Notion tests and 21 JavaScript tests per version, plus the remaining workflow gates. [Pages37904364952](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/37904364952) published the merge; the delivery receipt records matching served document hashes. These source gates are complete, not pending work.

The later `cfc4bf2` main retains this correction. Its [CI37994281894](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/37994281894) passes all three Python versions and [Pages37994281449](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/37994281449) succeeds. Neither deployment updates the installed desktop backend.

The October 9 activation receipt predates this correction and identifies `b34e69b`; the canonical local checkout was still clean at that revision on October 12. Current running-process revision and native acceptance were not inspected. T10 and T11 remain **FAIL**, while T17/T18 passed in the four-case pilot: 10 requests and 4070 generated tokens consumed, all attempts spent, no retry grant. Do not change the original 12 Pass / 5 Fail batch or infer improved model quality from numbered lines. Provider/physical/operator acceptance and H9 remain separate; Duncan's one-request CPU failure still requires stopping tuning.

Next: retain installed activation and live model-quality acceptance as separate scoped gates; do not repeat source delivery or the consumed pilot. The private Next Steps Board remains unavailable; the [project-owned projection](NEXT_STEPS.json) is reconciled without claiming board synchronization or creating a replacement.
