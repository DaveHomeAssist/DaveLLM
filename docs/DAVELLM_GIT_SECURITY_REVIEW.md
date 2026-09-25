# Security review: read-only Git tools

This records the independent security review of the PR-04 Git tools (`git.status`, `git.diff`, `git.log`, `git.show` in `davellm_git.py`), which gates the next tool set (Gate B in the plan). The reviewer worked from `main` at `fcf4a0f`, in an isolated copy, and tried to break each property below with hostile repositories inside a tool root.

| Item | Value |
|---|---|
| Date | 2026-09-25 |
| Scope | `davellm_git.py` as merged in PR-04, plus the shared path and read helpers it uses from `davellm_files.py` |
| Environment | Git 2.43.0, Python 3.11.15, Linux |
| First verdict | **Failed**: one medium and one low finding, both on the same line |
| Re-review verdict | **Failed**: the first fix held, and four findings from the original PR-04 code were new: two high and two low |
| Status | G-1, G-2, R-1, R-2, and R-3 are fixed in the change that adds this file. R-4 is accepted and documented below. |

## Properties tested

1. No program other than Git runs.
2. Nothing is written.
3. Nothing outside a tool root is read or revealed.
4. Protected paths never appear in results.
5. Model arguments never become Git options.
6. Memory, output, and time stay bounded.

## Findings

| ID | Property | Severity | Location | Status |
|---|---|---|---|---|
| G-1 | 6, bounds | Medium | `open_repository`, the alternates check | Fixed |
| G-2 | 3, disclosure | Low | the same line | Fixed |
| R-1 | 3, disclosure | High | the alternates line rule | Fixed |
| R-2 | 3, disclosure | High | admission checked four locations but not what was inside them | Fixed |
| R-3 | 3, disclosure | Low | the location check's message | Fixed |
| R-4 | 3, disclosure | Low | `include.path` read during discovery | Accepted |

### G-1: unbounded read through a symlinked alternates file

Admission refused repositories whose object store names alternates. It did so by calling `exists()` and `read_text()` on `objects/info/alternates`. Both follow symlinks, and `read_text()` has no size limit. A model that can shape a repository inside the root could point that file at `/dev/zero`. The read then ran between Git processes, so the 8-second Git deadline did not apply. The 10-second DaveHarness timeout stops waiting but cannot stop a synchronous worker thread (see `deadline_abandoned`). The reviewer's reproduction grew until the process was killed for lack of memory. A named pipe in the same place blocked the worker indefinitely. All four tools were affected, because they all admit repositories the same way.

### G-2: existence check for paths outside the root

The same symlink made the answer depend on its target. A target that existed and held a line that was not a comment gave "Repository layout is not supported". A missing target let the repository through. That told the model whether a path outside the root existed, but never what it contained.

### Fix

`open_repository` now calls `_names_alternates`, which reads `objects/info/alternates` with `read_admitted_bytes`, the same reader every extended tool uses for file contents:

- Every path component is opened from `/` relative to its parent with `O_NOFOLLOW`. On platforms without descriptor-relative opens, the file is opened and then checked to be the same inode, reached without a symlink.
- The file is opened non-blocking and must be a regular file.
- At most 64 KiB is read (`ALTERNATES_MAX_BYTES`). A real alternates file is a few lines.
- Before the read, `objects/info` and the alternates file are checked without following links, so a dangling symlink cannot pass for a missing file on any platform.

A missing file means no alternates. Anything else is refused with the same message, "Repository layout is not supported": a symlinked file or `info` folder, a special file, an oversized file, or an unreadable file. The answer no longer depends on where a symlink points. A small regular file holding only comments and blank lines is still accepted.

### Tests

Three tests in `tests/test_git_tools.py` cover the fix. Each one fails on the code before the fix:

- `test_comment_only_alternates_are_admitted_and_oversized_ones_refused`
- `test_symlinked_alternates_are_refused_the_same_way_whatever_they_point_at`: an existing target, a missing target, and a missing parent folder, across all four tools, plus an `objects/info` folder that links to an existing or a missing folder. It runs with both read strategies.
- `test_special_file_alternates_are_refused_without_blocking`: a named pipe. The test releases a stuck reader, so a regression fails the test instead of hanging the suite.

## Re-review

The same reviewer then tried to break the fix. It checked both read strategies, with each attempt in a child process under a 2 GiB memory cap and a 15-second timeout. The fix held:

- **Targets tried:** `/dev/zero`, `/dev/stdin`, a character device, a named pipe, a folder, a 20 GiB sparse file, a file one byte over the limit, and symlinked or special `info` entries. All were refused within 6 ms.
- **Timing:** symlinks to existing and missing targets took the same time over 3,000 runs each.

