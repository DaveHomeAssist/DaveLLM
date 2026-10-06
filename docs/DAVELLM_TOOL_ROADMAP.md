# DaveLLM tool roadmap

Brainstormed 2026-10-05. This is a candidate list, not an approved plan. Each idea still needs its own brief in `docs/briefs/` before work starts, and the accepted 2026-10-05 adapter-scope decision (finish the Notion adapter first) still stands.

## Starting point

The catalog in `tests/fixtures/davellm/tool_catalog.json` has 25 tools:

- Files: `file.read`, `file.write`, `file.append`, `file.edit`, `file.list`, `file.read_lines`, `file.search`
- Git (read only): `git.status`, `git.diff`, `git.log`, `git.show`
- Markdown: `md.outline`, `md.section`
- Web: `web.fetch`, `web.read`, `web.search`
- System: `shell.exec`, `system.info`, `cluster.status`, `chat.search`
- Project: `project.artifacts`, `project.brain.read`, `project.notepad.read`
- Notion: `notion.page.read`, `notion.page.append`, `notion.block.update`

None of the ideas below duplicates these.

## Rules every new tool follows

- It uses the existing permission tiers (`read`, `read_files`, `read_system`, `public_network`, `write_files`, `write`).
- Writes set `approval_required` and have a preflight that shows the change before it is approved, as `file.edit` and the Notion tools do.
- Targets come from an operator allowlist set by environment variable, as `DAVE_NOTION_PAGES` does. The model cannot widen the allowlist.
- Each write ends as verified, failed or unknown, and an unknown write is never repeated automatically.
- A tool that runs inference has an approval step and a per-run token budget, and is kept separate from H9 qualification.
- Heavy work (indexing, tests, transcription) runs on a runner node, not on the operator's Mac.
- Handler and pinned-catalog rules in `CLAUDE.md` still apply.

## The 100 candidates

**A. Cluster and models**
1. `ollama.ps`: loaded models per node, VRAM use, unload time
2. `model.pull`
3. `model.delete`
4. `model.unload`: free VRAM now (keep_alive 0)
5. `model.warm`: preload before a long task
6. `node.wake`: Wake-on-LAN
7. `node.diagnose`: Tailscale peer, ping, port, `/api/tags` latency, Ollama version
8. `node.disk`: free space per node and model store mount check
9. `node.ctx_check`: the context window a node really uses
10. `cluster.benchmark`: short tokens-per-second probe
11. `model.inventory_drift`: installed models against an expected list
12. `ollama.version_check`

**B. Routing and inference**
13. `route.suggest`
14. `model.ask`: a sub-question to another model on another node
15. `model.consensus`: same prompt to 2–3 models, answers compared
16. `token.count`: prompt tokens against node limits
17. `chat.summarize`
18. `prompt.lint`
19. `eval.run`: run a set of reference prompts against a model
20. `perf.read`: query `cost_log.jsonl` and `performance.db`

**C. Memory and knowledge**
21. `memory.recall`
22. `memory.propose`
23. `vector.search` over `dave_vectors.db`
24. `vector.index`
25. `project.notepad.write`
26. `project.brain.pin`
27. `chat.read`
28. `chat.export`
29. `docs.ask`: answers from a document folder, with citations
30. `pdf.read`
31. `agent.memory.read`: read-only agent memory files
32. `glossary.lookup`

**D. Notion**
33. `notion.search`
34. `notion.db.query`
35. `notion.db.create_row`
36. `notion.db.update_props`
37. `notion.page.create`
38. `notion.comment.add`
39. `notion.page.diff`: compare a page with the copy read earlier in the run
40. `notion.log_session`
41. `notion.routines.read`
42. `notion.inbox.triage`

**E. Code and development**
43. `gh.pr.status`
44. `gh.pr.comments`
45. `gh.ci.logs`
46. `gh.issue.create`
47. `git.worktrees`: worktrees and their `.agent-claim` files
48. `git.commit`
49. `git.push`
50. `test.run` on a runner
51. `code.symbols`
52. `code.grep_regex`
53. `lint.run`
54. `dep.audit`
55. `pages.deploy_status`
56. `vercel.deploy_status`

**F. Workspace governance**
57. `rules.lookup`: a workspace rule by ID
58. `machine.access.lookup`: the nonsecret host access guide
59. `comms.log.append`, with readback
60. `comms.log.read`
61. `nextsteps.read`
62. `nextsteps.update` through the governed updater, then `--check`
63. `agent.claim.check`
64. `runner.select`: pick a runner node by capability

**G. Home and machines**
65. `ha.state.get`
66. `ha.service.call`
67. `ha.history`
68. `ha.config.check`
69. `docker.ps`
70. `docker.logs`
71. `mac.disk.report`
72. `mac.pressure`: memory, swap, top processes
73. `tailscale.status`
74. `service.health`: search service and other node services

**H. Google and messaging**
75. `gmail.search`
76. `gmail.draft` (draft only, never send)
77. `calendar.list`
78. `calendar.suggest_time`
79. `drive.search`
80. `drive.read`
81. `contacts.lookup`
82. `notify.push`

**I. AV and production**
83. `dmx.patch_check`
84. `artnet.discover`
85. `osc.send`
86. `resolume.status`
87. `midi.map.lookup`
88. `av.inventory.lookup`
89. `video.probe`
90. `video.gap_find`: gaps between recorded takes
91. `audio.transcribe`
92. `setlist.parse`

