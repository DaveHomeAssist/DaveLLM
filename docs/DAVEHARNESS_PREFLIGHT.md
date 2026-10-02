# DaveHarness approval preflight

An approval-required tool may declare an optional preflight. It lets the tool
say, before the run pauses for approval, that this exact call is certain to
fail. The user is then not asked to approve a call that would only be refused
after approval. A preflight can spare an approval. It can never authorize an
effect.

## Contract

```python
ToolDefinition(..., approval_required=True, preflight=check, preflight_version="")

def check(arguments: dict, context: ExecutionContext) -> str | None: ...
```

- **When it runs.** Only on the approval path: after policy decides `pause`
  and after the arguments pass schema validation and canonicalization, before
  any pending call is created. It does not run for tools that need no approval,
  for calls the host pre-approved (`approved_tools` on the legacy loop), on
  `/tools/execute`, or again after approval.
- **What it gets.** A deep copy of the canonical arguments, so changing them
  changes nothing, and an `ExecutionContext` with the run and call IDs, the run
  deadline, a tool deadline bounded by the tool's `timeout_seconds` and the run
  deadline, the tool output budget, and the run's cancellation token. It runs
  in a worker thread with the caller's context variables, like a synchronous
  handler.
- **What it returns.** A nonempty string refuses the call. `None` lets the call
  pause as before. Any other value, an exception, a refusal larger than the
  tool output budget, or missing the tool deadline also lets the call pause; a
  late preflight is abandoned, like a `deadline_abandoned` handler. Failing
  open is safe because the handler still checks everything after approval.
- **What a refusal looks like.** An ordinary tool error whose handler never
  ran: status `error`, termination `denied`, an empty result, and the refusal
  as the error. `denied` is the termination DaveHarness already uses for calls
  refused before their handler runs (schema validation, policy); `error` would
  claim the handler ran and failed. No pending call or nonce is created. The
  call counts as one tool call and one error, its call ID enters the executed
  ledger, and the remaining calls from the same model step continue. The
  lifecycle records `policy_decision` (`pause`, `approval_required`) and
  `tool_result` (`error`, `preflight_refused`), and no `approval_required`
  event.
- **No effects.** A preflight runs before the user has approved anything, so it
  must only read. DaveHarness cannot enforce that; hosts must.
- **Registration.** The registry rejects a preflight on a tool that does not
  require approval (it could never run), a coroutine function, a callable that
  cannot take `(arguments, context)`, and a `preflight_version` without a
  preflight.

## Fingerprint

A preflight is part of the call's trust boundary, so its provenance is part of
the definition fingerprint exactly like the handler's: module, qualified name,
compiled-code digest, and serializable configuration (defaults, closure,
partial arguments), with `preflight_version` required for opaque state. The
`preflight` key is added only when a preflight is set, so every definition
without one keeps its existing fingerprint. DaveLLM's
`tests/fixtures/davellm/tool_catalog.json` pins each preflight's placement and
source in `extended_preflight_code`.

## DaveLLM

`file.edit` uses `preflight_file_edit` in `app.py`, which calls
`davellm_edit.check_edit`: the handler's own checks up to the write (path
admission, file type, hard links, UTF-8, `old_text` count, size), returning the
handler's own message. The approved edit still runs every check again, because
the file can change after approval.

The Notion tools on PR #48 can use the same hook once that branch merges: the
run ledger is per run and reachable from the preflight through
`HOST_RUN_CONTEXT`, which the worker thread inherits.
