# Tools100 deployment-input readiness

Checked October 7, 2026 against `main` at `b935ea09cb0832c5ca76233db93a16ac30c5c3da`.

This is a source and nonsecret-presence inventory, not an activation guide or live acceptance record. The installed router currently exposes the existing 22 qualified/extended tools and no Tools100 definitions. Neither `toolpack.json` nor `tool-roots.json` exists in the DaveLLM data directory. Dominic, Walter and Duncan answer the authenticated node-status route as online, but that proves neither model reliability nor helper readiness. No provider call, model call, mutation, helper installation, service change or restart was performed.

PR #71 was reviewed at `22a5bf332927b46d01fa6dabc09f12da4d1663a2` and has now been reconciled with current `main`. It owns selective `enabled_tools`, secure optional-file loading and `docs/TOOLS100_SETUP.md`; this record does not duplicate that work. Installed activation remains separate and has not occurred.

## Status meanings

- **Ready**: source needs no additional deployment input, or the required nonsecret target is currently established.
- **Missing**: a required named alias, allowlist, grant, helper or executable is not configured or not delivered.
- **Unknown**: presence or capability cannot be established without reading secret values, exercising an effect or selecting an operator-owned path.

All groups still require the default-off Tools100 registry to be enabled in a separately approved installed-runtime change. “Ready” below never means currently installed or accepted.

## Capability-group matrix

| Group | Status | Actual named targets / source-ready portion | Required deployment inputs and present gap |
| --- | --- | --- | --- |
| EX-01 `cluster.models` | **Ready / Missing** | Current node names: Dominic, Walter and Duncan. Read-only inventory, residency, context and version contracts are source-ready. | `expected_models` aliases are missing, so drift reporting is unavailable rather than inferred. |
| EX-03 `node.diagnose` / `node.wake` | **Ready / Missing** | Diagnose can use the three configured node names without adding arbitrary endpoints. | No `wake_targets` allowlist is configured. MAC/broadcast values and physical Wake-on-LAN support remain Unknown; no packet was sent. |
| EX-16 `server.ops` | **Missing** | Local source contracts for named health probes and helper-backed observations are complete. | No Tools100 `services` aliases or `runners` exist. POSIX helper/config, container allowlists, Mac pressure recipe and fixed executables are not deployed. |
| EX-14 `notify.push` | **Missing** | Bounded ntfy transport and approval contract are source-ready. | No `notifications` destination is configured. Token presence and phone delivery are Unknown; no notification grant was exercised. |
| EX-13 `ops.governance` | **Missing** | Canonical governance, machine-access, daily communications, DaveLLM next-steps record and governed board updater exist at their operator-owned paths. | No owner-filtered `sources` aliases or POSIX runner recipe is configured for these paths. Append/update effects remain ungranted. |
| EX-10 `github` | **Missing** | The intended repository target is `DaveHomeAssist/DaveLLM`; bounded PR/check/log/Pages contracts are source-ready. | No Tools100 `repositories` alias is configured. A suitable `DAVE_GITHUB_TOKEN` grant is Unknown; issue creation would additionally require explicit write scope and approval. No `vercel_projects` alias exists. |
| EX-20 `data.tools` | **Ready / Missing** | `calc.eval`, `time.convert` and inline `json.validate` need no target. | File queries and Mermaid rendering need admitted roots, a POSIX runner, fixed DuckDB/Node/Mermaid executables and an output root. None is configured. |
| EX-06 `chat.history` | **Ready / Missing** | Owner-scoped `chat.read`, `chat.export`, `perf.read` and `token.count` use existing DaveLLM state and require no provider alias. | `chat.summarize` requires a separate inference grant and a reliable installed node/model. No such grant is active. |
| EX-09 `Notion adapter v2` | **Missing** | Existing Notion v1 live-read evidence is historical and remains separate. | No Tools100 `notion_databases` data-source aliases, owner allowlists, writable-property allowlists or kinds are configured. Current page/token presence is process-scoped and not treated as persistent readiness; no provider call was made. |
| EX-07 `memory` | **Ready / Missing** | Run-owned notepad/BRAIN proposal and write contracts need no external provider. | Named owner-filtered `sources`, a dedicated `vector_database_source`, indexing runner recipe, PDF executable/dependency and inference grant for `docs.ask` are missing. The conversation database must never be substituted. |
| EX-15 `home.assistant` | **Missing** | Fixed entity/service enforcement and readback contracts are source-ready. | No `homeassistant` service alias, entity allowlist, service allowlist or established `DAVE_HA_TOKEN` grant. No state or service call was made. |
| EX-12 `code.check` | **Missing** | `code.symbols` is source-ready once a root is admitted. | No tool roots, POSIX `runners`, owner-filtered sources, test/lint/audit recipes or fixed executables are configured. Walter and Duncan are Windows and intentionally fail closed for jobs. |
| EX-04 `route.plan` | **Ready** | Advisory routing and token estimation can use Dominic, Walter and Duncan plus the existing node profiles; it performs no dispatch. | Installed registration remains off. Fresh model inventories/residency are runtime observations, not configuration assumptions. |
| EX-05 `model.ask` | **Missing** | Named node validation exists for Dominic, Walter and Duncan. | Inference is explicitly disabled for this slice. No reliable target is accepted: Duncan's bounded CPU baseline failed and the other nodes were not exercised here. H9 authority is unrelated. |
| EX-21 `quality` | **Ready / Missing** | Deterministic prompt lint, secret scan and bounded performance-record reads are source-ready. | Benchmark/eval require a separate inference grant, accepted targets and configured evaluation inputs. None is active; H9 corpora remain excluded. |
| EX-02 `cluster.model.manage` | **Missing** | Installed-node/model validation and exact approval contracts are source-ready. | No `allowed_model_pulls`, `model_storage` or fresh helper mount evidence. Warm/unload/delete/pull are effectful and ungranted. Duncan is not a viable current `gpt-oss:20b` target. |
| EX-11 `git.write` | **Missing** | Read-only worktree, claim, preview and secret-scan contracts are source-ready after roots are admitted. | No tool roots, repository/source aliases, POSIX helper, Git author, HTTPS destination, runner-held token or effect grant. Untracked paths are refused. |
| EX-08 `docs.ask` | **Missing** | Bounded source/search/citation contracts are source-ready. | No owner-filtered document source, dedicated vector index, POSIX indexing/PDF/OCR recipe or inference grant. |
| EX-17 `google` | **Missing** | Bounded Gmail, Calendar, Drive and Contacts contracts are source-ready. | No `google_users`, calendar/folder/file aliases or established short-lived access-token grant. The source supplies no OAuth consent or refresh persistence. |
| EX-18 `av.show` | **Ready / Missing** | Inline `dmx.patch_check` needs no network target. | No Art-Net interface/helper, OSC target/address allowlist, Resolume service, MIDI map or AV inventory records are configured. UDP/OSC/physical receipt remains Unknown. |
| EX-19 `media` | **Ready / Missing** | Inline take-gap and setlist parsing are source-ready. | No admitted media roots, POSIX runner, FFprobe/Tesseract/whisper/Mermaid-style fixed executables, whisper model source or accepted vision inference target. |