**J. Utility**
93. `calc.eval`
94. `time.convert`
95. `image.describe`
96. `ocr.image`
97. `data.query`: SQL over CSV or JSON files
98. `json.validate`
99. `diagram.render`: Mermaid to SVG
100. `secret.scan`

## Refined to 21

| # | Tool family | Merges | What it does | Safety | Size |
|---|---|---|---|---|---|
| 1 | `cluster.models` | 1, 9, 11, 12 | Extends `cluster.status`: installed and loaded models, VRAM, unload time, context window in use, Ollama version, drift | Read | S |
| 2 | `cluster.model.manage` | 2–5, 8 | Pull, delete, unload or warm; checks disk and model store mount first; pulls are cancellable jobs | Approval | M |
| 3 | `node.diagnose` + `node.wake` | 6, 7, 73 | Checks peer, ping, port and tags latency in order, then gives one verdict (asleep, unplugged, Ollama down, slow); Wake-on-LAN through a node on the same network | Read; wake needs approval | M |
| 4 | `route.plan` | 13, 16 | Prompt size, node limits, loaded models and work in flight give a recommended node and model with a reason; builds on DL-ROUTE profiles | Read, advisory | M |
| 5 | `model.ask` | 14, 15 | Second opinion or a 2–3 model comparison | Inference: approval and budget, never H9 | M |
| 6 | `chat.history` | 17, 27, 28 | Read, export and summarize conversations; summaries run in the background on a warm node, off the event loop | Read; summary uses inference | M |
| 7 | `memory` | 21, 22, 25, 26, 31, 32 | One search across executive memory, project brain, notepad and glossary; writes are only proposed | Read; writes need approval | M |
| 8 | `docs.ask` | 23, 24, 29, 30, 96 | Indexes allowlisted folders (including PDFs and OCR'd images); answers cite file and line | Read; indexing runs on a runner | L |
| 9 | Notion adapter v2 | 33–42 | Database query, row create and property update on allowlisted databases (`DAVE_NOTION_DBS`), search, page create, comments; every write first compares the page with the copy read earlier | Approval | L |
| 10 | `github` | 43–46, 55, 56 | PR status, checks, review comments, failing CI logs, Pages and Vercel state | Read; issue create needs approval | M |
| 11 | `git.write` | 47–49, 63, 100 | Lists worktrees and claims; commit and push after a secret scan and claim check; never force or rewrite history | Approval | M (highest risk) |
| 12 | `code.check` | 50–54, 64 | Tests, lint, dependency audit, symbols, regex search; heavy jobs go to a runner picked by capability | Execute_process with approval for test.run, lint.run and dep.audit (they run repository-controlled code; a runner changes where, not whether); read_files for symbols and regex search | M |
| 13 | `ops.governance` | 57–62 | Rule lookup, host access lookup, comms log read and append with readback, Next Steps read and update through the governed updater | Read; writes need approval | S–M |
| 14 | `notify.push` | 82 | Phone notification when a long run finishes, a node drops or an approval is waiting | Write to an allowlisted topic | S |
| 15 | `home.assistant` | 65–68 | State, history, config check, and service calls on allowlisted entities | Read; calls need approval | M |
| 16 | `server.ops` | 69–72, 74 | Container status and logs, service health, disk, memory and swap pressure | Read_system | S |
| 17 | `google` | 75–81 | Gmail search and draft (never send), calendar list and time suggestions, Drive search and read; the router holds the tokens, not the model | Read; drafts need approval | L |
| 18 | `av.show` | 83–88 | DMX patch validation, Art-Net discovery, Resolume status, MIDI and OSC maps, gear inventory | Read; OSC send needs approval and an allowlist | M |
| 19 | `media` | 89–92, 95 | ffprobe, finding gaps between takes, transcription, setlist parsing, image description, on runner nodes | Read, runner | M |
| 20 | `data.tools` | 93, 94, 97–99 | Calculator, time conversion, SQL over files, JSON schema validation, Mermaid rendering | Read | S |
| 21 | `quality` | 10, 18–20 | Prompt lint, reference-prompt runs, performance and cost history; evidence for model choice, separate from H9 | Inference: approval and budget | M |

## Suggested phases

| Phase | Families | Reason |
|---|---|---|
| 1 | 1, 3, 16 | Small; read-only diagnostics plus one approved write (`node.wake`); aimed at node reliability |
| 2 | 13, 10, 20, 6 | DaveLLM follows the workspace's own operating rules; summaries work |
| 3 | 9, 7, 15, 12, 14 | Approved writes built on the Notion preflight pattern; `notify.push` joins here because it is an outbound connector |
| 4 | 4, 5, 21, 2, 11 | Inference and higher-risk writes, after the routing and evidence work |
| 5 | 8, 17, 18, 19 | Large integrations, built when needed |

Phase 1 is not read-only: `node.wake` changes state and keeps its approval requirement. Phase 1 adds no connector to another service; `notify.push` (family 14) needs an outbound notification transport the repository does not have, so it sits in Phase 3 behind the accepted 2026-10-05 decision to finish the Notion adapter before other-service connectors. Notion adapter v2 (family 9) follows live write acceptance of the current adapter.

## Open decision

DL-TOOLS-01: which slice gets the first brief. Options:

- A: Phase 1 as one brief (recommended)
- B: Notion adapter v2
- C: `chat.history` summaries
- D: something else
