# Tools100 implementation brief

## Authority and outcome

The operator explicitly approved all 100 October 5 expansion candidates, superseding the earlier Phase 1A-only implementation scope. This is a serial, tools-only implementation. The 21 families organize work; they are not broad action-switch tools. Every candidate retains its own schema and permission/approval boundary.

Baseline: main `179a79c0459a77976249bbf1f7a3672496726dcc`. Existing qualified, extended and Notion tools remain unchanged. The frozen navigation branch is not a dependency. Installed desktop rollout is now in scope under Dave's October 7 directive to deliver working software.

## Flow and boundaries

Enable `DAVE_ENABLE_TOOLPACK` together with `DAVE_ENABLE_TOOLS` to register the expansion. Pure utilities and owner-scoped reads reuse existing services. Node operations use the existing configured node inventory. External integrations use operator-held credentials and named allowlists, never caller URLs, credentials, shell commands or host paths. Jobs use separately enabled, operator-selected runner recipes. Every write, inference, process execution and network send pauses for exact-call approval.

All requests have finite input, output and wall-clock budgets. Refusals use fixed messages; output redacts credential/address fields. Failed probes report Unknown, not asleep. Context distinguishes observed loaded from requested configuration. Notion writes compare a run snapshot immediately before dispatch and verify by readback; uncertain effects are never automatically retried. Git writes cannot force or rewrite history. Regex and document parsing run behind bounded process isolation.

## Delivery slices and acceptance

1. Catalog: exactly 100 distinct candidate names, closed bounded schemas, permission metadata, default-off flag, unchanged qualified provenance.
2. Local/data/native implementations: deterministic fixtures, ownership failures, path containment and bounded hostile inputs.
3. Cluster, service, Google, GitHub, HA and Notion adapters: mocked transports, allowlist refusals before I/O, readback and ambiguous-effect cases.
4. Runner/media/AV capabilities: fixed recipes and target allowlists, timeout/cancellation tests; real hardware remains a separate acceptance lane.
5. Generated capabilities, tool catalog and next-steps projection; protected CI, merge and provider readback.
6. Composer selection: remove the separate Run tools action; expose a searchable multi-select Tools menu, `/` and `@` filtering, removable selection chips, and one Send action. The router validates selected names, sends only selected schemas to the model, and rejects calls outside the selection.

Live inference and H9 are allowed when needed for real product acceptance. Destructive node operations, notifications, external writes, OAuth provisioning, credential persistence and service migration still require their exact existing approval and configuration boundaries. Missing integration configuration must fail closed and be reported, never be represented as live acceptance. No subagents or new sessions are authorized.

## Risks and dependencies

Provider access, actual runner recipes, entity/destination inventories, OAuth grants and hardware capability are deployment inputs. Implementing a connector does not establish those inputs. SDK/dependency choices must be limited to concrete functionality. Existing harness contracts and definition fingerprints are preserved; no harness contract version change is needed.
