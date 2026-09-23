# DaveHarness 0.4.0 policy and budget contract

H2 adds host-injected `ToolPolicy`, immutable `RunPolicyContext`, structured `PolicyDecision`, and immutable `RunBudget`. DaveHarness still selects no roots, credentials, model, transport, or persistence. DaveLLM's tools and shell remain default-off behind separate flags. The legacy loop and HTTP routes keep their shipped step/error defaults and serialized fields.

## Permission decision matrix

| Registered permission | Host permits it | Host omits it | Unknown string |
|---|---|---|---|
| `read`, `read_system`, `read_files` | `allow` or `pause` when approval is required | `deny: permission_denied` | `deny: permission_unknown` |
| `write`, `write_files` | `allow` only after approval when required; `pause: approval_required` otherwise | `deny: permission_denied` | `deny: permission_unknown` |
| `public_network`, `execute_process` | `allow` or `pause` when approval is required | `deny: permission_denied` | `deny: permission_unknown` |

All classes use the same fail-closed path. An absent name returns `tool_unknown`, a previously revoked name returns `tool_revoked`, a changed definition fingerprint returns `definition_changed`, a changed expected permission returns `permission_changed`, and a revoke followed by an equivalent re-registration returns `registration_changed`. Approved exact calls return `approval_granted`; ordinary allowed calls return `policy_allowed`. Policy decisions contain action, reason code, fingerprint, permission, and registration revision without argument values.

The fingerprint hashes canonical JSON of the declaration: name, description, nested schema, permission, approval flag, timeout, cancellation mode, async mode, actual registered handler code, and serializable handler configuration captured at registration. Opaque handler state requires an explicit `handler_version`. The registry stores that digest and a registration revision, so revocation followed by an equivalent registration invalidates a pending decision. The final registry check and effect acceptance are one locked operation; revocation after acceptance applies to new calls and does not cancel work already accepted. H3 adds serializable approval state and an executed-call ledger.

## New lifecycle budget matrix

| Limit | Default | Boundary response |
|---|---:|---|
| Model steps | 8 | Existing `step_limit` |
| Errors | 2 | Existing `error_budget` |
| Tool calls | 32 | `budget_exceeded` / `tool_call_limit` |
| Total wall time | 300 seconds | `budget_exceeded` / `run_wall_limit` |
| One tool output | 65,536 UTF-8 bytes | Tool `output_limit` / `tool_output_limit` |
| Transcript | 2,097,152 UTF-8 bytes | `budget_exceeded` / `transcript_limit` |
| Retained events | 1,000 | `RunBudget.exceeded("event_count", n)`; event retention is H5 work |

An explicit `RunBudget` opts the current in-process loop into these ceilings. The legacy adapter builds `RunBudget.legacy`, with only its supplied step and error limits; it does not silently tighten existing `/tools/agent/run` calls. `ExecutorOutcome.reason_code` exposes a bounded stop reason in Python without adding a legacy response field. The H3/H4/H5/H6 lifecycle will consume this contract for state, absolute deadlines, and events. H8 will enforce limits before copying or serializing large untrusted input and measure headroom.
