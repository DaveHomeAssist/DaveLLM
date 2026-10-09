# Tool run limits

## Scope and acceptance

The operator needs to enforce a small live-tool experiment's limits without changing normal chat defaults. The smallest slice adds two optional limits to the existing authenticated `POST /tools/agent/runs` request: `max_input_tokens` and `total_wall_seconds`. No new route, service, model configuration or permission is introduced. Existing callers retain the five-minute run budget and no additional input ceiling.

Acceptance: invalid limits fail before a run; the input estimate includes selected schemas and the complete accumulated transcript before every provider call; an over-limit call never reaches the provider; a shorter run deadline cancels the owned async model request and prevents subsequent steps. Responses expose the requested limits and bounded, content-free provider usage records. Fake-model tests cover initial admission, growth after a tool result, expiry, cancellation, output/step limits and default compatibility.

## Contract

`X-API-Key` authentication and existing ownership rules are unchanged. The optional fields are:

- `max_input_tokens`: integer from 1 to 262144, or null/omitted for existing behavior. This is an estimate using the existing host token estimator, not a provider tokenizer or a guaranteed billed-token ceiling. Tool schemas and non-content message fields count at the existing JSON estimate rate.
- `total_wall_seconds`: finite number from 0.1 to 300; omitted means 300. It bounds the whole run, including model and tool steps, and can only shorten the existing default.

The approved pilot sends `max_input_tokens: 4000`, `total_wall_seconds: 180`, `max_tokens: 1024`, `step_limit: 4`, and `error_budget: 2`, with the exact selected tools and a fresh message array. The collector must also enforce its batch limit and never retry uncertain POST outcomes.

Creation returns HTTP 413 for an initial input-limit violation and HTTP 422 for invalid fields. A later input-limit violation ends the run as the existing `model_error` status with a labelled harness `terminal_explanation.reason_code` of `input_limit`; it does not fabricate a model reply. Deadline expiry retains `run_expired` (the terminal event carries `run_deadline`; later snapshot reads use `run_expired`). The snapshot, permission/approval contracts and generic DaveHarness state schema are unchanged.

Additive `limits` and `model_usage` response fields expose the limits, admission estimate, whether a provider request was sent, provider-reported prompt/generated token counts when present, and duration/status. Missing usage stays null; no message text, thinking, credentials or node URL is included.

## Operational boundaries

Cancelling the owned HTTP request closes its transport; it is not proof that the remote provider has already stopped computing. After any timeout, cancellation or uncertain provider state, stop the batch and verify the owned request is no longer in flight before any further inference. Never unload another job's model or kill a shared service to obtain that proof.

This change does not qualify a model, authorize H9, alter sampling/context/model settings, increase the pilot budget, or add retries. Native macOS activation uses the existing installed launcher and existing app data. Preserve the previous revision as the rollback reference; do not change settings or data layout. Version remains 2.1.0, as for the preceding scoped source repair; this is not a new packaged product release. Full packaged signing, cross-platform, durability and human acceptance are outside this guard slice.
