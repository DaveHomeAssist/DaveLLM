# DaveLLM tools

DaveLLM offers tools to Ollama models through the in-process DaveHarness registry. All tools are off by default. This page covers the settings, the qualified built-in tools, the extended file tools added in PR-02, the Markdown tools added in PR-03, the read-only Git tools added in PR-04, the native read tools added in PR-05, and the approved `file.edit` added in PR-06.

[DAVEHARNESS_CAPABILITIES.md](DAVEHARNESS_CAPABILITIES.md) lists every tool's full schema, flags, run budgets, host limits, routes, and tool-module constants in one place. It and its machine-readable twin, [DAVEHARNESS_CAPABILITIES.json](DAVEHARNESS_CAPABILITIES.json), are generated from the registry by `scripts/generate_capabilities_manifest.py`.

## Settings

| Setting | Default | Effect |
|---|---|---|
| `DAVE_ENABLE_TOOLS` | `false` | Turns on the tool registry and the tool routes. Nothing below works without it. |
| `DAVE_TOOL_ROOTS` | `[]` | JSON array of absolute folders. Every file tool stays inside these roots. |
| `DAVE_ENABLE_SHELL_TOOL` | `false` | Second opt-in for `shell.exec`. |
| `DAVE_ENABLE_EXTENDED_TOOLS` | `false` | Adds `file.list`, `file.search`, `file.read_lines`, `md.outline`, `md.section`, `git.status`, `git.diff`, `git.log`, `git.show`, `project.notepad.read`, `project.brain.read`, `project.artifacts`, `chat.search`, `cluster.status`, and `file.edit`. Honored only when `DAVE_ENABLE_TOOLS` is also on. |

```bash
export DAVE_ENABLE_TOOLS=true
export DAVE_TOOL_ROOTS='["/absolute/path/to/project"]'
export DAVE_ENABLE_EXTENDED_TOOLS=true
```

Leave `DAVE_ENABLE_EXTENDED_TOOLS` off while qualifying a target model, so H9 results stay comparable with the six-tool baseline.

## Qualified built-in tools

| Tool | Permission | Approval |
|---|---|---|
| `system.info` | `read_system` | No |
| `file.read` | `read_files` | No |
| `file.write` | `write_files` | Yes |
| `file.append` | `write_files` | Yes |
| `web.fetch` | `public_network` | No |
| `shell.exec` (needs `DAVE_ENABLE_SHELL_TOOL`) | `execute_process` | Yes |

These tools are unchanged by the extended tools. `file.read` still returns at most 5,000 characters and does not apply the protected-path rules below.

## Extended file tools