The reviewer then asked whether Git itself could still reach outside the roots after admission. It could, in ways that were already in PR-04:

### R-1: alternates lines that only look blank

The check skipped any line that was empty after `strip()`. Git splits alternates on line feeds only and skips just empty and comment lines. A line holding a space, tab, carriage return, or vertical tab is therefore a relative path to Git. When that path was a symlink to another repository's objects, `git.show` returned that repository's file contents.

**Fix:** a line counts as an alternate unless it is empty or starts with `#`, exactly as Git reads it.

### R-2: links inside the Git directory

Admission resolved the working tree, Git directory, common directory, and object store, but Git follows symlinks inside them. With `packed-refs` and `objects/pack` linked to an outside repository, `git.log` and `git.show` returned that repository's history and files. This needed only the outside repository's path. The test fixture reproduces it with plain Git.

**Fix:** before any Git read, `_free_of_links` walks the Git directory, common directory, and object store without following links. It refuses the repository ("Repository layout is not supported") if any entry is a symlink or special file, or if there are more than 100,000 entries (`GIT_DIR_MAX_ENTRIES`). `hooks` is skipped, because hooks never run.

### R-3: `.git` pointers revealed outside Git directories

A `.git` file or symlink aimed at an outside Git directory gave "Access denied: path is not allowed". One aimed at a plain or missing folder gave "Not a Git working tree", so the difference revealed whether an outside path was a Git directory.

**Fix:** every location that fails the containment or protected-folder check now gives "Not a Git working tree", the same answer as a folder that is not a repository.

### R-4: `include.path` (accepted)

A repository's configuration can include another file. Git reads it during discovery, before any DaveLLM check can run. An outside file that exists but is not valid configuration makes discovery fail ("Not a Git working tree"), while a missing file is ignored. That reveals whether a readable outside file exists, never what it holds.

Refusing includes would not close this, because the include is read during discovery itself. Closing it would mean reimplementing Git's repository discovery before running Git. The finding is accepted as low severity and documented here. Included configuration cannot run programs or reach the network, because the forced settings and filter overrides apply to it as well.

### Tests for the re-review

Each test fails on the code before these fixes:

- `test_alternates_lines_that_only_look_blank_are_alternates`: space, tab, carriage return, and vertical tab.
- `test_links_inside_the_git_directory_are_refused`, across all four tools, with no outside text in any result. It covers:
  - linked `packed-refs` plus a linked pack folder
  - a linked loose object
  - a linked ref
  - a linked index
  - a named pipe in the Git directory
- `test_links_in_hooks_are_ignored_and_huge_git_directories_are_refused`.
- `test_pointers_outside_the_root_answer_like_a_plain_folder`: `.git` files and symlinks aimed at outside Git directories, a plain folder, and a missing path, all answering "Not a Git working tree".

## Attempts that did not succeed

The reviewer confirmed these defenses held:

- **Programs.** Clean, smudge, and process filters (including one brought in through `include.path`), external diff and diff drivers, text converters, fsmonitor, pagers, askpass and credential helpers, GPG, hooks, SSH commands, and trace2. Injected parent environment variables were ignored. No planted program ran.
- **Writes.** A repository `trace2.eventTarget` inside the root created nothing. Status took no lock, and the index timestamp did not change.
- **Reads outside the root.** A `.git` file or symlink pointing outside, `core.worktree` outside, a symlinked object store, alternates with real outside content, and a symlinked repository were all refused. Parent `GIT_DIR`, `GIT_WORK_TREE`, and `GIT_OBJECT_DIRECTORY` were ignored.
- **Network.** Promisor lazy fetches and `ext::` transports were refused.
- **Protected paths.** `.env`, `.ssh`, key files, and certificates stayed out of status, diff, and show, and were refused as a `file` argument.
- **Arguments.** Leading dashes, `--output=`, reflog and peel syntax, ranges, `:/text`, pathspec magic, shell metacharacters, line breaks, and oversized values were all rejected. Revisions resolve with `--end-of-options`, and files are passed as literal pathspecs after `--`.
- **Bounds.** Output caps, the 8-second deadline, and killing the process group, including a background child of Git, all held.

## Caveat

The review ran as root. Git's own ownership check does not refuse repositories owned by other users when it runs as root, so the "Repository is owned by another user" refusal was not exercised. No repository test covers it either, because creating a repository owned by another user needs root. The refusal relies on Git's check. The runner never sets `safe.directory`, and Git ignores that setting in repository configuration.
