# Notion adapter readiness brief

## Problem and outcome

Passing FakeNotion tests does not prove that an operator has configured the
router's own Notion connection or shared the designated page. Discover that
gap without asking a model to run a tool or risking a page write.

## Smallest useful slice and flow

Add `scripts/check_notion_adapter.py`. By default it checks this process's
environment locally. An explicit `--live` performs the existing bounded
`notion.page.read` transport with a temporary in-memory ledger. Select only a
configured page alias, defaulting to `adapter-test`. Return JSON containing
configuration state and, after a live read, counts and truncation state.

## Boundaries and effects

- No inference, write, approval, H9 qualification, or service restart.
- No credential persistence, connection creation, page creation, or new API.
- No page titles, content, IDs, URLs, secrets, or remote error bodies in output.
- No router import, database initialization, or filesystem persistence.
- Use existing settings validation, read budgets and Notion transport.
- Check both tool flags as the router does; missing configuration fails closed.

The CLI's environment is not evidence of a running router's environment. A
successful live read proves only bounded transport and page readability, not
write acceptance, lifecycle integration, or model qualification. Existing
read/append/edit/reject/readback acceptance remains a separate gate.

That separate bounded gate was explicitly authorized and completed on
2026-10-05; see [live acceptance](NOTION_ADAPTER_LIVE_ACCEPTANCE.md). The CLI's
read-only defaults and exclusions are unchanged, and no H9 qualification is
implied by the later acceptance checks.

## Dependencies, risks and acceptance

Use existing Python dependencies only. Default and refused runs contact nothing;
live runs send GET requests only. Tests must cover flags, invalid settings,
unconfigured aliases, successful and partial reads, API refusals, and output
redaction including unexpected exceptions. CLI success exits 0; failed
configuration or reads exit 1. A truncated read exits 0 but explicitly reports
partial evidence. No change to tool definitions or their fingerprints.

Environment-only setup stays the existing baseline. Persistent credential
configuration remains an unanswered product choice, not part of this slice.
