# DaveLLM design overhaul: component manifest for Claude Design

| | |
|---|---|
| Product | DaveLLM 2.1.0 (router + Electron/browser UI) |
| Reviewed at | `origin/main` a927755 (UI files unchanged since f6a672f), 2026-09-26 |
| UI sources | `static/index.html` (369 lines), `static/app.js` (4,305), `static/style.css` (2,859), `static/monitoring.html` (233), `static/anticipation.js` (334), `static/prompt-contract.js`, `desktop/main.js` |
| Screenshots | [`current-ui/`](current-ui/). These are before images from a scratch instance with sample data. Nodes are deliberately offline. |
| Purpose | Break the current UI into components so it can be redesigned in Claude Design and implemented against the existing code without breaking contracts |

---

## 0. How to use this file in Claude Design

1. Start a new Claude Design project. Attach this file and the PNGs in `current-ui/`. You can also connect the `DaveHomeAssist/DaveLLM` repo and point it at `static/`.
2. Paste the kickoff prompt in §1.
3. Work through the build order in §10, one surface per session. Build foundations (§7) first so every later screen uses the same tokens and primitives.
4. Each component has an ID (for example `CH-12`). Use these IDs in Claude Design prompts ("redesign CH-12 approval card, all states") and in the handoff back to code.

---

## 1. Kickoff prompt (paste into Claude Design)

> Redesign **DaveLLM**, a single-user, local-first desktop chat client that routes prompts to Ollama nodes on a private tailnet. It runs as an Electron app on macOS and in a browser, and on a phone over the tailnet. The user is a technical power user who works across AV/lighting, networking, home automation and software, and switches context constantly.
>
> Attached: a component manifest (IDs, states, data, constraints) and screenshots of the current UI. Keep every capability in the manifest unless it is marked **Remove**. Do not invent data the manifest says the backend doesn't provide. If you add a design-only slot, mark it as a backend dependency.
>
> Hard requirements: a **light theme (default) and a dark theme** with a visible toggle. A **viewport-height app shell** where scrolling happens inside panels, not the page. **Tabbed subject areas**: side rail or top nav on desktop, **bottom rail on mobile**. A deliberate **32:9 ultrawide layout** (5120×1440) that uses the extra width for real columns. Also required: WCAG AA contrast, visible focus, 44px touch targets on touch devices, and a reduced-motion variant. Icons come from **Lucide** only (no emoji as UI icons).
>
> Tone: a calm, dense, trustworthy operator console, not a toy chatbot. Tool-approval screens are security surfaces. They must show exactly what will run, and the design must never hide arguments to look tidy.
>
> Start with foundations: color tokens for light and dark, type scale, spacing, radius, elevation, and the Lucide icon set. Next, the primitives in manifest §7. Then the app shell at 390, 820, 1280, 1440, 1920 and 5120 widths.

---

## 2. Product snapshot

**What it does**
1. **Chat** with a chosen node and model. Responses stream token by token and can be stopped mid-stream. Image, audio and file attachments are supported, along with dictation.
2. **Tool runs**: an agent loop that calls local tools. Mutating calls pause for exact-call approval.
3. **Project context**: layered instructions (global → project → session), a project notepad, a Project Homepage (instructions, file references, artifact history, BRAIN memory tiers, context budget, request preview).
4. **Cluster awareness**: node online/offline status and latency, the models on each node, model health, errors, cost analytics, and Hugging Face model fetch.

**Who and where**
- One user (Dave). He is an expert and keyboard-heavy, and wants to see what is really going on rather than have it abstracted away.
- Primary use is the Electron window (default 1280×900) on an M4 MacBook Air, often on a **Samsung CRG9 32:9 (5120×1440)** alongside a 27" 4K display. Secondary use is a phone browser over Tailscale.
- Cluster (sample names are fine for mocks): `max` (Apple M4 laptop), `walter` (RTX 3070 laptop, fastest GPU), `duncan` (large-RAM box that runs 120B models slowly), `dominic` (CPU-only). Nodes come and go, and offline is a normal state, not an exceptional one.

---

## 3. Hard constraints

### 3.1 Workspace UI rules (binding)
| Rule | Requirement | Current state |
|---|---|---|
| WEB-1 | Light and dark modes, **default light**, visible toggle | ❌ Defaults to **dark**. The toggle cycles dark → light → **forest** (3 themes). |
| WEB-2 | Viewport-height shell, tabs for subject areas, scrolling inside panels. Desktop nav top or side, **mobile nav bottom** | ⚠️ Shell is viewport-locked. Mobile tabs are at the **top**. Monitoring is a separate page. Project Home is a modal. |
| WEB-3 | An extra **ultrawide breakpoint**, evaluated at 32:9, that uses the width for real columns | ❌ None. The middle column stretches and the side columns stay capped at 260/310px (see `07-ultrawide`). |