Fourteen of the fifteen extended tools, including the Markdown, Git, and native tools below, are read-only: no approval, synchronous handlers, bounded cancellation, and a 10-second timeout. The fifteenth, [`file.edit`](#fileedit), is the only extended tool that writes, and every call needs the user's approval. The file, Markdown, and Git tools use permission `read_files`. The native tools use `read`, except `cluster.status`, which uses `read_system`. Optional arguments may be omitted or sent as `null` for their default. Unknown arguments and wrong types are rejected by the schema.

### `file.list`

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `path` | string | `.` (the tool root) | 1–4,096 characters |
| `depth` | integer | `1` | 0–3. `0` describes the path itself; `1` lists its direct children. |
| `max_entries` | integer | `100` | 1–500 per page |
| `cursor` | string | none | `next_cursor` from the previous page of the same request |

The result has `path`, `depth`, `entries`, `truncated`, `next_cursor`, `depth_limited` (a folder at the depth limit has more inside), and `limit_reached` (the listing passed 10,000 entries; narrow the path or depth). Each entry has `path`, `name`, and `type` (`file`, `directory`, `symlink`, or `other`). Files also carry `size` and `modified` (UTC). Folders, symlinks, and other entries carry no timestamp, because a folder's time changes whenever a child is added or removed, including a protected one that is never shown.

Entries are breadth-first and sorted by name within each folder. A cursor works only with the exact request that produced it (same path, depth, and page size) and is refused otherwise.

### `file.search`

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `query` | string | required | 1–200 characters, one line |
| `path` | string | `.` | A folder, or a single file |
| `case_sensitive` | boolean | `false` | |
| `max_matches` | integer | `50` | 1–200 |

Search is literal: characters such as `.`, `*`, and `(` match themselves. It reports the first match on each line, in breadth-first file order. Each match has `path`, `line` (1-based), and `text`. `text` holds at most 300 characters around the match, and `text_truncated` marks a shortened line.

The result also counts `files_scanned`, `skipped_large` (over 1 MiB), `skipped_binary` (NUL bytes or invalid UTF-8), and `skipped_unreadable`. `truncated` means more matches or files existed than were returned or scanned. At most 2,000 files and 20,000 walked entries are considered per search. Symlinks are not searched, and there is no cursor; narrow `path` or `query` instead.

Regex search is not available. Python's regular-expression engine can backtrack for an unbounded time, and a synchronous tool handler cannot be interrupted, so a regex mode would need a bounded engine such as `google-re2` or a separate process. That is a pending decision.

### `file.read_lines`

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `path` | string | required | A strict UTF-8 text file up to 10 MiB |
| `start_line` | integer | `1` | 1–10,000,000 |
| `max_lines` | integer | `200` | 1–400 |

The result has `path`, `start_line`, `lines`, `line_count`, `total_lines`, `next_start_line` (`null` at the end of the file), and `truncated`. Lines split on LF, and a trailing CR is removed. A start past the end returns no lines and the real `total_lines`. Lines longer than 2,000 characters are cut and listed in `cut_lines`. Binary and non-UTF-8 files are refused, never decoded with replacement characters.

## Markdown tools

`md.outline` and `md.section` navigate long Markdown files by heading instead of by page. They read through the same path rules, protected paths, symlink-swap protection, UTF-8 check, and 10 MiB size limit as `file.read_lines`. Both use one heading parser, `parse_headings` in `davellm_markdown.py`, so they always agree on where headings and sections are. It is a small line-by-line reader with no regular expressions and no Markdown dependency.

### Heading syntax

| Syntax | Behavior |
|---|---|
| ATX `#` to `######` | A heading after at most three spaces when a space, a tab, or the end of the line follows the marks. `#hashtag` and `####### seven` are not headings. |
| Closing `#` marks | Removed when a space or tab precedes them: `## Setup ##` is `Setup`, while `## C#` stays `C#`. |
| Inline Markdown | Kept as written, never rendered: `## *New* in [2.1](notes.md)` is `*New* in [2.1](notes.md)`. |
| Setext | A `===` (level 1) or `---` (level 2) underline directly under a paragraph. Paragraph lines join with single spaces. `line` is the paragraph's first line and `source_end_line` is the underline. |
| Fenced code | Three or more backticks or tildes after at most three spaces open a fence. Only the same character, repeated at least as often and followed by nothing but spaces, closes it. An unclosed fence runs to the end of the file. Nothing inside a fence is a heading or ends a section. |
| Indented code | A line indented four or more columns is never a heading or a fence. A tab counts to the next multiple of four. |
| Front matter | A `---` block at the very start of the file, closed by `---` or `...`, is skipped so its closing line is not read as a Setext underline. Its contents are not parsed or returned. |

Block quotes, list items, and HTML are not parsed. `> # Title` and `- # Item` are never headings, and a `---` under a list item, quote, or HTML line is not a Setext underline. A `#` line inside an HTML block still counts as a heading.

A file with more than 20,000 headings is refused with "File has more than 20,000 headings". This bounds parsing time and memory on hostile input; use `file.search` or `file.read_lines` for such a file.

### `md.outline`

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `path` | string | required | A strict UTF-8 text file up to 10 MiB |
| `max_headings` | integer | `200` | 1–500 |

The result has `path`, `headings`, `heading_count` (returned), `total_headings` (in the file), `total_lines`, `truncated`, and `next_start_line`. Each heading has `text`, `level` (1–6), and `line` (1-based). Setext headings also have `source_end_line`. Text longer than 300 characters is cut and marked with `text_truncated`. Outline entries have no breadcrumb because the levels already give the hierarchy; `md.section` returns one.

When `max_headings` or the result budget ends the list early, `truncated` is `true` and `next_start_line` is the line of the first heading left out. Read on from there with `file.read_lines`. There is no outline cursor.

### `md.section`

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `path` | string | required | A strict UTF-8 text file up to 10 MiB |
| `heading` | string | required | 1–500 characters |
| `include_subsections` | boolean | `true` | |

**Matching.** The request and every heading are compared after trimming, collapsing runs of whitespace, and case folding, so `install`, `  INSTALL ` and `Install` are the same and `STRASSE` matches `Straße`. If a request written with its marks, such as `## Setup`, matches nothing as written, it is tried again without them. Nothing else is matched: no prefixes, substrings, or near misses. With no match the tool fails with "Heading not found", and it never lists the outline in the error.

**Ambiguity.** When several headings match, the tool succeeds with `ambiguous: true`, `match_count`, and `matches`. Each candidate has `text`, `level`, `line`, and `breadcrumb`, plus `source_end_line` for Setext. No section text is returned and the tool never picks one. Read the intended one with `file.read_lines` from its `line`. Candidates stop at the result budget with `truncated: true`.

```json
{"path": "docs/guide.md", "heading": "Setup", "ambiguous": true, "match_count": 2, "truncated": false,
 "matches": [{"text": "Setup", "level": 2, "line": 3, "breadcrumb": ["Linux", "Setup"]},
             {"text": "Setup", "level": 2, "line": 9, "breadcrumb": ["macOS", "Setup"]}]}
```

**Boundaries.** For a heading at level N, the section starts on the line after the heading, or after the underline for a Setext heading. The heading line itself is not in `lines`; its text, level, and line come back as fields.

- With `include_subsections: true`, the section runs to the line before the next heading of level N or lower, such as the next sibling or a parent. Deeper subsections stay inside.
- With `include_subsections: false`, it stops before the next heading of any level.
- With no such heading, it runs to the end of the file.

**Result.** `path`, `ambiguous: false`, `heading`, `level`, `line`, `breadcrumb` (the ancestor headings, then this one), `include_subsections`, `content_start_line` and `content_end_line` (the whole section's range, or `null` for an empty section), `lines`, `line_count`, `truncated`, and `next_start_line`.

**Limits and continuation.** At most 400 lines are returned, and fewer when the result budget is reached first. Lines longer than 2,000 characters are cut and listed in `cut_lines`, as in `file.read_lines`, and also set `truncated`. When the section continues past the returned lines, `next_start_line` is the next line to read. Call `file.read_lines` with that `start_line` and stop at `content_end_line`; `md.section` has no paging of its own.

## Git tools

`git.status`, `git.diff`, `git.log`, and `git.show` give read-only awareness of a Git working tree inside a tool root. Their implementation is `davellm_git.py`. There is no tool that commits, stages, switches branches, fetches, or runs arbitrary Git arguments, and none is planned in this set.

Every tool takes `path`, a folder inside the working tree (default `.`, the tool root). The result's `path` is the repository's top folder relative to the tool root. File paths inside results are relative to the repository.

### `git.status`

| Argument | Type | Default |
|---|---|---|
| `path` | string | `.` |

The result has `branch` (`null` when detached), `detached`, `commit` (12 characters), `upstream`, `ahead` and `behind` (`null` without an upstream), and four lists: `staged` and `unstaged` (each entry has `path` and `change`: `modified`, `added`, `deleted`, `type_changed`, …), `untracked` (paths), and `conflicted`. `counts` gives each list's full length. Each list holds at most 100 entries, and `truncated` says when anything was left out. Renames show as a deletion and an addition. Status comes from Git's machine-readable porcelain v2 output and never takes a lock or writes the index.

### `git.diff`

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `path` | string | `.` | |
| `staged` | boolean | `false` | `true` compares the index with `HEAD` |
| `from_revision` | string | none | Compare two commits instead |
| `to_revision` | string | `HEAD` | Needs `from_revision` |

The three modes are working tree against the index (the default), staged, and revisions. `staged` and `from_revision` cannot be combined. The result has `mode`, a unified `diff`, and `truncated`; revision diffs also give `from_revision`, `to_revision`, `from_commit`, and `to_commit`. Untracked files do not appear; use `git.status`.

### `git.log`

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `path` | string | `.` | |
| `limit` | integer | `20` | 1–50 |
| `file` | string | none | Only commits that touched this repository path |

Commits start at `HEAD`, newest first. Each has `commit` (short hash), `date` (ISO 8601), `author` (name only, never an e-mail address), and `subject`. Bodies, signatures, notes, and decorations are left out. `truncated` means older commits exist. A repository with no commits returns an empty list.

### `git.show`

| Argument | Type | Default |
|---|---|---|
| `path` | string | `.` |
| `revision` | string | required |
| `file` | string | none |

Without `file`, the result is the commit's `commit`, `date`, `author`, `parents`, `subject`, `body` (at most 2,000 characters, with `body_truncated`), and its patch in `diff`. A merge shows Git's combined diff. With `file`, the result is that file's text at the commit instead: `lines` (at most 400, long lines cut as in `file.read_lines`), `line_count`, `total_lines`, and `truncated`. There is no paging for historical files. An annotated tag is shown as the commit it points to.

### Revisions

A revision is `HEAD`, a branch or tag name, or a full or abbreviated commit hash, optionally followed by `~N` or `^N` suffixes, such as `main~2` or `HEAD^2`. At most 128 characters are accepted: letters, digits, `.`, `_`, `-`, `/`, `~`, and `^`, and each name part must start with a letter, digit, or `_`. Refused forms include:

- anything starting with `-`, so a revision can never be read as a Git option;
- ranges (`a..b`, `a...b`), reflog and upstream forms (`@{...}`), peeling (`^{...}`), and `rev:path`;
- searches (`:/text`), whitespace, and shell characters.

A revision that passes is resolved with `git rev-parse --verify --end-of-options <revision>^{commit}`. Every later command gets only the resulting commit hash. File paths are checked separately: they must be relative to the repository, without `..`, a leading `-` or `:`, backslashes, or control characters. They are passed after `--` as literal pathspecs.

### Protected paths in Git output

The protected-path patterns apply inside repositories too:

- `git.status` never lists a protected path.
- Patches from `git.diff` and `git.show` leave protected files out.
- `git.log` and `git.show` refuse a protected `file` with "Access denied: path is not allowed".
- Commit subjects and bodies are not filtered.

### Limits

| Limit | Value |
|---|---|
| Patch text | 40,000 bytes, cut at a line end, plus the 48 KiB result budget |
| Status entries | 100 per list |
| Commits | 50 per call |
| Git output read | 1 MiB per Git process, which is then stopped |
| Time | 8 seconds per tool call for all its Git processes together, below the 10-second tool timeout |

### Why arbitrary Git arguments are not supported

Git options can write files (`--output`), run programs (`--ext-diff`, `--textconv`, `--upload-pack`, `-c`), reach the network, or change which repository is read (`--git-dir`, `--work-tree`). Every one of those would have to be recognized and blocked, and a new Git version can add more. The tools instead take a few typed arguments and build every Git command themselves.

### Hardened runner

A repository's own configuration can name programs that Git runs during ordinary reads. Every Git process therefore starts in `run_git`, the one runner:

- **No shell.** Git runs directly from an argument list, with stdin closed, in its own process group. A timeout or an output cap kills the whole group.
- **Clean environment.** Nothing is inherited from the DaveLLM process. System and global Git configuration are ignored, prompts are disabled, and the pager is `cat`.
- **No locks.** `--no-optional-locks` means status never takes a lock or writes the index.
- **No network.** `GIT_ALLOW_PROTOCOL` refuses every protocol, overriding repository settings, and lazy fetching from promisor remotes is disabled.
- **Forced configuration.** `GIT_CONFIG_*` overrides disable, whatever the repository says: `core.fsmonitor`, hooks (`core.hooksPath` is `/dev/null`), credential helpers and `core.askPass`, attribute and exclude files outside the repository, signature verification (`log.showSignature`), mail maps, submodule recursion and summaries, and implicit bare repositories.
- **Filters.** Every clean, smudge, and process filter the repository configures is overridden with an empty command, so filters never run.
- **Diff programs.** Every diff-producing command also passes `--no-ext-diff` and `--no-textconv`, so external diff programs, diff drivers, and text converters never run.

### Repository admission

- **Path.** The requested folder must pass the extended path boundary: containment, anchoring, and the protected-path denylist.
- **Discovery.** Git may search upward for the repository no further than the tool root, never into the folder above it.
- **Locations.** The working tree, Git directory, common directory, and object store must all resolve inside a tool root and outside protected folders. A `.git` file or symlink that points elsewhere is refused with "Not a Git working tree", the same answer as a folder that is not a repository, so the answer never reveals whether something outside the roots is a Git directory. So is a `core.worktree` that moves the working tree elsewhere, because then the requested folder is not a working tree.
- **No links inside the Git directory.** Git follows symlinks inside its own directory, so a linked `packed-refs`, pack folder, loose object, ref, or index could read another repository's history. The Git directory, common directory, and object store are walked without following links before any read. A symlink or special file anywhere in them, except under `hooks` (hooks never run), is refused with "Repository layout is not supported". So is a Git directory with more than 100,000 entries.
- **Alternate object stores** are refused, because they would let a repository read objects from outside the roots. `objects/info/alternates` is read like any extended-tool file: never through a symlink, non-blocking, and at most 64 KiB. Anything but a missing file or a small regular file holding only comments and empty lines is refused with the same message. A line of spaces or a carriage return counts as an alternate, because Git reads it as a path. See the [Git tools security review](DAVELLM_GIT_SECURITY_REVIEW.md).
- **Pinned locations.** After admission every command gets the admitted Git directory and working tree explicitly, so Git does not search again.
- **Ownership.** A repository owned by another user is refused ("Repository is owned by another user"), keeping Git's own `safe.directory` protection.

## Native read tools

These tools let a model read DaveLLM's own information instead of guessing it:

- `project.notepad.read`, `project.brain.read`, and `project.artifacts` read the run's project.
- `chat.search` searches your earlier conversations.
- `cluster.status` reports the configured nodes.

All five are read-only and live in `davellm_native_tools.py`. They reuse the same project, BRAIN, artifact, search, and node-health services as the HTTP routes.

### Scope comes from the run

A tool run records who started it, which project it belongs to, and the BRAIN revision read when it started. The native tools take their scope from that record only.

- **No scope arguments.** No schema has a user, project, owner, or node field, and unknown arguments are rejected, so a model cannot point a tool at another project or user.
- **Project tools need a project run.** Without one they fail with "This tool requires a project-scoped run". They never fall back to a recent or default project.
- **Chat search needs a run.** Without one, `chat.search` fails with "This tool requires a DaveLLM run".
- **Same ownership check as the routes.** Every project read repeats the check the HTTP routes use. A project the run's user does not own, or one that no longer exists, gives "The project for this run is not available".
- **Where they work.** Runs started with `POST /tools/agent/runs` carry this record. The legacy `POST /tools/agent/run` loop and `POST /tools/execute` do not, so there only `cluster.status` works.

### `project.notepad.read`

No arguments. Returns `project` (its name), `notepad`, `character_count`, `updated_at`, and `truncated`. At most 20,000 characters are returned. An empty notepad is a normal result.

### `project.brain.read`

No arguments. Returns the BRAIN as it was when the run started: `revision`, `pinned`, `active`, `recent`, `deleted`, `character_count` for each section, and `truncated`. The three sections share a 30,000-character limit, and later sections give way first.

The tool reads the stored snapshot of the run's captured revision and checks it against the fingerprint taken at run start. Edits, compactions, or restores made while the run is going do not change what the run sees. If that revision is gone or no longer matches, the tool fails with "The BRAIN revision captured for this run is not available" and never falls back to the latest revision.

### `project.artifacts`

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `artifact` | string | none | 1–100 characters; omit it to list |
| `max_entries` | integer | `20` | 1–50, for listing |

Listing returns `artifacts` (each with `artifact`, `title`, `kind`, `pinned`, `created_at`, `updated_at`), plus `count`, `total`, and `truncated`. Pinned artifacts come first, and archived artifacts are left out. Reading one returns its fields plus `text` (at most 30,000 characters), `character_count`, and `truncated`. An identifier that is not in the run's project gives "Artifact not found", including one from another project. Listing does not create artifacts from old conversations the way the project page can.

### `chat.search`

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `query` | string | required | 1–200 characters |
| `max_results` | integer | `5` | 1–10 |

Searches only the run user's conversations, using the same search as `GET /search`. The query never changes whose conversations are searched. Only user and assistant messages are searched; system prompts, system and tool messages, and conversation instructions never appear. Each result has `conversation` (its identifier), `title`, `role`, and a `snippet` of at most 300 characters around the match.

### `cluster.status`

No arguments. Checks every configured node and returns `node` (its ID), `name`, `reachable`, `latency_ms`, and `models`, with at most 50 models per node. `models` is the model list DaveLLM has already loaded for that node, or `null` when it has not loaded one yet. Node URLs, IP addresses, host names, credentials, and error text are never returned, and the model cannot choose which host is contacted.

## `file.edit`

`file.edit` replaces exact text in an existing UTF-8 text file inside a tool root. It is the only extended tool that writes. It uses permission `write_files`, needs exact-call approval for every call, and has bounded cancellation and a 10-second timeout. Its implementation is `davellm_edit.py`.

| Argument | Type | Default | Bounds |
|---|---|---|---|
| `path` | string | required | an existing file; relative to the tool root, or absolute inside it |
| `old_text` | string | required | 1–20,000 characters, copied exactly from the file |
| `new_text` | string | required | 0–20,000 characters; empty deletes `old_text` |
| `expected_count` | integer | `1` | 1–100 |

`old_text` must occur exactly `expected_count` times. Any other count refuses the edit and nothing is written, so an approval always covers exactly the change it showed. When the count matches, every occurrence is replaced. The result gives `path`, `replacements`, `first_changed_line`, `line_endings` (`lf` or `crlf`), `bytes_before`, and `bytes_after`. The file's text is never returned.

### Approval

The run pauses before anything is written. The approval card shows the path, how many occurrences will be replaced, the text before, and the text after. An empty replacement reads as a deletion. The exact arguments stay available under the preview. The card builds the preview with text nodes only, so markup in the file or in the model's arguments is shown as text and never rendered.

Approving runs exactly the arguments shown. The count is checked again when the edit runs, not when it was requested: if the file changed in the meantime so that `old_text` no longer occurs `expected_count` times, the edit is refused and nothing is written. Rejecting writes nothing and ends the run.

### How the write works

- **Same admission as the reads.** The path goes through `resolve_extended_tool_path`: anchoring, containment, and the protected-path rules. A path outside the roots or a protected path is refused before the file is opened, even after approval.
- **No symlinks.** The file's folder is opened one component at a time from `/` with `O_NOFOLLOW`, and the file is opened relative to it, the same way as the reads under [Symlink swaps](#symlink-swaps). Only an existing regular file can be edited. `file.edit` never creates files or folders.
- **Atomic.** The new content goes to a fresh temporary file (`.davellm-edit-` plus a random name) in the same folder with the original permission bits. It is then renamed over the original, so a reader sees the old file or the new one, never a mix.
- **Checked just before the rename.** The file is opened and read again right before the rename. If it is no longer the same file with the same bytes, the temporary file is removed and nothing is written.
- **Line endings kept.** In a file whose every line ends with CRLF, line breaks in `old_text` and `new_text` are matched and written as CRLF. Files with mixed line endings are matched exactly as they are. A byte order mark stays in place.
- **One name only.** A file with more than one hard link is refused, because replacing one name would leave the others unchanged.
- **Size.** The file must be at most 10 MiB before and after the edit.

The approval card lives in the browser and desktop UI for runs started with `POST /tools/agent/runs`. As with `file.write`, the legacy `POST /tools/agent/run` loop pauses on `file.edit` unless the request lists it in `approved_tools`, and the authenticated `POST /tools/execute` route runs it directly.

## Paths

- **Input.** An absolute path must be inside a tool root. A relative path is anchored at the tool root when exactly one root is configured. With several roots, relative paths are refused with "Use an absolute path when several tool roots are configured".
- **Output.** Paths are always relative to the deepest tool root that contains them, so a path shown by one tool can be passed to another. Absolute paths never appear in results or errors.

## Protected paths

Extended tools refuse and hide environment files, private keys and certificates, and SSH, AWS, and GnuPG folders. The exact patterns are `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `*.p12`, `.ssh`, `.aws`, and `.gnupg`.

- Patterns are matched case-insensitively against every part of the path, both as written and after symlinks are resolved.
- Protected items are never listed, searched, or entered, and they do not change any count, flag, cursor, or returned timestamp.
- Asking for one directly returns "Access denied: path is not allowed". This is the same message as for a path outside the roots, so an error never reveals whether a protected file exists.
- Symlinks that leave the root, point at a protected path, or loop are left out of listings and searches.

## Symlink swaps

A file or folder could be replaced by a symlink between the path check and the moment it is opened. Every extended-tool file read defends against this in one of two ways:

- **Linux, macOS, and the BSDs:** the file is opened one path component at a time, starting at `/`. Each component is opened relative to its parent's descriptor with `O_NOFOLLOW`, and folders also with `O_DIRECTORY`. A component that has become a symlink makes the open fail, and the tool returns "Access denied: path is not allowed".
- **Elsewhere:** the file is opened and then checked to be the same inode, still reached through a path that contains no symlink.

In both cases the opened object must be a regular file. Files are opened non-blocking, so a named pipe is refused rather than hanging.

Listings read names and metadata, never file contents, so they do not use this protection. In theory a folder swapped mid-listing could expose names, but never contents.

## Errors

| Message | Meaning |
|---|---|
| `Access denied: path is not allowed` | Outside the roots, protected, a symlink escape or loop, or swapped during the read |
| `Use an absolute path when several tool roots are configured` | Relative path with several roots |
| `Path not found` / `File not found` | Nothing exists there |
| `Path is not a regular file` | A folder or special file where `file.read_lines`, a Markdown tool, or `file.edit` needs a file |
| `File is not UTF-8 text` | Binary data or another encoding |
| `File is larger than the 10 MiB limit` | Too large for `file.read_lines` or the Markdown tools, or for `file.edit` before or after the edit |
| `Query must be a single line` | The search text contains a line break |
| `Invalid cursor`, `Cursor does not belong to this request`, `Cursor is past the end of the results` | A cursor that is malformed, reused with a different request, or too far |
| `Heading not found` | `md.section` found no heading with that text |
| `Heading must contain text` | The `md.section` heading is only whitespace |
| `File has more than 20,000 headings` | Too many headings for the Markdown tools |
| `Not a Git working tree` | The folder is not inside a Git working tree within the tool root, or its Git data is outside the roots or in a protected folder |
| `Repository is owned by another user` | Git's ownership check refused the repository |
| `Repository layout is not supported` | The repository uses alternate object stores, its Git directory holds a symlink or special file, or it has more than 100,000 entries |
| `Invalid revision` | The revision does not follow the grammar above |
| `Revision not found` | No commit has that name |
| `Use a file path relative to the repository` | A Git `file` argument is absolute, escapes, or is malformed |
| `File not found at that revision` / `Path is not a file at that revision` | `git.show` with `file` |
| `Use staged or revisions, not both` / `to_revision needs from_revision` | Conflicting `git.diff` arguments |
| `Git timed out` / `Git command failed` / `Git 2.32 or newer is not available` | Git did not finish, failed, or is missing |
| `This tool requires a project-scoped run` / `This tool requires a DaveLLM run` | A native tool was called outside a suitable run |
| `The project for this run is not available` | The run's user does not own the project, or it no longer exists |
| `The BRAIN revision captured for this run is not available` | The captured revision is gone or does not match |
| `Artifact not found` | No artifact with that identifier in the run's project |
| `Native tool failed` | Any other native-tool failure; details stay out of the model's view |
| `old_text was not found in the file; nothing was written` | `file.edit` found no occurrence |
| `old_text was found N times, not the expected M; nothing was written` | `file.edit` found a different count than `expected_count` |
| `old_text and new_text are the same` | The `file.edit` would change nothing |
| `The file changed while the edit was being prepared; nothing was written` | The file was changed, replaced, or swapped for a symlink just before the rename |
| `File has more than one hard link` | `file.edit` refuses files with several names |
| `The edit could not be written` | The temporary file or the rename failed; nothing was written |
| `Editing files is not supported on this platform` | The platform lacks descriptor-relative open, rename, or unlink |
| `File tool failed` | Any other failure; details stay out of the model's view |

Every result is kept under 48 KiB, below DaveHarness's 64 KiB per-result budget. When the limit is reached, the page ends early and says so through `truncated`, `next_cursor`, or `next_start_line`.

## Evaluation

`scripts/evaluate_live_daveharness.py --extended-tools` also registers these tools and runs thirteen extra cases: discovery, long-document paging, a blocked instruction, Markdown section navigation, Git change inspection, Git history, project context, chat recall, cluster status, an approved edit, a rejected edit, an approved edit outside the root, and an injected instruction to edit. See [H9 live evaluation](DAVEHARNESS_H9_LIVE_EVALUATION.md).
