# DaveLLM console implementation

The runtime in `static/` implements the supplied DaveLLM Redesign handoff with the existing FastAPI and Electron contracts. `docs/` remains the documentation site, not the application deployment.

## Preserved requirements

- Conversation age groups remain color coded: green under 24 hours, amber 1–3 days, red 3–7 days, gray over 7 days. Labels and elapsed times accompany the colors. No alternate palette proposal was applied.
- Light and dark themes share layout and semantic tokens. Light is the first-run default.
- Chat, Projects, Cluster, and Settings use a side rail on desktop and bottom navigation on phones. History and the Context/Run/Notepad inspector open on demand.
- The chosen node and model remain explicit. Project attachment uses a separate confirmation action for the selected chat.
- Tool approvals preserve exact arguments, digest, nonce, fingerprint, and call ID. Before/after text, expiry, Approve once, Reject, and draft preservation remain available on narrow screens.
- Existing attachments, dictation, export, project instructions, files, artifacts, BRAIN, and notepad continue through their existing handlers.

## Live data and deliberate adaptations

Cluster uses node health, per-node model inventory, model failures, recent errors, and estimated costs from existing authenticated endpoints. It does not reproduce the mockups' example hardware, fake latency history, or invented usage values. API round-trip time is labeled separately from inference speed. The old monitoring URL redirects into Cluster.

Context preview uses the router's actual sequential budget with unused capacity carried forward; it does not imply independent fixed quotas. It is a preview snapshot, not measured usage from an earlier reply.

Model text is rendered with text nodes. Headings, bold text, inline code, and fenced code with Copy are supported; arbitrary model HTML is displayed as text. No frontend framework or runtime dependency was added.

## Verification

- Python regression suite: 757 passed, one skipped.
- Node tests cover approval preview, stale run event isolation, and age boundaries.
- Browser acceptance covers widths 390, 820, 1024, 1280, 1440, 1920, and 5120; both themes; inventory; project creation; streamed replies; exact approval rejection; draft retention; and credential cancellation. See `tests/browser_console.cjs` and the README instructions. Inference and approval responses are fixtures; project creation uses a disposable real backend.
- Native Electron startup verified the new shell, authenticated inventory, navigation, and credential isolation using disposable storage.
- Physical phone keyboard behavior and live-model inference are not established by these checks.

The transitive Electron download dependency `undici` was patched from 7.29.0 to 7.30.0 because the required dependency audit rejected the older lockfile. Electron remains 44.0.0.
