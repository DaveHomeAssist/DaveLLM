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

## Original redesign verification (historical)

- Python regression suite: 757 passed, one skipped.
- Node tests cover approval preview, stale run event isolation, and age boundaries.
- Browser acceptance covers widths 390, 820, 1024, 1280, 1440, 1920, and 5120; both themes; inventory; project creation; streamed replies; exact approval rejection; draft retention; and credential cancellation. See `tests/browser_console.cjs` and the README instructions. Inference and approval responses are fixtures; project creation uses a disposable real backend.
- Native Electron startup verified the new shell, authenticated inventory, navigation, and credential isolation using disposable storage.
- Physical phone keyboard behavior and live-model inference are not established by these checks.

The transitive Electron download dependency `undici` was patched from 7.29.0 to 7.30.0 because the required dependency audit rejected the older lockfile. Electron remains 44.0.0.

## DL-05 navigation repair — October 5, 2026

Current main `179a79c0459a77976249bbf1f7a3672496726dcc` still had the recorded defects: Chat's main was nested in a region; the other views had no main; the skip link pointed into hidden Chat content; switching views left focus on the rail. Implementation `792b75e8ffc85544c822da7fc044d31044de1281` in [PR #62](https://github.com/DaveHomeAssist/DaveLLM/pull/62) repairs only that bounded scope.

Each view now supplies a named, top-level main, with inactive views hidden. The skip link follows the active view and focuses it without changing URL fragments or scroll position. View changes focus the new main; startup and same-view updates leave existing focus alone. Chat's inner panel remains a named section with all existing IDs and handlers intact. Hash/Back/title routing, streaming announcements and dialog behavior are not changed.

This follows the [W3C main landmark pattern](https://www.w3.org/WAI/ARIA/apg/patterns/landmarks/examples/main/) and [skip-link focus guidance](https://www.w3.org/WAI/WCAG21/Techniques/general/G1). The targeted checks do not establish full WCAG AA conformance.

`tests/browser_navigation.cjs` runs only as an isolated fixture with the existing Electron runtime and Xvfb. It first loads the pinned baseline and asserts the four recorded defects, then checks Chat, Projects, Cluster and Settings in light/dark at 1440×900, 375×812 and 3840×1080 (24 combinations). Real Enter and Tab input checks rail activation, visible skip-link activation and focus entering content. The Chromium accessibility tree must expose exactly one correctly named main. DOM checks also require a top-level landmark, a valid heading and skip target, one current navigation item and no page overflow. Startup and same-view updates must preserve focus.

All API responses are synthetic; unknown or mutating requests fail, and outside network attempts are blocked and fail the fixture. The fixture creates its own temporary Electron profile and never imports the production launcher/backend or reads operator credentials/data. It does not perform Notion calls, inference or H9 qualification. The existing Python 3.14 CI job gates this renderer fixture; all three required Python jobs remain required for protected merge. Five bounded console VM tests passed locally. Required CI run 37418633159 passed on exact head 685d478, including the Electron renderer fixture; PR #62 merged at 92dc458.

The first fixture run ([37398025121](https://github.com/DaveHomeAssist/DaveLLM/actions/runs/37398025121)) failed while activating Projects on the pinned baseline, before candidate checks. It made no unexpected/external requests. The fixture originally sent only keydown/keyup; [Electron's keyboard tests](https://github.com/electron/electron/blob/v44.0.0/spec/api-web-contents-spec.ts#L1770) send keypress as a separate char event. The final correction delivers Enter and Tab through awaitable Chromium input events and explicitly brings the page to the front before input. The follow-up fixture disables background throttling and retries native input only when the renderer records no events. Native/DOM focus and trusted keypress delivery remain asserted. Failed CI runs 37398623096, 37418008233 and 37419336747 remain recorded; no application assertion, viewport or gate was weakened.

The canonical owner checkout, installed desktop/backend, configuration and healthy processes remain unchanged. GitHub Pages publishes these records, not the `static/` application. VoiceOver, physical phone keyboard behavior, installed-app navigation and the native approval card require separate acceptance; no restart or installation is authorized by this slice. DL-06 live-region behavior is the next independently eligible source revalidation. D-006 stays Open, and no proposed diagnostic or qualification grant is activated.