### 3.2 Product and trust constraints (must survive the redesign)
- **API key**: never displayed. In Electron the key never reaches the renderer. In a browser it lives in `sessionStorage` only, never `localStorage`. Credential entry needs a designed dialog (it's `window.prompt` today). The dialog has a masked field and says "this browser tab only".
- **Approval card (CH-12)**: must show the tool name, permission class, call ID and the **full exact arguments**, plus a before/after diff for `file.edit`. The only actions are **Approve once** and **Reject**. There is no "always allow" and no "remember". It takes focus when it appears.
- **Untrusted text**: everything a model produces is rendered as text through DOM text nodes (`innerHTML` is banned and enforced by tests). Markdown styling is fine to design. The implementation will be a safe DOM builder, so avoid designs that need raw HTML from the model.
- **No runtime CDN**: icons come from the vendored Lucide sprite (`static/vendor/lucide/lucide.svg`) and motion from vendored GSAP 3.15. **Fonts must be vendored locally.** The current CSS names `Inter` but never loads it, so it falls back to the system font. Choose a font you're happy to ship as local files.
- **Node addresses**: node cards currently print the raw node URL. The repo is public and screenshots get shared. Lead with name, status and latency, and put the address behind a disclosure or tooltip.
- **Reduced motion**: every animation needs a `prefers-reduced-motion: reduce` variant. The Project Home entrance (a GSAP timeline) already has one.
- **Accessibility already in place**: skip link to chat, `role="status"`/`aria-live` status lines, focus moves to the approval heading, ARIA tablist on mobile, message actions revealed on hover (always visible on touch). Keep all of these.
- **Nothing auto-sends**: suggestions prefill or select. They never submit, and they can always be undone.

---

## 4. Current visual language (as-is, for reference only)

- **Aesthetic**: "aqua glass HUD". Navy background, radial glows, a 28px grid overlay, gradient glass panels with a cyan hairline, gradient buttons.
- **Type**: `"Inter", system-ui` (Inter not loaded). The base is about 14px, with 12px meta text, 19px brand and uppercase kickers. Code uses the system monospace.
- **Shape**: `--radius: 10px`. Heavy glow shadows in dark.
- **Icons**: three systems mixed together:
  - inline SVG in the top bar
  - the Lucide sprite in the composer
  - **emoji used as controls**: 🔄 🚀 📦 ⬇️ ✏️ 🗑️ ❌ 📥 👍 👎 🔍 💾 👤 🤖 👁️ ⚡ 💡 ➕.

Replace all of them with Lucide.

| Token | Dark (default today) | Light | Forest |
|---|---|---|---|
| `--bg` | `#071726` | `#f7f9fc` | `#0b1812` |
| `--panel` | `#12344c` | `#ffffff` | `#102219` |
| `--text` | `#f0fbff` | `#1f2a35` | `#e6f4ec` |
| `--muted` | `#b8cbd5` | `#5a6a7c` | `#8fb3a2` |
| `--accent` | `#69eee1` | `#2f80ed` | `#5ccf87` |
| `--accent-blue` | `#78b9ff` | `#2f80ed` | `#74dba2` |
| `--success` | `#48e38a` | `#27ae60` | `#43d17a` |
| `--danger` | `#ff8a92` | `#e74c3c` | `#ff847c` |
| `--border` | `rgba(101,226,224,.42)` | `#dbe2ea` | `#1d3a2b` |

Token debt:
- Hard-coded colors bypass the tokens: `#ff6b6b` on node errors, `#9ba4b5` on memory previews, and `monitoring.html` has its own GitHub-dark palette.
- JavaScript sets inline styles on conversation items, node headers, the search box and images.
- Light and forest themes are implemented as about 90 per-selector overrides instead of token swaps.

The new system should be **tokens only**, so that switching theme swaps variables and nothing else.

---

## 5. Information architecture

### 5.1 As-is
```
Top bar: brand · runtime pill (node ● status / Ollama / model) · [key] [theme] [monitoring + badge]
(≤1024px) Tab strip at top: Chat | History | Runtime
3-column grid
├─ Conversations (left, 230–260px): search, project row, template row, list, Router Stats, Memories
├─ Chat (center): header [Instructions][Notepad], notepad drawer, thread, run ledger,
│                 Suggested next, context strip, composer, attach tools, Send
└─ Telemetry (right, 270–310px): node cards, node/model selects, routing inputs, HF fetch
Modals: Project Homepage (<dialog>), Session instructions (<dialog>)
Separate page: monitoring.html
Browser natives: 1 credential prompt (+1 in monitoring.html), 3 chained prompts for "New project", 17 alerts, 8 confirms
```

### 5.2 Proposed (recommendation; Claude Design should explore it and may push back)
```
App shell (viewport-locked)
├─ Nav rail (desktop: left icon rail · mobile: bottom rail)
│   Chat · Projects · Cluster · Settings        (+ status badge on Cluster)
├─ Global header: brand, runtime pill (node/model switcher), theme toggle, ⌘K
└─ Workspace (per tab)
    Chat      = conversation rail | thread + composer | inspector (context, memories, active run)
    Projects  = Project Homepage promoted from modal to full view (PH-*) + notepad + instructions
    Cluster   = node grid + model inventory + health/errors + usage + model fetch (merges RT-* and MO-*)
    Settings  = theme, browser credential, global instructions, suggestions reset, about/version
```

**Breakpoint behavior to design**

| Viewport | Layout |
|---|---|
| 390×844 phone | Bottom rail. The thread fills the screen. Context strip and suggestions collapse into one row above the composer. The conversation list and inspector become full-screen tabs or sheets. |
| 820×1180 tablet | Rail plus a single workspace column. The inspector opens as a drawer. |
| 1280×900 Electron default · 1440×900 laptop | Rail, conversation rail (collapsible), thread, inspector (collapsible). |
| 1920×1080 | Same as laptop, with the inspector open by default. |
| **5120×1440 32:9 (CRG9)** | Rail, conversation rail, thread (readable measure of about 80–100ch, not stretched), inspector, plus a **live Cluster column** (node health and the active run). Project Home fits as a side-by-side panel group without scrolling. |

---

## 6. Component inventory

Priority key:
- **P0**: core loop or security surface. Redesign first.
- **P1**: frequent use.
- **P2**: occasional use.
- **Remove**: dead or vestigial. Confirm with Dave (§9).

States to design for every component unless noted: default, hover, focus-visible, active/pressed, disabled, loading, empty, error.

### 6.1 Shell (SH)

**SH-01 · App shell / layout grid** (P0)
- Source: `body`, `.layout` (3-column CSS grid, `minmax(230px,260px) minmax(440px,1fr) minmax(270px,310px)`), `.panel`.
- Issue: at 1440×900 the thread gets about 370px of height because the footer stack (suggestions, context strip, composer, tools, status, Send) takes roughly 350px. On a phone the thread is under 30% of the screen (`09-mobile-390-chat`).
- Direction: the thread gets the space. Collapse the footer controls into one composer unit.

**SH-02 · Top bar** (P0)
- Source: `header.topbar`.
- Contents: brand (`D` mark and "DaveLLM"), runtime pill (SH-03), actions (SH-04, SH-05, SH-06).
- Issue: the runtime pill is hidden below 1024px.

**SH-03 · Runtime status pill** (P0)
- Source: `#topbarRuntime`, `#topbarStatusDot`, `#topbarNodeValue`, `#topbarStatusValue`, `#topbarModelValue`. These IDs are pinned by tests.
- Shows: a status dot (`online` / `offline` / `unknown`), node name, status word, "Ollama", model ID, and a bolt glyph.
- States: `online`, `offline`, `unknown`, no node ("No node"), no model ("No model").
- Direction: make it the node/model switcher (click to open a combined picker, RT-03) and keep it visible on mobile in compact form.

**SH-04 · Theme toggle** (P0)
- Source: `#themeToggle`, `applyTheme()`.
- Current behavior: cycles dark → light → forest, stored in `localStorage["dave_theme"]`.
- Direction: a two-state light/dark toggle, default light. Forest is an open question (§9). Decide whether to follow the OS setting before the user makes a choice.

**SH-05 · Browser credential control** (P1)
- Source: `#credentialBtn`, `requestBrowserCredential()` (uses `window.prompt`).
- Direction: open FB-01, a designed dialog. In Electron the button is hidden or disabled because the key is injected.

**SH-06 · Monitoring link and alert badge** (P1)
- Source: `.topbar-link`, `#monitorBadge` ("!"), `pollMonitoringBadge()` (every 60 seconds).
- Badge shows when any model has failures, there are recent errors, or the poll fails.
- Direction: becomes a badge on the Cluster nav item. Consider a count or severity instead of "!".

**SH-07 · Mobile tab bar** (P0)
- Source: `nav.mobile-tabs`: Chat / History / Runtime. ARIA tablist, the active tab is stored in `sessionStorage`.
- Issue: it sits at the top (violates WEB-2).
- Direction: move it to a bottom rail and use the same destinations as the desktop nav.

**SH-08 · Skip link** (P1)
- Source: `.skip-link` → `#chatPanel` (test-pinned).
- Keep it, and restyle it in the new tokens.

**SH-09 · Panel, panel header, panel subheader** (P0 primitive)
- Source: `.panel`, `.panel-header` (h2 plus an icon button), `.panel-subheader` (small caps label).
- Direction: define one panel primitive with an optional toolbar and one section-label style.

### 6.2 Conversation rail (CV)

**CV-01 · Rail header and New conversation** (P0)
- Source: `#historyHeading` "Conversations", `#newConvoBtn` "+".

**CV-02 · Global search** (P1)
- Source: `#globalSearchBox`, injected by `renderSearchBox()` with inline styles. Results in `#searchResults` as `.search-result` buttons.
- Result content: title, role, and a 120-character excerpt.
- States: idle, "Searching...", results, "No results", "Search failed".
- Issues: you have to click 🔍 because Enter does nothing, and there is no match highlighting.
- Direction: search as you type (debounced), highlight matches, ⌘K opens it from anywhere.

**CV-03 · Project picker row** (P1)
- Source: `#projectSelect` ("All Projects", one entry per project, "➕ New project…"), `#resyncProject` 🔄 ("Re-apply project instructions"), `#editProject` "Project Home".
- Issues: "New project…" chains three `window.prompt` calls (name, instructions, description). The meaning of the resync icon isn't clear.
- Direction: a project switcher plus a designed New Project sheet (FB-02).

**CV-04 · Template picker row** (P2)
- Source: `#templateSelect` (General, Code Review, Brainstorm) and `#createFromTemplate` 🚀.
- Direction: fold into the "New conversation" split button or menu.

**CV-05 · Conversation list and item** (P0)
- Source: `#convoList`, `.convo-item` (`role=button`, `.active-convo`), `renderConversationList()`. Sorted by `updated_at` descending.
- Item actions (hover-revealed emoji): ✏️ rename (also on double-click, inline input, Enter/Escape/blur), 🗑️ clear messages (confirm), ❌ delete (confirm), 📥 export Markdown.
- Data: `conversation_id`, `title`, `message_count`, `last_message` (first 50 characters), `created_at`, `updated_at`, `project_id`.
- Issues:
  - Only the title is shown, even though the data carries a preview, count, time and project.
  - Empty "New Conversation" entries pile up (see `07-ultrawide`).
  - The action glyphs look alike and are ambiguous.
- Direction: two-line items (title plus last-message preview, relative time, project chip), an overflow menu for actions, date grouping (Today, Yesterday, This week), and a design for rename-in-place.

**CV-06 · Router Stats box** (**Remove**)
- Source: `#statsContainer` (Decisions, Total tokens, Avg tokens/msg, Avg confidence, Last route).
- `state.stats.decisions` is never filled, so Decisions, confidence and last route always read 0 or "–". Token numbers are client-side estimates.
- Direction: remove it. If usage is wanted, put real numbers in Cluster → Usage (MO-04).

**CV-07 · Relevant memories** (P2)
- Source: `#memoryBox`, `showRelevantMemories()`. Header reads "💾 Relevant memories loaded (used for routing/context)".
- Each item: role (👤 You / 🤖 Assistant), similarity %, and a 60-character preview. The box is hidden when there are no memories.
- Direction: move it into the Chat inspector ("Context used for this reply").

### 6.3 Chat thread and composer (CH)

**CH-01 · Chat header** (P0)
- Source: `#chatHeading` "Chat", `#instructionsBtn`, `#notepadToggle` (`aria-expanded`).
- Issue: the header never shows the **conversation title** or its project.
- Direction: show the title (editable), a project chip, the Instructions entry point (IN-*), a Notepad toggle (CH-09) and a "…" overflow for export, clear and delete.

**CH-02 · Empty chat state** (P1)
- Source: `.empty-chat`, `renderEmptyChatState()`.
- Content: heading "What would you like to do?" or "Start this conversation", explainer text, and actions: Continue ‹last›, Start General, Code Review, Brainstorm, Start in ‹project›.
- Issue: it shows duplicate primary actions ("Continue New Conversation" next to "Start General").
- Direction: one primary action, then template cards and a list of recent conversations.

**CH-03 · Long-thread nudge** (P2)
- Source: `.info-banner`, shown when there are more than 12 messages ("💡 Conversation getting long…").
- Direction: a dismissible inline notice with a "Start fresh with summary" action (design only; this is a backend dependency).

**CH-04 · Message** (P0)
- Source: `.message.message-{user|assistant|system|error}`, `renderMessages()`.
- Anatomy: header with an uppercase role label ("USER:" / "ASSISTANT:"), actions, and content.
- Actions:
  - **Copy** (becomes "Copied" for 2 seconds; copies raw Markdown)
  - **Add to notepad** (appends the selected text)
  - 👍 / 👎 feedback on assistant messages
  - All actions are revealed on hover and always visible on touch.
- Content types: plain text, fenced code (CH-05), images (CH-06), multimodal text and image parts, and the streaming caret "▌".
- Data available: `role`, `content`, `timestamp` (user messages).
- **Not available today**: model, node, token count and latency for each assistant message. The backend doesn't store them, so a per-message model badge is a **backend dependency**.
- **Coming soon (DL-UX-01)**: the native Ollama transport (`davellm_ollama.py`, merged in a927755) already parses the model's `message.thinking` text and the done-line metrics (token counts and durations, so tokens/sec). Nothing forwards them to the UI yet. Design slots for a collapsible **"Thinking" disclosure** and a **per-reply metrics footer** (tokens, tok/s, time to first token).
- Issues:
  - Markdown isn't rendered: lists, headings, bold and links show as raw text.
  - Assistant bubbles are capped narrow even on wide screens.
  - The role labels are shouty.
- Direction:
  - Assistant messages as full-width prose with a readable measure. User messages as compact bubbles.
  - Show the timestamp on hover.
  - Design feedback states (voted, undo).
  - Design a Markdown style set: headings, lists, tables, blockquote, inline code, links.

**CH-05 · Code block** (P0)
- Source: `pre > code`, produced by splitting content on ```.
- Issues: the language tag leaks as the first line (see "python" in `02-desktop`). There is no copy button, no language label, no wrap toggle and no highlighting. Blocks overflow horizontally on mobile.
- Direction: a header bar (language, Copy, wrap toggle), horizontal scroll inside the block, and optional syntax colors built from the tokens.

**CH-06 · Image in message** (P2)
- Source: inline `img`, max 220×220.
- Direction: a thumbnail that opens a lightbox, with a caption for the model's vision support.

**CH-07 · Error message** (P0)
- Source: a message with `role: "error"` and content "❌ Error: ‹detail›".
- Direction: an error card with the cause (node unreachable, model error, timeout) and actions (Retry, Switch node or model).

**CH-08 · Jump to latest** (P1)
- Source: `#scrollBottomBtn` ⬇️. Shown when the user is more than 40px from the bottom. Auto-scroll pauses while the user is scrolled up.
- Direction: a floating pill ("↓ New messages") that shows a count while streaming.

**CH-09 · Project notepad drawer** (P1)
- Source: `#notepadPanel` (test-pinned). Contents: heading, project name, `#notepadInput` (plain text, autosave debounce), `#notepadStatus`, "Send to chat", Close.
- Status copy: "Not loaded", "Loading...", "Unsaved changes", "Saving...", "Saved", "Load failed: …", "Save failed: …".
- Rule: plain text only. No rich text, history or collaboration.
- Direction: a side drawer or inspector tab. Shown disabled when no project is selected.

**CH-10 · Tools box** (**Remove**)
- Source: `#toolsBox` has styles but no JavaScript uses it.

**CH-11 · Tool run ledger** (P0, security surface)
- Source: `#runLedger`, `renderToolRun()`, `watchToolRun()` (SSE).
- Contents:
  - Title "Tool run" with the status, which is one of the live states `created`, `running`, `approval_required`, `cancelling`, or the terminal states `completed`, `model_timeout`, `model_error`, `error_budget`, `step_limit`, `budget_exceeded`, `cancelled`, `cancellation_failed`, `approval_rejected`, `run_conflict`, `run_expired`
  - A reason line: "Run ‹id›… · N model steps" or "Finished: ‹reason›"
  - An ordered event list: sequence, kind, tool, status
  - A partial transcript (last 8 assistant or tool entries, each up to 2,000 characters)
  - **Stop run**, which is disabled at terminal states or while awaiting approval ("Reject the pending call to stop this run")
  - **Reconnect**, shown after 3 stream failures ("Event connection lost…")
- Entry point: CH-18 "Run tools". It only appears when `GET /tools` succeeds, meaning tools are enabled on the server.
- Direction: a structured run card in the thread, with a timeline of steps, a tool icon for each step, expandable results and live progress. On wide screens the same run can be pinned in the inspector.

**CH-12 · Approval card** (P0, security surface)
- Source: `#runLedgerApproval`, `approvalPreview()`.
- Contents:
  - heading "Approval required" (receives focus)
  - summary `‹tool› · permission: ‹perm› · call: ‹id›`
  - for `file.edit`: a preview with "Edit ‹path› · replaces N occurrence(s)", a **Before** block, an **After** block, and a "Nothing: the text above is deleted." note when the replacement is empty
  - **Approve once** (primary) and **Reject** (secondary)
  - "Exact arguments" as a `<details>` containing JSON
- Rules: text nodes only, pinned by `tests/test_approval_preview.mjs`. Buttons disable while the decision is being sent. Errors show on the reason line.
- Direction:
  - Show risk by permission class (read vs write vs execute).
  - Show a monospace diff with line context.
  - Put the path in a breadcrumb.
  - Keep "exact arguments" one click away and never truncated.
  - Design pending, sending, approved, rejected and expired states. The approval nonce expires after 300 seconds, so add a countdown.

**CH-13 · Suggested next** (P1)
- Source: `#suggestedNext`, `#suggestionChips`, `renderPredictions()`, `static/anticipation.js`.
- Chip types and labels:
  - continue: "Continue: ‹title›"
  - prefill: "Review attached code", "Review attached file", "Describe attached image", "Review attached audio", "Add tests", "Retry concise", "Summarize", "Turn into checklist"
  - select model: "Use vision model"
  - select template: "Use Code Review", "Use Brainstorm"
  - "Remember Support"
- Each chip has a dismiss "×". Applying a chip shows Undo (CH-14). Nothing is ever sent automatically.
- Direction: a lightweight chip row that fits in one line above the composer and scrolls horizontally on mobile.

**CH-14 · Context strip** (P0)
- Source: `#contextStrip`. Status text is one of "Manual selection", "Using last selection", "Stored selection unavailable; using safe defaults", or "‹label› applied · Undo available".
- Buttons: Project, Template, Node, Model. Each one jumps focus to its control. "Last used" marker, `#undoPredictionBtn` Undo, `#resetSuggestionsBtn` "Reset suggestions".
- Issues: it duplicates the top-bar pill and gets clipped on mobile (`09-mobile-390-chat`).
- Direction: merge with SH-03 into one "session context" control near the composer (project · template · node · model), with popover pickers.

**CH-15 · Composer** (P0)
- Source: `#promptInput`. Auto-sizing textarea. Enter sends, Shift+Enter adds a newline. The draft is kept in `sessionStorage` (`davellm_draft_session`).
- Direction: one composer card holding the input, the attachment tray, the context control (CH-14) and Send/Stop.

**CH-16 · Attachment toolbar** (P1)
- Source: `#attachmentTools`.
- Buttons: **Image** (`image/*`), **Audio** (`audio/*`), **Transcribe** (audio to text), **Dictate** (mic; Web Speech API with a MediaRecorder fallback that uploads to `/audio/transcribe`), **File** (inlined as text, max 1 MB), and a **Support** checkbox (prefixes `[SUPPORT]`).
- On mobile the toolbar collapses behind `#attachToolsToggle` "Attach".
- Lucide IDs pinned by tests: `image`, `file-audio`, `captions`, `mic`, `paperclip`.
- Direction:
  - a single "+" attach menu
  - attached items as removable chips with thumbnails
  - a recording state for dictation (live level indicator, Stop)
  - a vision-mismatch warning inline, replacing the `confirm()`

**CH-17 · Attachment status line** (P1)
- Source: `.tool-status` → `#imageStatus`, `#audioStatus`, `#fileStatus`, `#dictateStatus`.
- Issue: "Model is text-only" shows even when no model is loaded.
- Direction: fold these statuses into the attachment chips.

**CH-18 · Primary actions** (P0)
- Source: `#sendBtn` "Send", `#runToolsBtn` "Run tools" (hidden unless tools are enabled), `#attachToolsToggle`.
- Issue: while streaming, Send already works as **Stop**, but the label and icon never change.
- Direction: design Send, Stop (streaming), Sending (disabled), and "Run tools" as a secondary or split action.

### 6.4 Runtime and cluster (RT, becomes the Cluster tab)

**RT-01 · Telemetry header** (P1)
- Source: `#runtimeHeading` "Telemetry", `#refreshNodes`.

**RT-02 · Node card** (P0)
- Source: `.node-card` (plus `.active` for the selected node), built in `fetchNodeStatus()`.
- Contents: status dot, name, "Online"/"Offline", **raw URL**, and "⚡ ‹n›ms latency" when online.
- States: online, offline, unknown (status call failed: name and URL only), none ("No nodes registered yet."), backend down (red box "⚠️ Connection failed: Is the backend running at ‹origin›?").
- Data: `/nodes` → `{id, name, url}`; `/nodes/status` → `{node_id, name, status, latency, error}`.
- Direction:
  - Name, status, latency sparkline (design slot; history is not exposed yet, so this is a **backend dependency**).
  - Loaded model and tokens/sec are also design slots and **backend dependencies**.
  - Hide the address behind "Details".
  - Click to select as the active node.

**RT-03 · Node and model selectors** (P0)
- Source: `#targetNodeSelect`, `#loadModelsBtn` 📦, `#modelSelect`.
- Model options come from `/nodes/{id}/models`. A 👁️ suffix marks vision models.
- Placeholder and error options: "Loading models...", "Node unreachable", "No models pulled on node", "Error loading models", "No nodes available".
- Rule: a chat must name a node from `/nodes` and a model from that node's inventory.
- Direction: a combined picker (node list → models on that node) with search, vision and size badges, and an offline state inline. Reachable from SH-03, CH-14 and the Cluster tab.

**RT-04 · Routing inputs** (**Remove**, or wire up later)
- Source: `#routeCost` "Max cost ($/1k tokens)", `#routeQuality` "Min quality (0.7–1.0)", `#routeStatus`.
- The values are saved to `localStorage` but **never sent** with a request. `#routeStatus` only echoes "Using manual selection: ‹model›".

**RT-05 · Model fetcher** (P2)
- Source: `#modelFetcher`: `#hfUrlInput` (Hugging Face file URL; only `huggingface.co`/`hf.co` are allowed), `#hfDestInput` (optional path inside `models/`), `#hfDownloadBtn`, `#hfStatus`.
- Status copy: "Enter a Hugging Face URL.", "Starting download…", "Saved to ‹path› (N MB)", "Download failed: …".
- Direction: Cluster → Models → "Fetch model", with progress (design slot; the current API returns only on completion).

### 6.5 Project Homepage (PH, `<dialog>` today, proposed full Projects tab)

All IDs in this section are test-pinned (see Appendix A). GSAP entrance timeline: an ambient glow, the header, the toolbar and budget panel, staggered component cards with a number pop, and budget segments. A reduced-motion variant exists.

**PH-01 · Header** (P1)
- Source: kicker "Project Homepage", `#projectHomeHeading` (project name), `#projectHomeDescription`, Close. Decorative `.project-home-ambient`.

**PH-02 · Chat attachment toolbar** (P1)
- Source: `#projectAttachmentState`.
- Copy: "No active chat…", "“‹title›” is attached. Project context applies to future messages.", "…is attached to ‹other project›.", "…is a General chat with no project."
- Buttons: `#attachProjectChat` (Attach/Detach active chat) and `#startProjectChat`.

**PH-03 · Context budget** (P1)
- Source: `#projectContextBudget` (1,024–262,144, step 1,024) and `#contextBudgetMeter`.
- The meter has 4 fixed-ratio segments: Instructions 25%, BRAIN 25%, Files 30%, Artifacts 20%. Each shows "used / quota".
- Direction: a stacked bar that shows fill within each segment. Add over-quota states.

**PH-04 · 01 Project Instructions** (P1)
- Source: `#projectHomeInstructions`, `#projectHomeInstructionsCount` ("N of M tokens", `.over-budget` state), `#saveProjectInstructions`.

**PH-05 · 02 File Context Uploads** (P1)
- Source: `#projectFileForm` (file label plus "Upload reference"), `#projectFilesStatus`, `#projectFilesList`.
- Each record: name, attached/detached, index status, size, token count. Actions: Reindex, Attach/Detach, Delete (confirm).
- Empty copy: "No references yet. Upload UTF-8 text or code…".
- Direction: a drop zone, rows showing indexing status (indexed, unindexed, failed), and attach toggles.

**PH-06 · 03 Artifact History** (P1)
- Source: `#projectArtifactsList` and `#projectArtifactPreview` (`<pre>`).
- Each record: "Pinned · " prefix, title, kind, tokens. Actions: Open, Pin/Unpin, Archive, Delete (confirm).
- Direction: a searchable list with a side-by-side preview on wide screens.

**PH-07 · 04 BRAIN** (P1)
- Source: three tiers:
  - `#brainPinned` "Pinned facts and decisions: Preserved verbatim"
  - `#brainActive` "Active goals, contracts, and risks: Protected structured context"
  - `#brainRecent` "Recent working context: Compacted at the threshold"
- Controls: `#brainThreshold` "Compact at N tokens", `#brainRevision` "Revision N", `#brainRevisionSelect`, and buttons Delete (soft delete, confirm), Restore revision (confirm), Compact now, Save BRAIN. Status line `#projectBrainStatus`.
- Direction: show that the tiers differ in durability. Add a revision timeline or history and a view comparing a compaction with its original.

**PH-08 · Next-request context preview** (P2)
- Source: a `<details>` element, "Inspect next request context", `#previewProjectContext`, `#projectContextPreviewOutput`.
- Status copy: "Preview assembled N messages. No model request was sent."
- Direction: an inspector view listing the assembled messages with a source label and token count for each.

**PH-09 · Dialog status footer** (P2)
- Source: `#projectHomeStatus`. Copy: "Loading project context...", "All four project components are loaded.", "Load failed: …".

### 6.6 Session instructions (IN, `<dialog>`, test-pinned IDs)

**IN-01 · Header** (P1)
- "Session instructions". Explainer: "Precedence runs from global to project to session. Later layers win conflicts." Close button.

**IN-02 · Instruction layers** (P1)
- Three layers, each a textarea with a count ("N characters, about M tokens"):
  - `#globalInstructions` "1. Global default"
  - `#projectInstructions` "2. Project instructions" with the project name or "No project attached"
  - `#sessionInstructions` "3. Session override"

**IN-03 · Effective instructions** (P1)
- Source: `#effectiveInstructions` (read-only) "Exact instructions in effect".
- Direction: a stacked view that shows each layer's contribution, then the merged result.

**IN-04 · Actions and status** (P1)
- Buttons: Reset global default, Revert session override, Save and apply. Status `#instructionsStatus`.

### 6.7 Monitoring (MO, separate `monitoring.html`, proposed Cluster tab)

The page has its own palette, no theme toggle, no nav except "← Back to main", and renders `<pre>` error blocks (`10-desktop-monitoring`).

| ID | Card | Data |
|---|---|---|
| MO-01 | Header, Refresh, spinner, status ("Refreshing...", "Updated.", "Error: …") | n/a |
| MO-02 | Nodes | `name (node_id)`, status, latency |
| MO-03 | Model Health | per model: `failures`, `last_error`. Empty: "No failures recorded." |
| MO-04 | Cost Analytics | `total_cost`, `by_model{}`, `by_conversation{}` (dollar estimates) |
| MO-05 | Recent Errors | last 10 of `{timestamp, event, detail}`. Empty: "No recent errors." |

### 6.8 Feedback layer (FB, mostly new)

These replace browser-native dialogs, which can't be themed, block the page, and look broken in Electron.

| ID | Replaces | Current copy / trigger |
|---|---|---|
| FB-01 Credential dialog | `window.prompt` (app.js and monitoring.html) | "Enter the DaveLLM API key for this browser session:" (masked field, session-only note) |
| FB-02 New project sheet | 3 chained `prompt()` | Name, optional system instructions, optional description |
| FB-03 Confirm dialog (destructive) | 6× `confirm()` | Delete conversation, clear messages, delete reference, delete artifact, restore BRAIN revision, soft-delete BRAIN |
| FB-04 Confirm dialog (send-time warning) | 2× `confirm()` | "Selected model is marked text-only. Send anyway?" and "Total cost is $X. Continue?" (shown when cost exceeds $10). Better as an inline warning above the composer. |
| FB-05 Toast / inline notice | 17× `alert()` | For example "Please select a node first", "Attached file is too large (max 1MB…)", "Select a node and one of that node's loaded models before sending.", "Export failed: …", "Select text inside this message, then choose Add to notepad." |
| FB-06 Backend-offline state | Red box in node list | Full-shell banner or empty state when the router is unreachable, with Retry |
| FB-07 Loading / skeletons | Text "Loading…" | Conversation list, thread, node cards, Project Home |
| FB-08 Keyboard shortcut sheet | n/a (new) | ⌘K search, ⌘N new chat, ⌘Enter send, Esc stop, ⌘/ shortcuts |

---

## 7. Primitives and design-system layer (build these first)

| Primitive | Current variants in code | Needed |
|---|---|---|
| Button | `.primary` (gradient), `.secondary`, `.text-btn`, `.icon-btn`, `.danger-text`, `.action-icon` (emoji), `.message-action`, `.composer-tool-btn`, `.image-upload-btn` (a label styled as a button) | primary, secondary, ghost, danger, icon-only (with tooltip), split, toggle. Sizes sm/md, plus a 44px touch size. |
| Chip | `.suggestion-chip` + `.suggestion-dismiss`, `.context-item`, `.context-marker` | action chip (dismissible), filter chip, status chip, attachment chip |
| Text input / textarea / number / select | Native, themed per theme | Field with label, hint, count, error. Search field. Combobox for node/model. |
| Checkbox / toggle | `#supportFlag` | checkbox, switch |
| Status dot / badge | `.status-dot`, `.topbar-status-dot.status-*`, `.badge` "!" | online / offline / unknown / busy dots, count badge, severity badge |
| Status line | `role=status` spans everywhere | inline status with idle, working, success and error tones |
| Panel / card / section label | `.panel`, `.project-component`, `.stats-box`, `.fetcher-box`, `.panel-subheader`, `.project-home-kicker`, `.component-number` | panel, card, section header, kicker |
| Record row | `.project-record` (title, meta, action buttons) | list row with meta and an overflow menu |
| Empty state | `.project-empty-state`, `.info-message`, `.empty-chat` | one empty-state pattern with an optional action |
| Disclosure | `<details>` (context preview, exact arguments) | an accordion that stays `<details>`-compatible |
| Code / pre | `pre > code`, `.edit-preview-before/after`, `.project-artifact-preview` | code block, diff block, log block |
| Meter | `.budget-segment` | segmented stacked bar with used/quota |
| Dialog / drawer / sheet | `<dialog>` ×2, notepad drawer | modal, side drawer, bottom sheet (mobile) |
| Tabs / nav | `.mobile-tabs` | side rail, top tabs, bottom rail |
| Icon | Mixed (see §4) | Lucide only, vendored sprite. One stroke width. 16/20/24px sizes. |

---

## 8. Sample data for mocks

- **Nodes**: max (online, 18 ms), walter (online, 9 ms, fastest GPU), duncan (online, 31 ms), dominic (offline).
- **Models**: `llama3:8b`, `gpt-oss:20b`, `gpt-oss:120b` (duncan only), `qwen3-coder:30b`, `llava:13b` (vision).
- **Conversations**: "Router retry backoff" (Code Review, 4 messages), "Stage wash cue ideas" (Brainstorm), "Ollama context length notes", "HA routine audit".
- **Project**: "DaveLLM Router". Instructions: "Prefer small, reviewable diffs. Cite file:line." 2 references (`app.py` indexed, 41,200 tokens; `REPORT.md` indexed), 3 artifacts, BRAIN revision 7.
- **Tool run**: `file.search` (read, completed), then `file.edit` awaiting approval on `static/app.js`, replacing 1 occurrence, with 290 seconds left on the nonce.
- **Errors**: `2026-09-26T03:41Z — node_error: walter timed out after 120 s`.

---

## 9. Open decisions for Dave (answer before or during Claude Design)

1. **Forest theme**. WEB-1 needs light (default) and dark. Options: drop forest, or keep it as a third selectable theme on top of the light default. *Recommendation: ship light and dark; keep forest only as an accent variant if it's cheap.*
2. **Project Homepage**: stay a modal or become a full **Projects** tab? *Recommendation: a tab (WEB-2 subject areas).*
3. **monitoring.html**: fold into a **Cluster** tab? *Recommendation: yes. Retire the separate page, or keep it as a redirect.*
4. **Router Stats (CV-06) and routing inputs (RT-04)** are dead or vestigial. Remove, or wire them to real data? *Recommendation: remove now and reintroduce with real `/analytics` data.*
5. **Per-message metadata** (model, node, latency, tokens) needs a backend change to persist it. Design for it now as optional?
6. **Visual direction**: keep the aqua-glass HUD identity or reset to a quieter console look? The current glow and grid effects cost contrast in light mode and add noise at high density.

---

## 10. Build order in Claude Design

1. **Foundations**: color tokens (light and dark), type scale with a vendorable font, spacing, radius, elevation, focus ring, motion (with reduced-motion variants), Lucide icon sheet.
2. **Primitives** (§7): every state, both themes.
3. **App shell and navigation** at 390, 820, 1280, 1440, 1920 and 5120 widths (SH-*).
4. **Chat core** (P0): CH-04, CH-05, CH-07, CH-14, CH-15, CH-16, CH-18, CH-13, CH-01, CH-02.
5. **Tool run and approval** (P0): CH-11 and CH-12 in every state.
6. **Conversation rail** (CV-01, CV-02, CV-03, CV-05).
7. **Cluster tab**: RT-02, RT-03, RT-05 and MO-01 to MO-05, merged.
8. **Projects tab**: PH-* plus IN-* and CH-09.
9. **Feedback layer** (FB-*) and **Settings**.
10. **Ultrawide pass** at 5120×1440 with all tabs populated.

**What to hand back for implementation:**
- a token sheet as CSS custom properties
- component specs keyed by the IDs in this file
- redlines for the 6 breakpoints
- motion specs as GSAP-timeline-friendly durations and eases

---

## Appendix A: Contracts pinned by tests

Renaming or removing any of these means changing the tests in the same change:
- `tests/test_security_and_frontend.py`
- `tests/test_approval_preview.mjs`
- `tests/test_run_ledger_watch.mjs`
- `tests/test_anticipation.py`

- **Element IDs**: `topbarStatusDot`, `topbarNodeValue`, `topbarStatusValue`, `topbarModelValue`, `chatPanel` (with `tabindex="-1"`), `instructionsDialog`, `globalInstructions`, `projectInstructions`, `sessionInstructions`, `effectiveInstructions`, `notepadPanel`, `notepadInput`, `projectHomeDialog`, `projectHomeInstructions`, `projectFilesList`, `projectArtifactsList`, `brainPinned`, `brainActive`, `brainRecent`, `previewProjectContext`, `projectContextPreviewOutput`.
- **Markup**:
  - `class="topbar-status-dot status-unknown"` as the initial state
  - `class="skip-link" href="#chatPanel"`
  - `aria-controls="notepadPanel"`
  - `<script src="vendor/gsap/gsap.min.js">`
  - Lucide refs `#image`, `#file-audio`, `#captions`, `#mic`, `#paperclip`
- **Banned glyphs** in `index.html` and `app.js`: 📷 🎤 🗣️ ✍️ 🎙️ ⏹️ 📎. The test suite already pushes toward icons instead of emoji.
- **Copy**: "Copy" → "Copied" (resets after 2,000 ms), "Add to notepad".
- **CSS**:
  - `@media (prefers-reduced-motion: reduce)`
  - `@media (hover: none), (pointer: coarse)`
  - `.message:hover .message-actions`
  - The scroll contract: `html {height:100%}`; `body` `height:100dvh; overflow:hidden`; `.layout` `min-height:0; overflow:hidden`; `.panel`, `.response` and `.instruction-layer textarea` all `overflow-y:auto`
  - The mobile panel height `calc(100dvh - 118px)`
  - Landscape mobile `body {overflow:auto}`
  - A new shell will change these, so update the scroll-contract test on purpose and don't delete it.
- **Security**:
  - no `innerHTML` in `app.js` or `monitoring.html`
  - no persistent browser copy of conversations or feedback
  - no API key in `localStorage` or the preload script

## Appendix B: Screenshot index (`current-ui/`)

| File | Shows |
|---|---|
| `01-desktop-1440-dark-empty.png` | Default dark theme, empty chat state, both nodes offline |
| `02-desktop-1440-dark-chat.png` | Code-review conversation: squeezed thread, leaking code language tag, full footer stack |
| `03-desktop-1440-light-chat.png` / `03-desktop-1440-forest-chat.png` | The other two themes |
| `04-desktop-instructions-dialog.png` | Session instructions dialog (IN-*) |
| `05-desktop-project-home.png` | Project Homepage dialog (PH-*) |
| `06-desktop-notepad-open.png` | Notepad drawer open over the thread (CH-09) |
| `07-ultrawide-2560x720-dark-chat.png` | 32:9 at half scale: stretched center, capped side columns, piled-up "New Conversation" entries |
| `08-tablet-820-chat.png` | Tablet: top tabs, single panel |
| `09-mobile-390-{chat,history,runtime}.png` | Phone: top tab bar, thread under 30% of the screen, clipped context strip, code overflow |
| `10-desktop-monitoring.png` | Separate monitoring page with its own palette |
