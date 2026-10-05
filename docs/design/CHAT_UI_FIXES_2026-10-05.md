# Chat and sidebar fixes, 2026-10-05

Scope: the chat pane, the conversation sidebar and the navigation rail in `static/`, plus link handling in `desktop/main.js`. No router, persistence or tool changes.

## Bugs fixed

| ID | Problem | Cause | Fix | Acceptance check |
|----|---------|-------|-----|------------------|
| UI-01 | A rename reverts when the chat is opened | The rename skipped the router save when the chat's messages were not loaded yet, and opening the chat reloads its title from the router | Always save; on failure restore the old title and say so; settle once (Enter then blur, or Escape then blur) | Rename a chat that has not been opened since reload, open it: the new title stays and `GET /conversations` returns it |
| UI-02 | The Copy button looks gone | It is muted 12px text under the stats line and appears only when the reply finishes | Bordered button with a copy icon and readable label | Each finished user and assistant message shows a visible Copy button; clicking it shows "Copied" |
| UI-03 | Markdown is dropped | The renderer handled only code fences, bold, inline code and whole-paragraph headings; paragraphs had no spacing | DOM-only renderer for headings (also mid-paragraph), ordered, unordered and nested lists, tables with alignment, quotes, rules, italics, strikethrough, escapes, links and bare URLs; paragraph spacing | `tests/test_markdown_render.mjs`; model HTML stays text; only `http(s)` and `mailto` links |
| UI-04 | The reply drags the view down while you read | Every token rebuilt the whole chat and scrolled to the bottom unless the reader was already more than 40px up; a trackpad scroll could not escape before the next token | Tokens redraw only the streaming bubble; any upward wheel, touch, key or scrollbar movement stops following; scrolling back to the bottom or the jump button resumes | Scroll up 30px during a reply: the view stays while the reply grows below it |
| UI-05 | Sending during a reply starts a second stream | No guard; the second stream replaced the first one's Stop handle | One reply at a time: sending is refused with a notice and the draft kept; the Send button reads Stop while a reply streams | Press Enter mid-reply: nothing is sent, the draft stays, the button reads Stop |
| UI-06 | No sign the model is still working | The cursor's `blink` animation did not exist and the status line disappeared at the first token | Blinking cursor at the end of the text, animated dots with elapsed seconds under the reply, Stop button, "Writing…" on the chat in the sidebar | During a reply the bubble shows "Writing · N s" and the sidebar row shows Writing… |
| UI-07 | A finished reply reloads the wrong chat | The done handler used the open chat, not the chat that sent | The handler uses the sending chat's ID | Switch chats mid-reply: the replying chat gets its title; the open chat is unchanged |
| UI-08 | Clicking a chat can open the wrong one | Hovering added a row of action buttons, which pushed every row below it down under the pointer; fast clicks could finish out of order | Actions float over the row without changing its height; the latest click wins | Item positions are identical with and without hover |
| UI-09 | Rail labels overflow their box | "PROJECTS" measured 61px in a 58px button | Narrower padding and letter spacing | Every rail button's text width fits its box at desktop and phone widths |

## Sidebar features (built with defaults)

Problem: with many chats, Dave cannot see which replies he has not read, cannot reorder the list, and the two-line rows show few chats at once.

Flow and smallest useful slice:

- **Unread dot.** A reply that finishes while its chat is not open, the window is unfocused, or another view is showing marks the chat unread: an accent dot and bold title, also announced in the row's label. Opening the chat, or returning to the window on it, clears the mark.
- **Sort.** Recent (default, grouped by age), Oldest (grouped), Created, Title A–Z, and Unread first.
- **View.** Detailed (title, preview, age) or Compact (titles only, tighter rows).

Data effects: the choices and unread IDs live in this browser's `localStorage` (`dave_convo_sort`, `dave_convo_density`, `dave_unread_conversations`). Unread holds conversation IDs only. No router or API change.

Exclusions: desktop notifications, a server-side read state, search filters and manual pinning.

Risks: unread marks do not follow Dave between the Electron app and a browser; a cleared browser storage forgets them.

## Open choices

- **DL-UI-01 Unread scope.** A per-device marks in browser storage (built, recommended for now); B a router `read_at` per conversation so marks follow Dave across the app and browsers. B unlocks cross-device unread and notifications from other clients.
- **DL-UI-02 Desktop notifications.** A notify only when a reply finishes while the window is hidden or unfocused, with a Settings toggle on by default (recommended); B always notify; C no notifications. A needs the macOS notification permission prompt once.
- **DL-UI-03 Sending during a reply.** A refuse and keep the draft (built, recommended); B stop the current reply and send the new message; C queue the new message until the reply ends.
- **DL-UI-04 Model capabilities band.** A show vision, audio, tools, context size and quantization chips per model in the composer, read from Ollama's model details (recommended); B vision only, as today. A needs a small router change to pass `/api/show` capabilities through `/nodes/{id}/models`.

## Verification

Local test router on port 8765 with a stubbed Ollama node and scratch data (`DAVE_DATA_DIR`), driven in a real browser at 1440×900 and 375×812, light and dark: renamed unopened chat persisted; tables, lists, quotes and links rendered; a 30px scroll-up held during a reply; a second send was refused with the draft kept; switching chats mid-reply set unread on the right chat; hover left row positions unchanged; page scroll size equaled the viewport at both sizes. Electron link handling and the live cluster are not exercised by this check.
