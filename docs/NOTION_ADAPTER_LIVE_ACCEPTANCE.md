# Notion adapter live acceptance

Verified on 2026-10-05, completing at 22:47 UTC. This is bounded acceptance
evidence, not model qualification or a new inference authorization.

## Authority and execution

Dave explicitly authorized all three previously described scopes: a read-only
model-driven Notion test, a plain inference smoke, and labelled test-block
append/edit/reject/refusal/readback acceptance. H9, delegation, credential
persistence, service restarts and edits to existing page content were excluded.

The existing native DaveLLM window and authenticated loopback backend were
preserved on the operator's Mac. The installed runtime source remained
`6aad6d6702e3f5f30bf75610e1e9385691dfc455`, DaveLLM `2.1.0`. The backend's
environment-only Notion connection performed every adapter operation. The
coordinator used the existing backend API credential without printing it; it
did not acquire or persist the Notion credential.

Model inference ran on the existing Walter and Duncan Ollama services. The Mac
only performed lightweight API coordination, assertions, edits and native
window inspection. No model was pulled and no service or configuration changed.

## Actual results

| Check | Host/model | Evidence |
| --- | --- | --- |
| Plain inference | Walter and Duncan, `llama3:latest` | Both native `/api/chat` requests returned exactly `ADAPTER_SMOKE_OK`, HTTP 200, 7 output tokens each, with no tools supplied |
| Model-driven Notion read | Walter, `gpt-oss:20b` | Authenticated lifecycle run completed in 2 model steps; `notion.page.read` succeeded, 11 blocks, no truncation or unread children |
| Approved append | Walter, `gpt-oss:20b` | Exact pending call approved through the lifecycle decision API; 2 uniquely labelled new blocks, adapter outcome `verified` |
| Approved plain edit | Walter, `gpt-oss:20b` | Only the new paragraph was targeted using its run-local ref; adapter outcome `verified` |
| Rejected checklist edit | Walter, `gpt-oss:20b` | Valid edit of the new checklist was rejected; terminal `approval_rejected`, no update handler result, checklist remains unchecked |
| Unconfigured page | Walter, `gpt-oss:20b` | Adapter returned its configured-page refusal; terminal `error_budget` was expected for this negative test |
| Formatting-preserving edit | Walter, `gpt-oss:20b` | Exact edit inside the new paragraph's bold run approved; adapter outcome `verified`; subsequent model-driven read succeeded |
| Independent preservation | Notion connector readback | Complete original page markup unchanged, including sentinel; new paragraph retains bold, italic and link; exactly 13 blocks after 2 additions |

The adapter append accepts plain text only. To prepare the formatting fixture,
the Notion connector made one exact targeted update to the **newly appended**
paragraph, adding bold, italic and a link. DaveLLM then performed the actual
rich-text edit. Connector fixture preparation is not counted as an adapter
write. No pre-existing block was edited. Three adapter writes were approved:
one append, one plain edit and one formatted edit. No append was replayed.

Seven lifecycle runs were bounded to 1–4 model steps each, at most 1,536 output
tokens per model step, one error per run, and a 180-second client deadline.
Completed assertions inspect tool results and fresh page data, not the model's
claim of success. Private run IDs, decision digests and host addresses belong
in the shared communications record, not this public projection. Existing
page content and credentials are omitted from that log.

## Failures retained, not hidden

- The initial legacy tool-loop smoke supplied tool schemas to `llama3:latest`.
  Ollama returned HTTP 400: that model does not support tools. The subsequent
  plain-chat smoke passed. HTTP 200 from the DaveLLM wrapper did not make the
  failed model invocation a success.
- The first rejection assertion was stricter than the schema: the model sent
  nullable optional text arguments. The coordinator failed closed and rejected
  the pending call. Inspection confirmed the exact owned checklist edit and
  terminal rejection; no extra rejection run or write was needed.
- Walter's first formatting attempt invented `notion.block_update` rather than
  `notion.block.update`. The registry refused it as `tool_unknown`; no write or
  approval occurred. A final bounded attempt with the exact name passed.
- Duncan's `gpt-oss:20b` fallback failed before any tool call. A bounded native
  diagnostic returned HTTP 500: Vulkan buffer/KV-cache allocation failed during
  model startup. This is a model-load failure despite a healthy service and a
  successful `llama3:latest` smoke. No context, GPU or service setting was changed.

## Availability and remaining boundaries

Authenticated final node checks confirmed Walter and Duncan online. Fresh
inventories contained:

- Walter: `llama3:latest`, `qwen3-coder:30b`, `gpt-oss:20b`.
- Duncan: `gpt-oss:20b`, `llama3:latest`, `qwen3-coder:30b`, `gpt-oss:120b`,
  `gemma3:27b`.

Dominic remained offline after its tags timeout. Dave reported a disconnected
cable; reconnection and a fresh inventory have not been verified. Its earlier
same-session `llama3:latest` inventory is historical, not a current result.

The native desktop window and existing backend remained healthy. API approvals
do not prove the Electron approval-card interaction or human acceptance. No H9
corpus, throughput evaluation, additional model qualification, release/version
change or persistent Notion setup was performed. The other installed models
were inventoried, not all exercised by inference.

## Source delivery

The inference-free readiness CLI remains unchanged. PR #58 passed all required
Python 3.12/3.13/3.14 checks on exact head
`5644df68941d5857f142b2ff077736581fe0d263` (run `37382405607`) and merged by
protected squash as `2a2df35e8628a3688186087b3a9f1faac9002547`. The Pages provider
reported that commit built, and the public documentation root returned HTTP
200. This does not update or restart the installed native runtime.

This evidence-only followup must pass its own protected delivery gates. Live
adapter evidence and repository/CI/Pages delivery are separate proof lanes.
