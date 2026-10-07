# Switching on tools in the Mac app

This is the operator guide for turning on the file and Git tools and a first slice of [Tools100](TOOLS100.md) in the installed Mac app. Everything here is a nonsecret file in the DaveLLM data folder. No token goes in any file, and nothing turns on until you create the file and restart the app.

The data folder is `~/Library/Application Support/DaveLLM`. The launcher (`scripts/macos/launch-davellm.sh`) reads two optional files from it on every start:

| File | Becomes | Rules |
|---|---|---|
| `tool-roots.json` | `DAVE_TOOL_ROOTS` | A JSON list of absolute folders |
| `toolpack.json` | `DAVE_ENABLE_TOOLPACK=true` and `DAVE_TOOLPACK_CONFIG` | A JSON object with a nonempty `enabled_tools` list, under 64 KiB |

Each file must be a regular file you own that no one else can write: not a symlink, and not group- or world-writable. The launcher ignores a file that breaks a rule, logs one line to `~/Library/Logs/DaveLLM/launcher.log`, and starts anyway. A value already set in the environment (a Terminal launch with `DAVE_TOOL_ROOTS` or `DAVE_TOOLPACK_CONFIG` exported) wins over the file.

## 1. Let the file and Git tools see your folders

Without roots, `git.status`, `git.diff`, `git.log`, `git.show` and the file tools refuse every path. List only the folders the model may read; secret files inside them (`.env`, keys, `.ssh` and the rest of the denylist in `CLAUDE.md`) stay hidden either way.

```bash
cd ~/Library/Application\ Support/DaveLLM
cat > tool-roots.json <<'JSON'
["/Users/YOUR-MAC-USER/Code/DaveLLM", "/Users/YOUR-MAC-USER/Code/prompt-lab"]
JSON
chmod 600 tool-roots.json
```

Use your real paths ([example](examples/tool-roots.example.json)), quit DaveLLM and start it again. With several roots, give the tools absolute paths; a relative path works only when there is exactly one root.

## 2. Switch on the Tools100 starter

The [starter](examples/toolpack.starter.json) turns on 13 read-only tools that need no token, no helper machine and no further settings:

| Area | Tools |
|---|---|
| Cluster | `ollama.ps`, `node.diagnose`, `node.ctx_check`, `route.suggest` |
| Git and code (need step 1) | `git.worktrees`, `git.write_preview`, `agent.claim.check`, `code.symbols` |
| Utilities | `calc.eval`, `time.convert`, `json.validate`, `secret.scan`, `prompt.lint` |

```bash
cp ~/Code/DaveLLM/docs/examples/toolpack.starter.json ~/Library/Application\ Support/DaveLLM/toolpack.json
chmod 600 ~/Library/Application\ Support/DaveLLM/toolpack.json
```

Adjust the checkout path, then restart DaveLLM. To add more later, add names to `enabled_tools`. An entry can be an exact tool name or a family such as `"gh.*"`. A misspelled name registers no Tools100 tool at all, so a typo is obvious rather than half-applied.

## 3. Check what is on

```bash
curl -s -H "X-API-Key: $(security find-generic-password -a "$USER" -s com.davellm.api-key -w)" \
  http://127.0.0.1:8000/tools | jq '.tools | keys'
```

The key is read from your Keychain for that one command and is not printed.

## Why not everything at once

Every registered tool's description goes to the model on every step of a tool run, and the model's context has to hold them plus the conversation and the reply. Estimates from the schema sizes (characters divided by 3 to 4):

| Tools on | Tools | Tool descriptions |
|---|---|---|
| Today's launcher (qualified plus extended) | 22 | about 3,000 to 3,900 tokens |
| Plus the starter | 35 | about 4,100 to 5,500 tokens |
| Plus all of Tools100 | 124 | about 13,700 to 18,300 tokens |

Plain chat asks Ollama for 16,384 tokens, and llama3 holds only 8,192. The whole pack would leave no room for the conversation, so turn on families as you configure them, and use a model with the larger window (such as `gpt-oss:20b`) for tool runs. A tool run that cannot fit is refused before the model is called, with a message naming the tool descriptions, the messages and the reply reserve; project context in a run already leaves room for the descriptions.

## 4. Tools that need settings but no token

