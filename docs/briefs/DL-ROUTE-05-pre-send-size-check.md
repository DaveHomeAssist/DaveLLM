# DL-ROUTE-05: Pre-send prompt size check

## Problem

The node profiles (DL-ROUTE-01) say how large a prompt each node reads in reasonable time, but the user only learns a prompt is too large after sending it. The reply bubble then says so (DL-ROUTE-02) while the node reads for minutes. A project conversation is the common case: project context fills toward the model's context window, so a short draft can still produce a prompt several times the node's limit.

## Outcome

Before sending, the user sees the draft's estimated prompt size and whether it is over the selected node's limit for the selected model, and can change the node, model, project or draft first.

## Flow

1. The user types. About 600 ms after typing pauses, the composer asks the router to size the draft.
2. The router assembles the messages `/chat/stream` would send (instructions, project context, history and the draft), applies the stream's warm-cache rule, and compares the estimate with the node profile.
3. The composer shows one line in the context strip: `≈1,240 tokens`, or in amber `≈13,520 tokens · Duncan reads ~2,500 quickly`, or `≈12 new tokens to read` when the node still holds the conversation's prefix. Its tooltip gives the full sentence.
4. The check repeats when the node, model, project, conversation, attached file or Support flag changes, and clears when the draft is empty or sent.

## Smallest useful slice

`POST /chat/size-check` plus the composer line. The size comparison (`prompt_size_estimate`) and the warm rule (`reads_only_new_message`) are shared with the stream's waiting status, and the message assembly (`chat_messages_for_node`) is shared with `/chat/stream`, so the two cannot drift.

## Exclusions

- No blocking, confirmation dialog or rerouting. Limits stay advisory; the user picks the node.
- No automatic trimming of project context to fit a node. That changes the context contract and is a separate decision.
- No time estimate in seconds. Profiles hold token limits, not measured read rates.
- Images are not sized; the estimate is text only, as in the stream.
- Templates that create a new conversation on first send are sized as the current conversation.

## Rule, data, API and persistence effects

- New read-only route `POST /chat/size-check` (documented in INTEGRATION.md). It creates and changes no conversation, calls no model and does not count as in flight.
- Earlier turns past the 10-message history window count as the summary placeholder, which can be up to 150 tokens shorter than the model summary the stream writes.
- No new persistence and no new configuration.

## Dependencies

DL-ROUTE-01 profiles in `DAVE_NODES`; the loaded model inventory for the node; DL-ROUTE-03 residency probe for the warm case.

## Risks

- Request volume while typing: bounded by the debounce and by aborting the previous check.
- Project context retrieval runs per check: SQLite only, no model call.
- The estimate uses the router's 4-characters-per-token heuristic, the same one behind the stream warning.

## Acceptance

- A draft that makes a project conversation exceed the node limit shows the amber line before sending, and its token count equals the stream's waiting-status estimate for the same draft.
- A short plain draft shows a neutral line; a node without a profile shows the estimate without a limit.
- A warm follow-up reports only the new message.
- The check leaves conversations unchanged and calls no model, including for conversations past the history window.
- Automated: `tests/test_api_contracts.py` (DL-ROUTE-05 block), `tests/test_node_profiles.py`, `tests/test_console.mjs`, `tests/test_composer.mjs`.
- Human acceptance in the desktop app: pending.
