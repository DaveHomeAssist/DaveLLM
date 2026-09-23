# DaveHarness 1.0.0-rc.1 offline qualification

H8 implements actions 49–54 after H7 merge `16620214335c735fe947a9347d15620d2a2272a7` (PR #12). DaveLLM remains `2.1.0`. This is an internal source candidate: live-model qualification, human acceptance, and publishing remain separate gates.

## Required evidence

| Action | Gate |
|---|---|
| 49 | Pinned development-only Hypothesis `6.168.1`; 100 reproducible generated examples per property for serialization, byte measurement, parser equivalence, schema constraints, and terminal transition legality |
| 50 | Hostile depth, size, nodes, cycles, Unicode, non-finite numbers, duplicate IDs/keys, malformed native/fenced calls, unsupported schema constraints |
| 51 | Seeded duplicate decisions and cancellation/completion schedules; revocation, CAS conflict, late synchronous model/tool results, failing sinks; one terminal event and no duplicated/unauthorized effect |
| 52 | Admission before recursive copy/JSON encoding; bounded output and transcript growth; event retention and delivery byte ceilings; measured workload plus twice headroom |
| 53 | Development-only coverage `7.16.1`; required Python 3.12, 3.13, 3.14 CI lanes with package-only coverage at least 85%, strict package typing, API signature/field snapshots, dependency-boundary scans and redaction canaries |
| 54 | 24 offline evaluations: 12 cases through an injected fake adapter and replayed JSON recordings; machine-readable report requires zero unauthorized effects and exactly one terminal event |

Dependency versions were checked against the official [Hypothesis package metadata](https://pypi.org/pypi/hypothesis/json) and [coverage package metadata](https://pypi.org/pypi/coverage/json) on 2026-09-23. Both support this Python matrix. They add no runtime dependency. Coverage measures only `daveharness/`; the full repository suite still runs. The 85% minimum establishes an enforceable baseline below the measured 88%; it does not replace behavioral assertions.

The recordings in `tests/fixtures/daveharness/corpus.json` are authored synthetic response fixtures, not captured live-model performance. Cases cover final answers, native and fallback calls, malformed-call repair, approve/reject, permission denial, repeated IDs, tool/step ceilings, model timeout and cancellation. Effects are isolated in-memory counters. No network model adapter or file/shell tool is available to this runner.

## Limits and compatibility

New lifecycle budgets remain eight model steps, two errors, 32 tool calls, 300 seconds total, 64 KiB per result, 2 MiB transcript, and 1,000 retained events. The event byte ceiling remains 1 MiB and now also bounds queued sink delivery. One event larger than that ceiling fails before retention; slow sinks can lose delivery while the bounded replay ledger stays available. Byte accounting is cached only for retained events and removed on eviction. Truncation preserves the terminal record.

Generic finite JSON admission defaults to 16 MiB, depth 32 and 100,000 nodes (including object keys). Strings are measured as UTF-8 without first allocating an encoded copy. Encoded JSON is scanned for byte/depth/node limits before decoding; duplicate keys and non-finite constants fail. Structured values are measured before recursive copying or serialization. Oversized native batches fail together, without returning a partial executable batch. Unsupported schema constraints fail at registration even when the corresponding argument property is absent.

`RunBudget` configures result/transcript/step/error/tool/time limits; `EventJournal` configures event count/bytes; `daveharness.limits.PayloadLimits` configures admission, and `parse_tool_calls(..., limits=...)` accepts stricter per-parser limits. Core contracts retain the structural safety ceiling. These guards bound data admitted to the harness, not memory allocated inside arbitrary host adapters, handlers, or custom object conversion methods; hosts remain responsible for transport and handler limits.

The shared parser/contract guards also reject pathological legacy payloads beyond these documented structural limits. Existing legacy step/error defaults, normal outputs, import paths, status strings, endpoint fields, authentication, root containment, tools-off and separate shell opt-in remain intact. In particular, whitespace-only fallback names retain the shipped revoked-tool behavior. Unknown schema keywords are now explicitly unsupported instead of silently ignored. No production model inventory or persistence schema changed.

## Reproduction

```sh
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m mypy daveharness
python -m coverage run -m pytest -q
python -m coverage report
python scripts/qualify_daveharness.py --output qualification-report.json
python scripts/measure_daveharness.py > load-report.json
```

CI uploads coverage, qualification, and load reports for each Python lane. The public API fixture snapshots export names, parameter names/kinds/requiredness, dataclass fields and status/permission sets. Existing compatibility tests also verify the legacy re-export identity. Event/log canary tests cover prompts, arguments, results, paths and hostile identifiers; payload content is absent from reports.

The load gate admits 1,000,000 and 2,000,000 character requests under the unchanged 2 MiB transcript budget, and records 1,000 and 2,000 events under the unchanged retention ceiling. Instrumented request admission must stay below 32 MiB peak allocation and 30 seconds; event recording below 8 MiB and 30 seconds. These portable CI bounds are regression ceilings, not interactive latency promises. The report records actual elapsed time and peak allocation. Separate hostile-load tests require oversized input rejection without a proportional allocation.

H8 changes headless admission and qualification infrastructure; it adds no desktop controls. H7's actual desktop acceptance remains recorded separately. API tests exercise default-off/authentication, exact approval/resume, cancellation, immutable BRAIN context, and ledger behavior with local fakes.

## Delivery and H9 boundary

Merge is gated on all three exact-commit CI lanes. The coordinator records commit/merge SHAs, run IDs, Pages deployment/readback, and final local evidence in the phase handoff. Candidate gate status remains explicit in the implementation plan until delivery finishes.

Before action 55, Dave must authorize target model IDs, disposable roots, evaluation scope and resource budget. The plan requires at least 500 representative evaluations per target model and all stated safety/reliability thresholds. H8 synthetic cases do not count toward that quota. Do not set `1.0.0` or claim DaveLLM `2.2.0` complete from this candidate; no tag, release, published package, separate repository, service, or production mutation is authorized.
