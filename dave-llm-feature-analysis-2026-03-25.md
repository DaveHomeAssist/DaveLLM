# DaveLLM — Feature Analysis

**Date:** 2026-03-25
**Project:** dave-llm
**Stack:** FastAPI backend (app.py) + vanilla JS frontend (app.js + style.css + index.html) + Electron desktop shell, multi-node Ollama LAN cluster

---

## Summary Table

| Feature | Status | Data Source / Persistence | Critical Gap |
|---|---|---|---|
| Multi-node LLM routing | Complete | FastAPI backend, node health checks | None |
| Streaming chat with abort control | Complete | Server-Sent Events, AbortController | None |
| Persistent conversations (backend-synced) | Complete | JSON file on disk (atomic writes), localStorage fallback | No database — JSON file doesn't scale |
| Three-column desktop UI | Complete | Vanilla JS DOM, CSS flex layout | None |
| Three-theme system (dark/light/forest) | Complete | CSS custom properties, localStorage | None |
| Model selection and vision detection | Complete | Ollama API, model metadata normalization | None |
| Conversation management (rename, delete, clear, export) | Complete | Backend API + localStorage sync | None |
| Project system with system prompts | Complete | JSON file persistence, API CRUD | None |
| Router stats tracking | Complete | localStorage, in-memory decisions array | Stats are local to the frontend, not backend |
| Image/audio/file attachment | Complete | FormData upload to backend | None |
| Dictation (speech-to-text) | Complete | Web Speech API + MediaRecorder fallback + whisper.cpp | Requires whisper.cpp binary on backend |
| Audio transcription | Complete | whisper.cpp via subprocess | None |
| Model fetcher (HuggingFace download) | Complete | Backend download endpoint | None |
| Monitoring dashboard | Complete | Separate monitoring.html page | Limited — only shows node health, costs, errors |
| Template conversations | Complete | Predefined templates (general, code review, brainstorm) | None |
| Feedback system | Complete | Backend endpoint + localStorage | None |
| Routing preferences (cost/quality) | Complete | localStorage, sent with requests | None |
| Memory system | Complete | Vector DB (SQLite), memory panel in sidebar | None |
| Conversation export (markdown) | Complete | Backend `/conversations/:id/export` endpoint | None |

---

## Detailed Feature Analysis

### 1. Multi-Node LLM Routing

**Problem it solves:** Distributes inference across 4 LAN machines with different GPU capabilities, selecting the right node/model for each task.

**Implementation:** The FastAPI backend (`app.py`) maintains a node list configurable via `DAVE_NODES` env var or `DEFAULT_NODES`. Each node runs Ollama on port 11434. The router checks node health, tracks failures (`MODEL_HEALTH` dict, `track_model_failure()` at line ~83), and records errors (`RECENT_ERRORS` list with 50-entry cap). A model catalog (`MODEL_CATALOG`, line ~64-78) maps model paths to metadata including vision capability, cost per 1k tokens, and quality score. The frontend sends routing preferences (max cost, min quality) with each request.

**Tradeoffs:** No cloud fallback when all local nodes are down. Node selection is based on a simple catalog, not dynamic load balancing. The system prompt is long (~200 lines) and sent with every message, consuming context window.

---

### 2. Streaming Chat with Abort Control

**Problem it solves:** Provides real-time token-by-token response display and lets users stop long generations.

**Implementation:** The frontend uses `fetch()` with `AbortController` (state.abortController) to consume Server-Sent Events from the backend streaming endpoint. The `state.streaming` flag tracks active streams. The response box (`#responseBox`) uses `white-space: pre-wrap` monospace rendering. Auto-scroll is managed by `updateScrollBottomButton()` (line ~135-144) which checks scroll position and shows/hides a sticky scroll-to-bottom button.

**Tradeoffs:** SSE is one-directional — the client can only abort, not send mid-stream signals. The monospace response rendering works for code but is less readable for prose.

---

### 3. Persistent Conversations with Backend Sync

**Problem it solves:** Preserves chat history across sessions with a backend as the source of truth and localStorage as a fallback.

**Implementation:** `loadConversationsFromBackend()` (app.js line ~579-608) fetches all conversations from `/conversations`, populating `state.conversations`. Messages are loaded on-demand via `loadConversationHistory()` (line ~614-637). Rename syncs via `syncRenameToBackend()` (line ~642-658). Delete and clear have corresponding backend calls. `saveAllConversations()` writes to localStorage as a fallback. The backend uses atomic file writes — write to `.tmp` then `os.rename()` (app.py line ~259-267).

