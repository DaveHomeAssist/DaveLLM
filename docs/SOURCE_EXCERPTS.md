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

Next: complete exact-head required CI and source publication; retain installed activation and live model-quality acceptance as separate gaps. The missing private Next Steps Board cannot be synchronized; the project-owned projection is updated without creating a replacement.
