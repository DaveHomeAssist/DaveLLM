# Tools100

All 100 approved candidates have separate schemas and implementations in `davellm_toolpack_catalog.py` and the local, cluster, HTTP, Notion and runner modules. The generated [capabilities catalog](DAVEHARNESS_CAPABILITIES.md) is the full argument/permission reference. The 100-name contract is independently pinned by `tests/test_toolpack.py`; the two additional tools are `project.notepad.snapshot` and `git.write_preview`, which supply freshness evidence for approved writes.

This is an opt-in source delivery, not an installed-runtime upgrade. No existing launcher, service, credential store, model inventory or native UI is changed. Existing qualified handler provenance and Notion v1 behavior remain pinned. Provider and physical acceptance are separate from offline tests.

## Enabling

`DAVE_ENABLE_TOOLS=true` and `DAVE_ENABLE_TOOLPACK=true` register the expansion at startup. Inference additionally needs `DAVE_ENABLE_TOOL_INFERENCE=true`; SSH jobs additionally need `DAVE_ENABLE_TOOL_JOBS=true`. All default to false. `DAVE_TOOLPACK_CONFIG` is at most 64 KiB of operator-owned nonsecret JSON. Malformed/unconfigured targets fail closed.

Every mutation, model request, process job and UDP send requires exact-call lifecycle approval. Build approval does not automatically accept tool calls. Read-only network probes use named configured nodes/endpoints. No tool accepts credentials, a shell command, arbitrary provider URL or arbitrary remote host. A registry captures configuration and credentials in a private fingerprint; changing either invalidates pending calls and requires a fresh configured registry. Credential fingerprints are not published in the capabilities manifest.

## Configuration

The following are configuration **types**, not a runnable configuration or instructions to supply private values in chat. Target aliases are the only provider destinations accepted in calls. Keep real configuration and credentials out of source control. `users` contains existing DaveLLM owner IDs, not display names. Sources and runners require explicit owners; optional `users` on other mapping targets further restricts access. Scalar provider aliases represent operator-approved shared targets.

| Key | Admitted value / use |
|---|---|
| `sources` | Alias to `{path, users}`. Router paths must also fall inside `DAVE_TOOL_ROOTS`; runner paths inside its separate roots. Secret components and symlinks are refused. |
| `runners` | Alias to `{ssh_alias, os, python, script, config, host, users, tools, capabilities}`. `os` is `posix`; `host` must equal the helper's observed hostname. Absolute Python/helper/private-config paths are fixed by the operator. |
| `expected_models` | Node alias to expected model-name list. Without expectations, drift is unavailable, not an inferred removal. |
| `allowed_model_pulls` | Node alias to specific model names. Pull also needs fresh job-based storage evidence. |
| `model_storage` | Node alias to `{runner, minimum_free_bytes}`. Runner must have a configured observed mount; missing/false mount evidence refuses pull. |
| `wake_targets` | Node alias to `{mac, broadcast}` on an operator-selected LAN. One packet; service response is not proof of physical wake. |
| `repositories` | Alias to GitHub `owner/repository`; `DAVE_GITHUB_TOKEN` stays in the router environment. |
| `vercel_projects` | Alias to Vercel project ID; `DAVE_VERCEL_TOKEN` in router environment. Status only, no deployment. |
| `services` | Alias to `{url}` for health; `resolume` additionally needs `kind: resolume`. `homeassistant` uses `{url, entities, allowed_services}` and `DAVE_HA_TOKEN`. No templates/reloads. |
| `notion_databases` | Alias to `{id, users, writable_properties, kind}` for an actual Notion **data source**. `kind` is `routines`, `inbox` or `session_log` where needed. Inbox optionally uses `status_property`, `resolved_values` and at most 20 `routing_rules: [{contains, project}]` to propose configured destinations; no automatic routing. Pages/token retain existing `DAVE_NOTION_PAGES` and `DAVE_NOTION_TOKEN`. No database discovery or provisioning. |
| `google_users` | Explicit owners of the router-held `DAVE_GOOGLE_ACCESS_TOKEN`. This release supplies no OAuth flow or refresh-token persistence. |
| `calendars`, `drive_folders`, `drive_files` | Alias to specific provider IDs. Gmail uses the admitted OAuth account; Drive search is confined to the configured folder. |
| `notifications` | Alias to `{url, topic}` for HTTPS ntfy, optional `DAVE_NTFY_TOKEN`. Duplicate suppression 10 minutes, per-destination minimum interval 60 seconds. Transport acknowledgement is not phone acceptance. |
| `osc_targets` | Alias to `{host, port, addresses}` on a fixed private network destination. String packets only, address allowlist. Receipt remains Unknown. |
| `glossary`, `midi`, `av_inventory` | Nonsecret operator records keyed by names. Lookups do not infer current hardware health. |
| `vector_database_source` | Source alias for a dedicated existing SQLite index, at most 1 MiB, shared read-only with the router. Never configure the conversation DB here. Owner/source-scoped records replace only that index slice; active WAL/journal files or unrelated tables refuse indexing. |

## Runner installation contract

