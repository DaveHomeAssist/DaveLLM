"""Markdown heading parsing and section selection for md.outline and md.section.

``parse_headings`` is the one heading parser both tools use (and later editing
tools can reuse), so they always agree on where headings and sections are. It
is a small, line-by-line reader of the CommonMark subset that matters for
navigation, linear in the size of the file and free of regular expressions:

- ATX headings, ``#`` to ``######`` after at most three spaces and followed by
  a space, tab, or end of line. A closing run of ``#`` is dropped when a space
  or tab precedes it. Inline Markdown in the text is kept as written.
- Setext headings, a ``=`` (level 1) or ``-`` (level 2) underline directly
  under a paragraph of plain text lines. The heading text is the paragraph's
  lines joined by spaces, and the heading spans the paragraph and underline.
- Fenced code, three or more backticks or tildes after at most three spaces,
  closed only by the same character repeated at least as often. Nothing inside
  a fence counts, and an unclosed fence runs to the end of the file.
- Lines indented four or more columns are never headings or fences.
- A YAML front-matter block at the very start of the file is skipped, so its
  closing ``---`` is not taken for a heading underline.

Container blocks are not parsed: a ``#`` line after ``>`` or a list marker is
never a heading, while one inside an HTML block still is. Files with more than
20,000 headings are refused, which keeps the worst case bounded.

File access, the secret denylist, size limits, the output budget, and safe
error messages all come from davellm_files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from davellm_files import (
    NOT_TEXT, READ_MAX_FILE_BYTES, READ_MAX_LINE_CHARS, READ_MAX_LINES, FileToolError, _fitting,
    _option, decode_text, deepest_root, read_admitted_bytes, relative_display, split_lines,
)


MARKDOWN_MAX_HEADINGS = 20_000  # per file; more is refused rather than parsed
OUTLINE_DEFAULT_HEADINGS = 200
OUTLINE_MAX_HEADINGS = 500
SECTION_MAX_HEADING_CHARS = 500
SECTION_MAX_LINES = READ_MAX_LINES
HEADING_TEXT_CHARS = 300
HEADING_NOT_FOUND = "Heading not found"
EMPTY_HEADING = "Heading must contain text"
TOO_MANY_HEADINGS = f"File has more than {MARKDOWN_MAX_HEADINGS:,} headings"


@dataclass(frozen=True)
class Heading:
    text: str
    level: int
    line: int  # first source line; for Setext, the paragraph's first line
    source_end_line: int  # the underline for Setext, otherwise ``line``
    body_start_line: int
    breadcrumb: tuple[str, ...]  # ancestor heading texts, then this heading's


def _indent(line: str) -> int:
    """Leading indentation in columns; a tab advances to the next multiple of four."""
    columns = 0
    for char in line:
        if char == " ":
            columns += 1
        elif char == "\t":
            columns += 4 - columns % 4
        else:
            break
    return columns


# The helpers below take ``body``, a line with its (at most three) leading spaces removed.


def _atx(body: str) -> tuple[int, str] | None:
    level = len(body) - len(body.lstrip("#"))
    rest = body[level:]
    if not 1 <= level <= 6 or (rest and rest[0] not in " \t"):
        return None
    content = rest.strip(" \t")
    closed = content.rstrip("#")
    if not closed:
        content = ""
    elif closed != content and closed[-1] in " \t":
        content = closed.rstrip(" \t")
    return level, content


def _fence_open(body: str) -> tuple[str, int] | None:
    char = body[0]  # the caller has checked for a backtick or tilde
    length = len(body) - len(body.lstrip(char))
    # A backtick fence's info string may not contain a backtick (that is inline code).
    if length < 3 or (char == "`" and "`" in body[length:]):
        return None
    return char, length


def _fence_close(line: str, char: str, length: int) -> bool:
    if _indent(line) > 3:
        return False
    body = line.lstrip(" ")
    run = len(body) - len(body.lstrip(char))
    return run >= length and not body[run:].strip(" \t")


def _setext_level(body: str) -> int | None:
    underline = body.rstrip(" \t")
    if not underline or underline[0] not in "=-" or underline.strip(underline[0]):
        return None
    return 1 if underline[0] == "=" else 2


def _thematic_break(body: str) -> bool:
    marks = body.replace(" ", "").replace("\t", "")
    return len(marks) >= 3 and marks[0] in "-*_" and not marks.strip(marks[0])


_MAY_NOT_BE_PLAIN = frozenset("<>-*+0123456789")  # _plain is True for any other first character


def _plain(body: str) -> bool:
    """Paragraph text that is not a list item, block quote, or HTML."""
    if body[:1] in (">", "<"):
        return False
    if body[:1] in ("-", "*", "+") and body[1:2] in (" ", "\t", ""):
        return False
    digits = len(body) - len(body.lstrip("0123456789"))
    return not (1 <= digits <= 9 and body[digits:digits + 1] in (".", ")")
                and body[digits + 1:digits + 2] in (" ", "\t", ""))


def _front_matter_end(lines: Sequence[str]) -> int:
    if not lines or lines[0].rstrip(" \t") != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].rstrip(" \t") in ("---", "..."):
            return index + 1
    return 0


def parse_headings(lines: Sequence[str], max_headings: int = MARKDOWN_MAX_HEADINGS) -> list[Heading]:
    """Every heading in ``lines`` (as split by ``split_lines``), in source order.

    A file with more than ``max_headings`` headings is refused, which bounds the
    work and memory of both tools on hostile input.
    """
    found: list[tuple[str, int, int, int]] = []
    fence: tuple[str, int] | None = None
    paragraph_start: int | None = None  # index of the open paragraph's first line
    plain = False
    for index in range(_front_matter_end(lines), len(lines)):
        line = lines[index]
        first = line[:1]
        if fence is not None:
            if first in (" ", fence[0]) and _fence_close(line, *fence):
                fence = None
            continue
        if not first:
            paragraph_start = None
            continue
        if first in (" ", "\t"):
            if not line.strip(" \t"):
                paragraph_start = None
                continue
            if _indent(line) > 3:  # indented code, or a paragraph continuation line
                continue
            body = line.lstrip(" ")
        else:
            body = line
        lead = body[0]
        heading: tuple[str, int, int, int] | None = None
        if lead in ("`", "~"):
            opened = _fence_open(body)
            if opened is not None:
                fence, paragraph_start = opened, None
                continue
        elif lead == "#":
            atx = _atx(body)
            if atx is not None:
                heading = (atx[1], atx[0], index + 1, index + 1)
        elif lead in ("=", "-") and paragraph_start is not None and plain:
            underline = _setext_level(body)
            if underline is not None:
                text = " ".join(part.strip(" \t") for part in lines[paragraph_start:index])
                heading = (text, underline, paragraph_start + 1, index + 1)
        if heading is not None:
            found.append(heading)
            if len(found) > max_headings:
                raise FileToolError(TOO_MANY_HEADINGS)
            paragraph_start = None
        elif lead in ("-", "*", "_") and _thematic_break(body):
            paragraph_start = None
        elif paragraph_start is None:
            paragraph_start, plain = index, lead not in _MAY_NOT_BE_PLAIN or _plain(body)
        elif plain:
            plain = lead not in _MAY_NOT_BE_PLAIN or _plain(body)
    headings: list[Heading] = []
    parents: list[tuple[int, str]] = []
    for text, level, line_number, end in found:
        while parents and parents[-1][0] >= level:
            parents.pop()
        breadcrumb = tuple(parent for _, parent in parents) + (text,)
        parents.append((level, text))
        headings.append(Heading(text, level, line_number, end, end + 1, breadcrumb))
    return headings


def normalize_heading(text: str) -> str:
    """Case-insensitive comparison key: surrounding and repeated whitespace collapse."""
    return " ".join(text.split()).casefold()


def _without_atx_marker(text: str) -> str | None:
    stripped = text.strip()
    hashes = len(stripped) - len(stripped.lstrip("#"))
    if 1 <= hashes <= 6 and stripped[hashes:hashes + 1] in (" ", "\t"):
        return stripped[hashes:]
    return None


def find_headings(headings: Sequence[Heading], requested: str) -> list[int]:
    """Indexes of every heading whose text equals ``requested`` after normalization.

    Matching is exact, never partial or fuzzy. A request written with its ATX
    marker, such as ``## Setup``, is tried without the marker only when the text
    as written matches nothing.
    """
    wanted = normalize_heading(requested)
    if not wanted:
        raise FileToolError(EMPTY_HEADING)
    matches = [index for index, heading in enumerate(headings) if normalize_heading(heading.text) == wanted]
    unmarked = _without_atx_marker(requested)
    if matches or unmarked is None:
        return matches
    wanted = normalize_heading(unmarked)
    return [index for index, heading in enumerate(headings) if normalize_heading(heading.text) == wanted]


def section_bounds(
    headings: Sequence[Heading], index: int, include_subsections: bool, total_lines: int,
) -> tuple[int, int]:
    """Inclusive body lines of heading ``index``; the range is empty when end < start.

    The body runs to the line before the next heading of the same or a higher
    level, or, without subsections, before the next heading of any level.
    """
    target = headings[index]
    end = total_lines
    for later in headings[index + 1:]:
        if not include_subsections or later.level <= target.level:
            end = later.line - 1
            break
    return target.body_start_line, end


def _load(
    arguments: Mapping[str, Any], resolve: Callable[[str], Path], roots: Sequence[Path],
) -> tuple[str, list[str]]:
    target = resolve(arguments["path"])
    display = relative_display(target, deepest_root(target, roots))
    text = decode_text(read_admitted_bytes(target, READ_MAX_FILE_BYTES))
    if text is None:
        raise FileToolError(NOT_TEXT)
    return display, split_lines(text)


def _heading_entry(heading: Heading, *, breadcrumb: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "text": heading.text[:HEADING_TEXT_CHARS], "level": heading.level, "line": heading.line,
    }
    if len(heading.text) > HEADING_TEXT_CHARS:
        entry["text_truncated"] = True
    if heading.source_end_line != heading.line:
        entry["source_end_line"] = heading.source_end_line
    if breadcrumb:
        entry["breadcrumb"] = [item[:HEADING_TEXT_CHARS] for item in heading.breadcrumb]
    return entry


def markdown_outline(
    arguments: Mapping[str, Any], *, resolve: Callable[[str], Path], roots: Sequence[Path],
) -> dict[str, Any]:
    """md.outline: headings in source order, stopping cleanly at the output budget."""
    limit = _option(arguments, "max_headings", OUTLINE_DEFAULT_HEADINGS)
    display, lines = _load(arguments, resolve, roots)
    headings = parse_headings(lines)
    entries = [_heading_entry(heading, breadcrumb=False) for heading in headings[:limit]]
    envelope = {
        "path": display, "headings": [], "heading_count": 0, "total_headings": len(headings),
        "total_lines": len(lines), "truncated": True, "next_start_line": len(lines),
    }
    kept = _fitting(entries, envelope)
    truncated = kept < len(headings)
    return {
        **envelope, "headings": entries[:kept], "heading_count": kept, "truncated": truncated,
        "next_start_line": headings[kept].line if truncated else None,
    }


def markdown_section(
    arguments: Mapping[str, Any], *, resolve: Callable[[str], Path], roots: Sequence[Path],
) -> dict[str, Any]:
    """md.section: one section's lines, or every candidate when the heading is ambiguous."""
    requested = arguments["heading"]
    include_subsections = _option(arguments, "include_subsections", True)
    display, lines = _load(arguments, resolve, roots)
    headings = parse_headings(lines)
    matches = find_headings(headings, requested)
    if not matches:
        raise FileToolError(HEADING_NOT_FOUND)
    if len(matches) > 1:
        candidates = [_heading_entry(headings[index], breadcrumb=True) for index in matches]
        envelope = {"path": display, "heading": requested, "ambiguous": True,
                    "match_count": len(matches), "matches": [], "truncated": True}
        kept = _fitting(candidates, envelope)
        return {**envelope, "matches": candidates[:kept], "truncated": kept < len(candidates)}
    index = matches[0]
    entry = _heading_entry(headings[index], breadcrumb=True)
    start, end = section_bounds(headings, index, include_subsections, len(lines))
    body = lines[start - 1:end] if end >= start else []
    window = body[:SECTION_MAX_LINES]
    shown = [line[:READ_MAX_LINE_CHARS] for line in window]
    heading_fields = {"heading": entry.pop("text"), **entry}
    envelope = {
        "path": display, "ambiguous": False, **heading_fields,
        "include_subsections": include_subsections,
        "content_start_line": start if body else None, "content_end_line": end if body else None,
        "lines": [], "line_count": 0, "truncated": True, "next_start_line": start, "cut_lines": [],
    }
    kept = _fitting(shown, envelope)
    cut = [start + offset for offset, line in enumerate(window[:kept]) if len(line) > READ_MAX_LINE_CHARS]
    result = {
        **envelope, "lines": shown[:kept], "line_count": kept,
        "truncated": kept < len(body) or bool(cut),
        "next_start_line": start + kept if kept < len(body) else None,
    }
    del result["cut_lines"]
    if cut:
        result["cut_lines"] = cut
    return result
