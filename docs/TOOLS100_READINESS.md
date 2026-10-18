# Tools100 deployment-input readiness

## Reconciliation — October 18, 2026

Source checked at `8f2fc9cffa509eb60ca80508441c95d978098ce2`. This is a **source and record review**, not a new installed probe, provider call, model run or qualification. It supersedes stale routing language below without rewriting dated observations.

| Considered item | Current evidence and disposition | Remaining operation-specific gate |
| --- | --- | --- |
| DL-TOOLS-01 / all 100 candidates | The accepted October 5 Tools100 decision in [NEXT_STEPS.json](NEXT_STEPS.json) supersedes the earlier first-slice question. All 100 contracts and two freshness companions are delivered through PR71–73. [The original roadmap](DAVELLM_TOOL_ROADMAP.md) is historical design context, not an open build approval. | Registration, configured targets and exact-call effect acceptance remain distinct. Do not rebuild the delivered catalog or re-ask which first slice to approve. |
| `web.fetch` / `web.read` public connection repair | [Repair source](https://github.com/DaveHomeAssist/DaveLLM/commit/e1c89cc47edcac84b9209290640d6f01e44f7b4e) is present. [Connection fixtures](../tests/test_public_http.py) exercise public numeric-address pinning, Host/TLS identity, environment-proxy bypass, same-host redirect revalidation, mixed public/private rejection, vetted-address fallback and independent TLS connections for hosts sharing an IP. [Catalog provenance fixtures](../tests/test_tool_catalog_provenance.py) pin source/placement separately. No remaining handler/fixture gap was established by this bounded review. | Changed fingerprints do not inherit historical qualification. Passing source fixtures do not qualify a live model, prove all public destinations or authorize H9. |
| Existing source checks | [CI39100255344](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/39100255344) passed at `ab3cade4f82ecd817aac3701e765a3e924d699f4`. The only subsequent change to this review base was session-naming guidance; [CI39173987087](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/39173987087) also passed at exact `8f2fc9c`, including all three Python 3.12/3.13/3.14 jobs. The workflow runs the regression suite containing the connection and catalog tests. | Reused evidence, not a new test execution. Documentation delivery has its own exact-candidate CI gate; no runtime source or fixture changed here. |
| PR86 source excerpt correction / installed tools | The October 17 `installedToolsAcceptance` receipt in [NEXT_STEPS.json](NEXT_STEPS.json) identifies `5f88b5e`, native picker acceptance before a later shutdown, and five direct read-only tool successes. `file.read_lines` and `md.section` matched exact source text and absolute contiguous line identities. This supersedes October 12's absence of installed evidence for that correction. | This is bounded historical activation/direct-tool evidence, not today's process identity, all-tool acceptance or proof of final model-answer fidelity. The receipt's later native-readback limitation remains recorded. |
| T10/T11, Duncan diagnostic and H9 | T10/T11 remain FAIL; the four-case pilot's 10 requests / 4070 generated tokens remain spent. Duncan's one-request CPU allocation failure remains a closed diagnostic attempt. H9 remains unqualified. | No retry, tuning, budget reset, model dispatch or H9 authority follows from unused headroom or this reconciliation. A fresh operation needs its own bounded grant. |
| EX-01 through EX-21 deployment inputs | The dated matrix below names the aliases, owner allowlists, roots, helper recipes and physical evidence each family needs. [TOOLS100.md](TOOLS100.md) remains the current configuration/approval contract; PR77/78 already delivered the nonsecret checker. No current private configuration or provider capability was inspected here. | Only the dependent operation is gated: provider targets/scopes; POSIX helper configuration and capability; exact-call writes/process jobs; or physical/device acceptance. Historical Missing entries do not block independent local/advisory tools. |

**Acceptance delta:** resolve one contradictory readiness record, not add a tool. DL-TOOLS-01 is no longer an open decision; revised `web.fetch` source acceptance is distinguished from live qualification; later bounded installed evidence is recognized. No independent accepted source/fixture defect was established in the items above. DL-TOOLUX-02 remains an unanswered product choice, not permission to implement its recommended reliability or picker changes. These findings do not assert that the whole project queue is exhausted. The next eligible work must be a concrete accepted source defect, or an explicitly scoped operation with its actual inputs and effect grant; none is manufactured here.

## Reconciliation — October 12, 2026

Source checked at `cfc4bf2edfa1f974aa1d0d17cfafc4f1ca47deb1`. This is a documentation reconciliation using existing receipts, not a new runtime probe or grant. The October 7 matrix below remains a **dated historical snapshot**, not today's installed inventory.

| Evidence lane | Reconciled state | Limit |
| --- | --- | --- |
| Tool source | All 100 candidate contracts and two freshness companions are delivered. [PR71](https://github.com/DaveHomeAssist/DaveLLM/pull/71), [PR72](https://github.com/DaveHomeAssist/DaveLLM/pull/72) and [PR73](https://github.com/DaveHomeAssist/DaveLLM/pull/73) are merged; their old open/repair blockers are closed. | Registration is not provider, hardware or model acceptance. Do not rebuild these contracts. |
| Readiness checker | [PR77](https://github.com/DaveHomeAssist/DaveLLM/pull/77) and its procfs-fixture follow-up [PR78](https://github.com/DaveHomeAssist/DaveLLM/pull/78) are merged. | The earlier missing-file output is historical; the checker was not rerun here. |
| Later installed evidence | The October 7 [model-context record](MODEL_CONTEXT_SETUP.md) reports a selected `calc.eval` run returning 108. The October 9 native activation receipt records canonical source `b34e69b`, a native window, authenticated health/inventories and preserved root/toolpack configuration. | This supersedes the blanket claim that Tools100 remained uninstalled. It does not establish all 100 tools, all provider inputs or today's running-process revision. |
| Prompt acceptance | The October 9 four-case pilot at `b34e69b` is **2 Pass / 2 Fail**: T10 source fidelity and T11 format/citations remain **FAIL**; T17/T18 passed. It consumed 10 model requests and 4070 generated tokens. | All four attempts are spent; unused aggregate headroom is not a retry grant. The original 12 Pass / 5 Fail batch is unchanged. |
| Source excerpt correction | [PR86](https://github.com/DaveHomeAssist/DaveLLM/pull/86) merged as `3e0b76b`; [source/CI/publication evidence](SOURCE_EXCERPTS.md#delivered-source-and-remaining-acceptance) closes its source gates. | No installed activation or live improvement of this correction is established. |
| Current source delivery | [CI37994281894](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/37994281894) and [Pages37994281449](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/37994281449) passed at `cfc4bf2`. | Canonical local checkout was observed clean at `b34e69b` on October 12; a checkout observation is not a running-process or native acceptance check. |

Provider aliases/grants, helper configuration and physical/native acceptance remain individually evidenced inputs, not blanket Missing or Ready claims inferred from this snapshot. None was inspected or changed during this reconciliation. Duncan's October 7 CPU baseline failed after **one request**; the recorded stop-tuning condition remains in force, with no higher profile, retry or budget reset. H9 remains unqualified. See the separately dated entries in [NEXT_STEPS.json](NEXT_STEPS.json).

## Historical snapshot — October 7, 2026

Checked against `main` at `b935ea09cb0832c5ca76233db93a16ac30c5c3da`. Present-tense observations in this historical section refer only to that check.

This is a source and nonsecret-presence inventory, not an activation guide or live acceptance record. The installed router currently exposes the existing 22 qualified/extended tools and no Tools100 definitions. Neither `toolpack.json` nor `tool-roots.json` exists in the DaveLLM data directory. Dominic, Walter and Duncan answer the authenticated node-status route as online, but that proves neither model reliability nor helper readiness. No provider call, model call, mutation, helper installation, service change or restart was performed.

PR #71 was reviewed at `22a5bf332927b46d01fa6dabc09f12da4d1663a2` and has now been reconciled with current `main`. It owns selective `enabled_tools`, secure optional-file loading and `docs/TOOLS100_SETUP.md`; this record does not duplicate that work. Installed activation remains separate and has not occurred.

## Historical status meanings

- **Ready**: source needs no additional deployment input, or the required nonsecret target is currently established.
- **Missing**: a required named alias, allowlist, grant, helper or executable is not configured or not delivered.
- **Unknown**: presence or capability cannot be established without reading secret values, exercising an effect or selecting an operator-owned path.

At that snapshot, all groups required the default-off Tools100 registry to be enabled in a separately approved installed-runtime change. “Ready” below never meant installed or accepted; later installed evidence is separated above.

## Historical capability-group matrix

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

## Historical cross-cutting deployment prerequisites

1. **Installed selection and roots — Missing.** Tools100 is not registered in the running app, and the optional nonsecret configuration/root files owned by PR #71 are absent. Merge PR #71 before any installed switch-on work; do not recreate its launcher or setup guide elsewhere.
2. **POSIX helper — Missing.** No configured runner alias or deployed `scripts/tool_job_runner.py` plus private helper config is established. Dominic is the only inventoried remote POSIX candidate; choosing it still requires exact helper path, hostname, owners, roots, tools, capabilities, executables and recipes. The Mac is POSIX but is not a silent heavy-work fallback. Walter and Duncan remain unsupported for helper jobs.
3. **Named provider aliases and grants — Missing/Unknown.** No Tools100 configuration exists, so repositories, Notion data sources, Google scopes/targets, Home Assistant entities/services, Vercel projects, notifications, service endpoints and OSC destinations are absent. Secret presence or scope was not read or inferred.
4. **Effect acceptance — Missing by design.** Enabling a definition never authorizes its exact model request, mutation, process job, UDP send or provider write. Those remain lifecycle approvals plus any external grant named above.
5. **Physical/native evidence — Unknown.** Notifications, Wake-on-LAN, OSC, Art-Net, Resolume, Home Assistant and installed desktop behavior need their own scoped acceptance.

## Nonsecret readiness checker

`scripts/check_tools100_readiness.py` implements the source-only preflight. It safely
reads `toolpack.json` and `tool-roots.json`, optionally reads only an explicitly named
local nonsecret runner-config snapshot, and reports enabled names plus alias, owner,
runner-capability and executable-key presence/shape for all 21 groups. Output never
contains configuration values or paths. Secrets remain `not_read`, network remains
`not_used`, and live acceptance remains `not_checked`.

The October 7 installed-data check reported both optional files **Missing**, zero enabled
Tools100 names and 21 group rows. That is expected evidence of preserved installed
state, not a request to create files. The command exits nonzero until every reported
requirement is Ready; Unknown is intentionally not promoted to Ready.

## Historical next item and its disposition

No independent source dependency remained in that deployment-input slice. Its next
step was an explicitly approved operator-owned configuration staging pass: choose the
nonsecret starter selection and admitted roots, create them outside the repository,
run this checker, and keep provider grants, helper deployment, restart/activation and
live acceptance as separate approvals and evidence lanes.

The later receipts above supersede this as an instruction to recreate starter files or repeat the already-delivered checker. No new tool implementation gap is established by this reconciliation. The remaining product work is separately bounded installed activation/acceptance of newer source, live answer quality, named provider/helper readiness and physical/operator evidence. This document grants none of those effects, retries or H9 runs.

Live/provider-dependent rows require exact inputs rather than broad permission: a named alias and owner allowlist; the minimum required provider scope; or a specific POSIX helper path/config/executable recipe. Historical missing rows do not block the ready local/advisory tools or the delivered source-only checker.
