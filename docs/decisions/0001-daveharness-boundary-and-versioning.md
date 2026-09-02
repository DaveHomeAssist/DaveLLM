# Decision: DaveHarness boundary and release versioning

**Status:** decided

**Date:** 2026-09-02

**Decision-maker:** Dave Robertson

**Baseline:** `PROJECT_SPEC.md` at commit `3511410`

## Decision 1: DaveHarness product boundary

**Recommendation:** Make DaveHarness a headless library contract inside the DaveLLM repository, consumed in-process by DaveLLM. Give it an independent package and release identity when the extraction is implemented. Do not create a separate network service or repository yet.

**Confidence:** high

### Options

1. **Keep the harness as an unnamed DaveLLM module** — Preserve the current `tool_executor.py` arrangement indefinitely.
2. **Create an in-process DaveHarness library boundary** — Separate ownership and API contracts while retaining one repository and runtime.
3. **Create a separate DaveHarness service and repository now** — Give the harness its own process, HTTP boundary, deployment, and release lifecycle.

### Analysis

| Option | Advantages | Disadvantages | Cost | Primary risk |
|---|---|---|---|---|
| Unnamed module | No migration and minimal short-term maintenance. | Product responsibilities remain blurred; reuse and independent tests become harder. | Low | DaveLLM continues accumulating execution-engine concerns. |
| In-process library | Clear product boundary, independently testable engine, no new network or operations burden, reversible extraction path. | Requires a deliberate package interface and adapter migration. | Medium | A weak interface could leak DaveLLM state back into the library. |
| Separate service | Strongest deployment and ownership separation; reusable by remote clients. | Adds authentication, transport, failure, deployment, compatibility, and observability surfaces before they are needed. | High | Premature distribution makes a local safety boundary harder to reason about. |

### Chosen boundary

| DaveLLM owns | DaveHarness owns |
|---|---|
| Electron/browser UI and operator interactions | Tool definition and registry contracts |
| FastAPI HTTP routes and API-key authentication | JSON-schema validation and tool-call normalization |
| Ollama node configuration, inventory, and model transport adapter | Permission classes and per-run approval state machine |
| Conversations, projects, instructions, BRAIN, files, artifacts, and notepad | Bounded model/tool execution loop |
| Runtime persistence, cost/feedback data, dictation, and monitoring UI | Step, timeout, and error budgets |
| Product-specific tool implementations and configured roots | Structured execution events, terminal states, and complete transcripts |

DaveHarness must not read DaveLLM persistence, environment variables, node configuration, HTTP requests, or UI state directly. DaveLLM supplies model invocation and tool-handler interfaces. DaveLLM remains responsible for authenticating the operator and deciding which implementations and roots are available.

The current `tool_executor.py` is the compatibility implementation until extraction into a dedicated `daveharness` package. Its public behavior must remain stable during that move.

### Consequences

- DaveLLM and DaveHarness are separate product concepts with an explicit dependency direction: DaveLLM depends on DaveHarness, never the reverse.
- One process and repository remain the operational default.
- A second service, repository, or remote protocol is out of scope until justified by evidence.
- Extraction must preserve the existing default-off tool posture, approval gates, containment, complete partial transcripts, and test coverage.
- Revisit the process/repository decision when there is a second production consumer, independent deployment cadence, incompatible dependency needs, or a required remote execution boundary.

## Decision 2: Release-version scheme

**Recommendation:** Use Semantic Versioning for DaveLLM with root `VERSION` as the canonical source. Normalize the existing Router `2.1` identity to release `2.1.0` and require every product surface to match it.

**Confidence:** high

### Options

1. **Keep independent Router and desktop versions** — Preserve `2.1` and `1.0.0` as separate identities.
2. **Use `package.json` as the only source** — Make the Python runtime parse the Node manifest.
3. **Use a neutral root `VERSION` file** — Make Python and JavaScript manifests consume or validate one product version.

### Analysis

| Option | Advantages | Disadvantages | Cost | Primary risk |
|---|---|---|---|---|
| Independent versions | Components can release separately. | The installed product has no single support identity and drift is already present. | Low | Bug reports and artifacts name conflicting releases. |
| `package.json` authority | Existing SemVer field; no new file. | Makes the Python product depend on a Node-specific manifest and keeps product metadata coupled to packaging. | Low | A packaging concern becomes cross-runtime authority. |
| Root `VERSION` authority | Runtime-neutral, easy to read, easy to validate, compatible with tags and future packaging. | Mirrored manifests still require an automated drift check. | Low | Manual updates drift unless CI enforces equality. |

### Version contract

- `VERSION` contains the complete DaveLLM SemVer value and is the source of truth.
- FastAPI metadata, startup output, `GET /health`, `package.json`, and the root entry in `package-lock.json` must match it.
- CI must fail when a mirrored version differs from `VERSION`.
- Git release tags use `vMAJOR.MINOR.PATCH` and must equal `VERSION` for the tagged commit.
- Increment **MAJOR** for incompatible user-facing, API, persistence, or configuration contracts.
- Increment **MINOR** for backward-compatible capabilities.
- Increment **PATCH** for backward-compatible fixes. Documentation-only commits do not require a version increment.
- Pre-release identifiers follow SemVer, for example `2.2.0-beta.1`.
- The current DaveHarness compatibility module inherits the DaveLLM release version. When the dedicated package is created, it starts at `0.1.0`, follows independent SemVer, and declares the compatible DaveLLM adapter range.

### Consequences

- DaveLLM now has one operator-visible support version: `2.1.0`.
- A release must update `VERSION` and generated/mirrored package metadata together.
- DaveHarness can evolve independently after extraction without forcing its internal API version to equal the desktop application's version.
- The version rule is enforceable without importing the full application or contacting a runtime node.