**Tradeoffs:** JSON file persistence is simple but doesn't scale to many conversations or concurrent users. The atomic write prevents corruption but there's no backup or versioning. Messages are loaded on-demand (lazy loading) which is good for large conversation lists but adds latency on selection.

---

### 4. Three-Column Desktop UI

**Problem it solves:** Provides simultaneous access to conversations list, chat, and node/model controls without switching views.

**Implementation:** A flex layout (`.layout` in style.css line ~229-237) with three panels: conversations (260px fixed, sticky), chat (flex 2), and nodes (flex 1, sticky). The topbar is sticky with a gradient brand name. At 1024px, the layout switches to a 2-column CSS grid with convos/nodes on top and chat spanning full width below. At 768px, the actions column stacks and textarea gets larger min-height.

**Tradeoffs:** On screens below 1024px the three-column benefit is lost. The sticky sidebars use `max-height: calc(100vh - 120px)` with overflow scroll, which can feel cramped with many conversations.

---

### 5. Model Selection and Vision Detection

**Problem it solves:** Lets users pick models and automatically detects whether a model supports image input.

**Implementation:** `normalizeModelMeta()` (app.js line ~217-247) extracts model ID and vision capability from various API response formats. It checks model name for keywords ("vision", "multimodal", "mm"), capabilities objects, modality arrays, and explicit flags. `modelSupportsVision()` (line ~254-258) uses cached metadata or falls back to name-based detection. The UI shows "Model supports images" or "Model is text-only" in the image status area.

**Tradeoffs:** Vision detection by name pattern is heuristic — a model named "panoramic-vision-demo" would incorrectly flag as vision-capable. The metadata normalization handles multiple API formats which adds complexity.

---

### 6. Dictation System

**Problem it solves:** Enables voice-to-text input for the chat prompt.

**Implementation:** Three-tier fallback: (1) Web Speech API via `SpeechRecognition` (line ~276-329) with continuous + interim results, (2) MediaRecorder fallback (line ~435-489) that records audio as webm/opus and uploads to the backend's `/audio/transcribe` endpoint, (3) the backend runs whisper.cpp via subprocess for transcription. `toggleDictation()` (line ~399-433) manages the state machine. The dictate button toggles between microphone and stop icons.

**Tradeoffs:** Web Speech API requires Chrome/Edge and sends audio to Google. The MediaRecorder fallback works offline but requires whisper.cpp to be compiled and available on the backend machine. The FFMPEG dependency for audio format conversion adds another system requirement.

---

### 7. Monitoring Dashboard

**Problem it solves:** Provides visibility into node health, model failures, cost tracking, and recent errors.

**Implementation:** `monitoring.html` (210 lines) is a separate page with its own CSS matching the main dark theme. It fetches from `/monitoring/health` and `/analytics/costs` endpoints with an 8-second timeout. Four cards display nodes (name, status, latency), model health (failure counts), cost analytics (total and per-model), and recent errors (last 10, reverse chronological). A refresh button triggers manual reload.

**Tradeoffs:** No auto-refresh interval — monitoring is manual. No graphs or time-series visualization. Cost analytics depend on the backend tracking costs per request, which is only as accurate as the model catalog's `cost_per_1k` estimates.

---

### 8. Project System

**Problem it solves:** Groups conversations under projects with shared system prompts, so different work contexts (code review, brainstorming, specific clients) maintain separate instruction sets.

**Implementation:** Backend stores projects in `dave_projects.json` with CRUD endpoints. Frontend `loadProjects()` (app.js line ~331-356) populates a dropdown with project names plus a "New project..." option. `createProjectFlow()` (line ~358-386) prompts for name, system instructions, preferred model, and description. Conversations can be filtered by project. `resyncProject` button re-applies project instructions to the current conversation.

**Tradeoffs:** Project creation uses `window.prompt()` dialogs, which is functional but not polished. There's no project editing UI beyond the edit button (which likely opens another prompt). Projects and conversations share the same JSON file persistence model.

---

## Top 3 Priorities

1. **Migrate from JSON file persistence to SQLite.** The conversation and project stores use atomic JSON writes, which work for single-user but can't handle concurrent access and will slow down as the data grows. SQLite is already used for vectors and feedback — extending it to conversations would be natural.

2. **Add auto-refresh to the monitoring dashboard.** The monitoring page requires manual refresh and shows no historical data. A 30-second polling interval and a simple in-memory ring buffer for time-series metrics would make it useful for ongoing operations.

3. **Build a proper model routing algorithm.** The current model catalog is static and manually maintained. A routing layer that considers node load, model capabilities, prompt complexity, and user preferences dynamically would unlock the full potential of the multi-node cluster.