These need entries in `toolpack.json` next to `enabled_tools`. The shapes are checked by the code; see [TOOLS100.md](TOOLS100.md#configuration) for the full list.

| Tools | Settings key and shape |
|---|---|
| `model.inventory_drift` | `"expected_models": {"walter": ["llama3:latest", "gpt-oss:20b"]}` (exact installed names) |
| `glossary.lookup`, `midi.map.lookup`, `av.inventory.lookup` | `"glossary"`, `"midi"`, `"av_inventory"`: `{"name": record}` |
| `rules.lookup`, `machine.access.lookup`, `comms.log.read`, `nextsteps.read`, `memory.recall`, `claude.memory.read` | `"sources": {"alias": {"path": "/abs/path", "users": ["default"]}}`; the path must be inside a tool root and `users` must name the DaveLLM user |
| `service.health`, `resolume.status` | `"services": {"alias": {"url": "https://host/health"}}`; the endpoint must answer 2xx with JSON or an empty body |
| `node.wake` | `"wake_targets": {"walter": {"mac": "00:11:22:33:44:55", "broadcast": "192.168.1.255"}}` |
| `notify.push` | `"notifications": {"alias": {"url": "https://ntfy.sh", "topic": "your-topic"}}` (token optional) |
| `osc.send` | `"osc_targets"` with a private host, port and address allowlist |
| `runner.select` | `"runners"` (see step 6) |

Tools that change your project data (`project.notepad.write`, `project.brain.pin`, `memory.propose`, each with approval) and the chat readers (`chat.read`, `chat.export`, `perf.read`, `token.count`) need no settings but work only inside a lifecycle tool run, where DaveLLM knows the user and project.

## 5. Tokens

Tokens come only from the router's environment, read once at start; changing one needs a restart. They are never written into `toolpack.json`, `tool-roots.json`, the repository, a chat, or a log, and the launcher reads none of them.

| Token | Unlocks | Also needs in `toolpack.json` |
|---|---|---|
| `DAVE_GITHUB_TOKEN` | `gh.pr.status`, `gh.pr.comments`, `gh.ci.logs`, `gh.issue.create` (approval), `pages.deploy_status` | `"repositories": {"davellm": "DaveHomeAssist/DaveLLM"}` |
| `DAVE_VERCEL_TOKEN` | `vercel.deploy_status` | `"vercel_projects": {"alias": "<project id>"}` |
| `DAVE_HA_TOKEN` | `ha.state.get`, `ha.history`, `ha.service.call` and `ha.config.check` (approval) | `"services": {"homeassistant": {"url": "...", "entities": [...], "allowed_services": [...]}}` |
| `DAVE_NOTION_TOKEN` | the ten `notion.*` Tools100 tools | `"notion_databases"`, and `DAVE_NOTION_PAGES` for page tools |
| `DAVE_GOOGLE_ACCESS_TOKEN` | `gmail.*`, `calendar.*`, `drive.*`, `contacts.lookup` | `"google_users": ["default"]` plus `calendars`, `drive_folders`, `drive_files` |
| `DAVE_NTFY_TOKEN` (optional) | private ntfy topics for `notify.push` | `"notifications"` |

The GitHub tools read pull requests, check runs, Actions jobs and logs, and Pages builds, and `gh.issue.create` writes issues. A fine-grained token limited to the listed repositories needs read access to those areas, plus issue write only if you want issue creation. The Google token is a short-lived OAuth access token with no refresh in this release, so it suits a one-off session, not daily use.

Until a Keychain-backed setting exists (the open "Notion setup" decision in `docs/NEXT_STEPS.json`), keep each token in your own Keychain and hand it to one Terminal launch. `security add-generic-password ... -w` with no value prompts for the secret, so it never lands in shell history:

```bash
security add-generic-password -a "$USER" -s com.davellm.github-token -w
DAVE_GITHUB_TOKEN="$(security find-generic-password -a "$USER" -s com.davellm.github-token -w)" \
  zsh ~/Code/DaveLLM/scripts/macos/launch-davellm.sh
```

A Dock launch does not see Terminal variables, so the token tools stay off there and refuse with a fixed message.

## 6. Helper machine and model-calling tools (later)

Thirty tools need more than a file:

- 22 run as one-shot jobs on a POSIX helper over SSH (`git.commit`, `git.push`, `test.run`, `lint.run`, `dep.audit`, `docker.*`, `pdf.read`, `ocr.image`, `audio.transcribe`, `video.probe`, `data.query`, `diagram.render` and others). They need `DAVE_ENABLE_TOOL_JOBS=true`, a `runners` entry, and `scripts/tool_job_runner.py` installed with its own configuration on that machine; see [the runner contract](TOOLS100.md#runner-installation-contract). Walter and Duncan run Windows, where jobs refuse by design; Dominic or the Mac is the candidate.
- 7 call a model (`model.ask`, `model.consensus`, `cluster.benchmark`, `chat.summarize`, `eval.run`, `docs.ask`, `image.describe`). They need `DAVE_ENABLE_TOOL_INFERENCE=true`, exactly `true`, and approval on every call.
- `model.pull` needs both: an allowlisted model and a fresh disk check from a helper.

Every one of them asks for exact-call approval each time, so turning them on never lets a model act without you.

## Every Tools100 tool by what it needs

- **Flags only, or the nodes and folders the app already has (29):** `ollama.ps`, `model.delete`, `model.unload`, `model.warm`, `node.diagnose`, `node.ctx_check`, `ollama.version_check`, `route.suggest`, `token.count`, `prompt.lint`, `perf.read`, `memory.propose`, `vector.search`, `project.notepad.write`, `project.brain.pin`, `chat.read`, `chat.export`, `git.worktrees`, `code.symbols`, `agent.claim.check`, `dmx.patch_check`, `video.gap_find`, `setlist.parse`, `calc.eval`, `time.convert`, `json.validate`, `secret.scan`, `project.notepad.snapshot`, `git.write_preview`.
- **Settings only (16):** `node.wake`, `model.inventory_drift`, `memory.recall`, `claude.memory.read`, `glossary.lookup`, `rules.lookup`, `machine.access.lookup`, `comms.log.read`, `nextsteps.read`, `runner.select`, `service.health`, `notify.push`, `osc.send`, `resolume.status`, `midi.map.lookup`, `av.inventory.lookup`.
- **A token (27):** the ten `notion.*` tools, `gh.pr.status`, `gh.pr.comments`, `gh.ci.logs`, `gh.issue.create`, `pages.deploy_status`, `vercel.deploy_status`, the four `ha.*` tools, `gmail.search`, `gmail.draft`, `calendar.list`, `calendar.suggest_time`, `drive.search`, `drive.read`, `contacts.lookup`.
- **Inference, jobs or a helper (30):** listed in step 6.

`model.delete`, `model.unload` and `model.warm` need no settings but change a node, so each asks for approval.