## Cross-cutting deployment prerequisites

1. **Installed selection and roots — Missing.** Tools100 is not registered in the running app, and the optional nonsecret configuration/root files owned by PR #71 are absent. Merge PR #71 before any installed switch-on work; do not recreate its launcher or setup guide elsewhere.
2. **POSIX helper — Missing.** No configured runner alias or deployed `scripts/tool_job_runner.py` plus private helper config is established. Dominic is the only inventoried remote POSIX candidate; choosing it still requires exact helper path, hostname, owners, roots, tools, capabilities, executables and recipes. The Mac is POSIX but is not a silent heavy-work fallback. Walter and Duncan remain unsupported for helper jobs.
3. **Named provider aliases and grants — Missing/Unknown.** No Tools100 configuration exists, so repositories, Notion data sources, Google scopes/targets, Home Assistant entities/services, Vercel projects, notifications, service endpoints and OSC destinations are absent. Secret presence or scope was not read or inferred.
4. **Effect acceptance — Missing by design.** Enabling a definition never authorizes its exact model request, mutation, process job, UDP send or provider write. Those remain lifecycle approvals plus any external grant named above.
5. **Physical/native evidence — Unknown.** Notifications, Wake-on-LAN, OSC, Art-Net, Resolume, Home Assistant and installed desktop behavior need their own scoped acceptance.

## Next executable item

The smallest independent source-only item after PR #71 merges is a **nonsecret readiness checker**: read the optional files without revealing values, report enabled tool names and the presence/shape of required aliases, owners, runner capabilities and executable keys, and emit only `Ready`, `Missing` or `Unknown`. It must never read secret values, contact providers, install helpers, mutate configuration or restart the app. This avoids asking for credentials generically and turns the matrix above into a repeatable preflight.

Live/provider-dependent rows require exact inputs rather than broad permission: a named alias and owner allowlist; the minimum required provider scope; or a specific POSIX helper path/config/executable recipe. Missing rows do not block the ready local/advisory tools or the source-only checker.