The provided `scripts/tool_job_runner.py` is a one-shot helper, not a service or agent. Install the reviewed source on an approved **POSIX** runner using existing machine-access procedures; keep its absolute private JSON configuration operator-owned and not group/world-writable. The helper checks ownership, identity, roots, users and per-tool allowlists independently of the router. Router and helper roots/configuration are separate; don't assume local Mac paths exist remotely. SSH host-key checking is mandatory.

Windows job execution intentionally fails closed until an operator-provided job-object watchdog is implemented. Duncan's Windows host is not declared a capable POSIX runner. Mac-only pressure commands require an explicitly configured Mac recipe; registration does not route them onto an active Mac automatically.

Runner configuration uses `roots`, `users`, `tools`, `sources`, `executables`, `recipes`, `containers`, `interfaces`, `storage_source`, `mount_path`, `whisper_model_source`, `vector_database_source`, `nextsteps_updater`, `git_author` and `git_destinations` as applicable. `executables` are fixed absolute installed paths. Test/lint/audit recipes map tool name and recipe alias to `{source, argv}`; caller-selected commands/environment are never accepted. The governed next-steps updater is a fixed operator script, used once to update and once with `--check`. It is not bundled or replaced here.

PDF/data jobs use the optional `requirements-toolrunner.txt` dependencies (`pypdf` and DuckDB), installed only on a runner. They add concrete bounded PDF extraction and isolated in-memory read-only SQL, not router dependencies. OCR, transcription, probing and diagrams require existing configured Tesseract, whisper.cpp/model, FFprobe and Mermaid CLI installations. This release installs none of those binaries or models. Decoder input is copied to a private temporary directory after descriptor-secure admission. FFprobe forces a standalone container format, refuses playlists and network protocols, and disables MOV external tracks/absolute references. Mermaid accepts only bounded text-only flowcharts, disables HTML labels, checks SVG active content and never overwrites an existing output.

Git commits require a named **tracked-file** diff from `git.write_preview`, no existing claim or staged work, and a matching digest. Only the validated index is committed, with hooks/filters disabled. Push requires a clean matching branch/head and one configured HTTPS GitHub destination; no force/history rewrite. A runner-held `DAVE_GITHUB_TOKEN` is required independently of the router credential. It is supplied to Git only through a destination-scoped private environment header, never argv; repository URL/HTTP/include overrides and redirects are refused. Neither runner credentials nor recipes are provisioned by registration.

Transcription accepts standalone WAV input. OCR admits standalone PNG, JPEG, TIFF or WebP signatures. Conversion, binary installation and model installation are operator setup, not implicit tool side effects.

## Limits and outcomes

- Closed schemas; every array capped at 50, nesting at 16. JSON output at most 48 KiB, provider captures at most 512 KiB, SSH captures at most 64 KiB.
- Registry deadline 60 seconds; provider request deadline 12 seconds, log downloads 20 seconds, helper wall deadline 30 seconds and CPU soft/hard limits 18/20 seconds. Remote SSH closes by 40 seconds. Closing SSH alone does not prove cancellation of a dispatched effect.
- Secondary inference has at most three requests per run, 256/512 output tokens per call, installed target validation, no recursive tools or forwarded reasoning. Summaries require observed model residency. This is not H9 qualification or a reliability corpus; exact token counting remains an estimate.
- Notepad writes lock freshness and autosave together; BRAIN uses existing revision CAS and readback. Legacy cost rows are shown only for still-owned conversations. Exports return bounded Markdown; no file is implicitly written.
- Notion v2 restricts data sources/properties/options, uses private run-local row refs, compares fresh snapshots immediately before writes and checks targeted readback/untouched properties. Same-destination writes are serialized. Notion offers no atomic compare-and-swap: remote concurrent edits between GET and PATCH remain a provider limitation. Page diff is metadata/properties, not a block-tree diff. Search is configured-target titles only; inbox triage returns proposals and never routes automatically.
- Dispatched HTTP effects retain an Unknown ledger before send and continue bounded readback after caller cancellation. Unknown is never retried automatically. UDP, HA service calls and notifications distinguish acknowledgement from physical/device acceptance.
- PDF reads at most 1 MiB/10 pages; table data at most 64 KiB/1,000 rows/30 columns, one SELECT with DuckDB external access and extension loading disabled. Document indexing is at most 40 walked entries/1 MiB. Regex has process isolation, not an in-event-loop execution mode.
- Provider/OS errors are fixed messages; credential/address fields and credential patterns are redacted. Heuristic secret scanning is not proof that an artifact contains no private data.

## Evidence lanes

Tests exercise isolated router registration/defaults, every candidate schema, exact-call permission/preflight metadata, private provider fixtures, ownership/freshness/refusals, ambiguous effects, cancellation/readback and disposable local data/Git operations on a bounded Linux runner. Existing provenance tests protect old tools. The full repository/CI gates remain required.

No test result in this release establishes actual provider grants, account permissions, SSH helper deployment, model/hardware capability, installed-desktop tool availability or physical acceptance. Those are named configuration and separately scoped runtime checks, never implied by the 100-tool catalog.
