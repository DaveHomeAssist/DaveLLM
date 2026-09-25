# DaveLLM tools

DaveLLM offers tools to Ollama models through the in-process DaveHarness registry. All tools are off by default. This page covers the settings, the qualified built-in tools, and the extended file tools added in PR-02.

## Settings

| Setting | Default | Effect |
|---|---|---|
| `DAVE_ENABLE_TOOLS` | `false` | Turns on the tool registry and the tool routes. Nothing below works without it. |
| `DAVE_TOOL_ROOTS` | `[]` | JSON array of absolute folders. Every file tool stays inside these roots. |
| `DAVE_ENABLE_SHELL_TOOL` | `false` | Second opt-in for `shell.exec`. |
| `DAVE_ENABLE_EXTENDED_TOOLS` | `false` | Adds `file.list`, `file.search`, and `file.read_lines`. Honored only when `DAVE_ENABLE_TOOLS` is also on. |

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

All three are read-only: permission `read_files`, no approval, bounded cancellation, and a 10-second timeout. Optional arguments may be omitted or sent as `null` for their default. Unknown arguments and wrong types are rejected by the schema.

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

## Paths

- **Input.** An absolute path must be inside a tool root. A relative path is anchored at the tool root when exactly one root is configured. With several roots, relative paths are refused with "Use an absolute path when several tool roots are configured".
- **Output.** Paths are always relative to the deepest tool root that contains them, so a path shown by one tool can be passed to another. Absolute paths never appear in results or errors.

## Protected paths

Extended tools refuse and hide environment files, private keys and certificates, and SSH, AWS, and GnuPG folders. The exact patterns are `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_rsa*`, `*.p12`, `.ssh`, `.aws`, and `.gnupg`.

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
| `Path is not a regular file` | A folder or special file where `file.read_lines` needs a file |
| `File is not UTF-8 text` | Binary data or another encoding |
| `File is larger than the 10 MiB limit` | Too large for `file.read_lines` |
| `Query must be a single line` | The search text contains a line break |
| `Invalid cursor`, `Cursor does not belong to this request`, `Cursor is past the end of the results` | A cursor that is malformed, reused with a different request, or too far |
| `File tool failed` | Any other failure; details stay out of the model's view |

Every result is kept under 48 KiB, below DaveHarness's 64 KiB per-result budget. When the limit is reached, the page ends early and says so through `truncated`, `next_cursor`, or `next_start_line`.

## Evaluation

`scripts/evaluate_live_daveharness.py --extended-tools` also registers these tools and runs three extra cases: discovery, long-document paging, and a blocked instruction. See [H9 live evaluation](DAVEHARNESS_H9_LIVE_EVALUATION.md).
