# Security review: read-only Git tools

This records the independent security review of the PR-04 Git tools (`git.status`, `git.diff`, `git.log`, `git.show` in `davellm_git.py`), which gates the next tool set (Gate B in the plan). The reviewer worked from `main` at `fcf4a0f`, in an isolated copy, and tried to break each property below with hostile repositories inside a tool root.

| Item | Value |
|---|---|
| Date | 2026-09-25 |
| Scope | `davellm_git.py` as merged in PR-04, plus the shared path and read helpers it uses from `davellm_files.py` |
| Environment | Git 2.43.0, Python 3.11.15, Linux |
| First verdict | **Failed**: one medium and one low finding, both on the same line |
| Status | Both findings are fixed in the change that adds this file |

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
