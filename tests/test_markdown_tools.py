"""PR-03: md.outline and md.section behind DAVE_ENABLE_EXTENDED_TOOLS.

Both tools read through the PR-02 extended path boundary and share one heading
parser, davellm_markdown.parse_headings. Tests drive them through DaveHarness's
validated dispatcher against disposable roots, usually the hostile tree from
tests/hostile_fs.py.
"""

import asyncio
import json
import os
import threading
import time

import pytest

import davellm_files
import davellm_markdown
from daveharness import run_tool
from daveharness.registry import DEFAULT_TOOL_TIMEOUT_SECONDS
from davellm_files import AMBIGUOUS_RELATIVE_PATH, OUTPUT_BUDGET_BYTES, PATH_NOT_ALLOWED, split_lines
from davellm_markdown import (
    EMPTY_HEADING, HEADING_NOT_FOUND, MARKDOWN_MAX_HEADINGS, TOO_MANY_HEADINGS, parse_headings,
)
from hostile_fs import (
    BINARY_FILE, CHAIN_LINK, INTERNAL_LINK, LOOP_LINK, OUTSIDE_DIR_LINK, OUTSIDE_LINK,
    SECRET_DIR_LINK, SECRET_DIRS, SECRET_FILE_LINK, SECRET_FILES, sentinel,
)
from tool_contract import PathToolContract, assert_path_contract, run_path_contract


MARKDOWN_TOOLS = {"md.outline", "md.section"}
EXTENDED_TOOLS = {"file.list", "file.search", "file.read_lines"} | MARKDOWN_TOOLS

GUIDE = "\n".join([
    "# Guide",             # 1
    "",
    "Intro text.",
    "",
    "## Install",          # 5
    "",
    "Install steps.",
    "",
    "### Linux",           # 9
    "",
    "apt install dave",
    "",
    "#### Notes",          # 13
    "",
    "Linux notes.",
    "",
    "### macOS",           # 17
    "",
    "brew install dave",
    "",
    "## Configure",        # 21
    "",
    "```bash",
    "# not a heading",
    "## Install",
    "```",
    "",
    "~~~",
    "# Tilde fake",
    "~~~",
    "",
    "Configure text.",
    "",
    "# Appendix",          # 34
    "",
    "Last words.",         # 36
]) + "\n"
GUIDE_HEADINGS = [
    ("Guide", 1, 1), ("Install", 2, 5), ("Linux", 3, 9), ("Notes", 4, 13), ("macOS", 3, 17),
    ("Configure", 2, 21), ("Appendix", 1, 34),
]
DUPLICATES = "\n".join([
    "# Linux", "", "## Setup", "", "Linux setup.", "",
    "# macOS", "", "## Setup", "", "macOS setup.",
]) + "\n"
SYNTAX = "\n".join([
    "---",
    "title: front matter",
    "---",
    "Title One",
    "=========",
    "",
    "   ### Three spaces ###",
    "    # Indented code",
    "```text",
    "# Fenced",
    "```",
    "Sub title",
    "spanning lines",
    "---",
    "- list item",
    "---",
    "~~~~",
    "## Tilde fenced",
    "~~~",
    "~~~~~",
    "## Closing #s ##",
    "Para",
    "",
    "---",
    "#hashtag",
    "####### seven",
    "## Setup",
    "### Setup",
]) + "\n"


@pytest.fixture
def extended(router_factory, monkeypatch, hostile_tree):
    """Load app with tools and extended tools on, rooted at the hostile tree by default."""
    def load(*roots, extended="true", tools=True):
        monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", extended)
        router, _, _ = router_factory(
            tools=tools, tool_roots=[str(item) for item in roots or [hostile_tree.root]],
        )
        return router

    return load


@pytest.fixture
def router(extended, hostile_tree):
    for name, text in (("guide.md", GUIDE), ("dupes.md", DUPLICATES), ("syntax.md", SYNTAX)):
        hostile_tree.at(name).write_text(text, encoding="utf-8")
    return extended()


def run(router, name, **arguments):
    return asyncio.run(run_tool(name, arguments, registry=router.TOOL_REGISTRY))


def ok(router, name, **arguments):
    execution = run(router, name, **arguments)
    assert execution.status == "success", execution.error
    return json.loads(execution.result)


def error(router, name, **arguments):
    execution = run(router, name, **arguments)
    assert execution.status != "success", execution.result
    return execution.status, execution.error


def outline(router, path, **arguments):
    return ok(router, "md.outline", path=path, **arguments)


def section(router, path, heading, **arguments):
    return ok(router, "md.section", path=path, heading=heading, **arguments)


def triples(result):
    return [(item["text"], item["level"], item["line"]) for item in result["headings"]]


def write(tree, name, text):
    target = tree.at(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def file_lines(tree, name):
    return split_lines(tree.at(name).read_text(encoding="utf-8"))


def body(result):
    return (result["content_start_line"], result["content_end_line"], result["lines"])


# Registration ---------------------------------------------------------------------------------

def test_markdown_tools_register_only_with_both_flags(extended):
    assert not MARKDOWN_TOOLS & set(extended(extended="true", tools=False).TOOL_REGISTRY.public_catalog())
    assert not MARKDOWN_TOOLS & set(extended(extended="false").TOOL_REGISTRY.public_catalog())
    router = extended()
    assert EXTENDED_TOOLS <= {definition.name for definition in router.extended_tool_definitions()}
    for registry in (router.TOOL_REGISTRY, router.HARNESS_REGISTRY):
        assert EXTENDED_TOOLS <= set(registry.public_catalog())


def test_markdown_definitions_are_read_only_synchronous_and_bounded(extended):
    router = extended()
    for name in MARKDOWN_TOOLS:
        definition = router.TOOL_REGISTRY.get(name)
        assert (definition.permission, definition.approval_required) == ("read_files", False)
        assert (definition.cancellation, definition.async_handler, definition.context_handler) == (
            "bounded", False, False)
        assert definition.timeout_seconds == DEFAULT_TOOL_TIMEOUT_SECONDS
        assert definition.parameters["additionalProperties"] is False
        assert definition.parameters["properties"]["path"]["maxLength"] == 4096


def test_markdown_schemas_reject_bad_arguments_and_accept_null_defaults(router):
    for arguments in ({}, {"path": ""}, {"path": 3}, {"path": "guide.md", "extra": 1},
                      {"path": "guide.md", "max_headings": 0}, {"path": "guide.md", "max_headings": 501},
                      {"path": "guide.md", "max_headings": 1.5}, {"path": "guide.md", "max_headings": "5"}):
        assert error(router, "md.outline", **arguments)[0] == "validation_error", arguments
    for arguments in ({"path": "guide.md"}, {"heading": "Install"}, {"path": "guide.md", "heading": ""},
                      {"path": "guide.md", "heading": "x" * 501}, {"path": "guide.md", "heading": 5},
                      {"path": "guide.md", "heading": None},
                      {"path": "guide.md", "heading": "Install", "include_subsections": "yes"},
                      {"path": "guide.md", "heading": "Install", "occurrence": 1}):
        assert error(router, "md.section", **arguments)[0] == "validation_error", arguments
    assert outline(router, "guide.md", max_headings=None) == outline(router, "guide.md")
    assert section(router, "guide.md", "Install", include_subsections=None) == section(
        router, "guide.md", "Install")
    assert error(router, "md.section", path="guide.md", heading="   ")[1] == EMPTY_HEADING


# md.outline -----------------------------------------------------------------------------------

def test_outline_reads_atx_levels_one_to_six(extended, hostile_tree):
    write(hostile_tree, "levels.md", "".join(f"{'#' * level} Level {level}\n" for level in range(1, 7)))
    result = outline(extended(), "levels.md")
    assert triples(result) == [(f"Level {level}", level, level) for level in range(1, 7)]


def test_outline_allows_zero_to_three_leading_spaces(extended, hostile_tree):
    write(hostile_tree, "spaces.md", "# Zero\n # One\n  ## Two\n   ### Three\n")
    assert triples(outline(extended(), "spaces.md")) == [
        ("Zero", 1, 1), ("One", 1, 2), ("Two", 2, 3), ("Three", 3, 4)]


def test_outline_ignores_four_space_and_tab_indented_headings(extended, hostile_tree):
    write(hostile_tree, "indented.md", "# Real\n\n    # Four spaces\n\t## Tab\n  \t### Mixed\n")
    assert triples(outline(extended(), "indented.md")) == [("Real", 1, 1)]


def test_outline_removes_closing_hashes_and_keeps_inline_markdown(extended, hostile_tree):
    write(hostile_tree, "closing.md", "\n".join([
        "# Title #", "## Title ##   ", "### Title #####", "#### Title#", "##### #####",
        "###### Title \\#", "# *Bold* `code` [link](x) #", "#hashtag", "####### seven", "#",
    ]) + "\n")
    assert triples(outline(extended(), "closing.md")) == [
        ("Title", 1, 1), ("Title", 2, 2), ("Title", 3, 3), ("Title#", 4, 4), ("", 5, 5),
        ("Title \\#", 6, 6), ("*Bold* `code` [link](x)", 1, 7), ("", 1, 10)]


def test_outline_reports_source_line_numbers_and_counts(router):
    result = outline(router, "guide.md")
    assert triples(result) == GUIDE_HEADINGS
    assert (result["path"], result["heading_count"], result["total_headings"], result["total_lines"]) == (
        "guide.md", 7, 7, 36)
    assert (result["truncated"], result["next_start_line"]) == (False, None)


def test_outline_keeps_source_order_even_when_levels_jump(extended, hostile_tree):
    write(hostile_tree, "order.md", "### Deep first\n# Top\n###### Six\n## Two\n")
    assert triples(outline(extended(), "order.md")) == [
        ("Deep first", 3, 1), ("Top", 1, 2), ("Six", 6, 3), ("Two", 2, 4)]


def test_outline_levels_give_the_hierarchy_that_section_breadcrumbs_report(router):
    headings = outline(router, "guide.md")["headings"]
    stack = []
    for entry in headings:
        while stack and stack[-1][0] >= entry["level"]:
            stack.pop()
        stack.append((entry["level"], entry["text"]))
        expected = [text for _, text in stack]
        assert section(router, "guide.md", entry["text"])["breadcrumb"] == expected
    assert "breadcrumb" not in headings[0]


def test_outline_reads_setext_level_one(router):
    first = outline(router, "syntax.md")["headings"][0]
    assert first == {"text": "Title One", "level": 1, "line": 4, "source_end_line": 5}


def test_outline_reads_setext_level_two_and_rejects_non_paragraph_underlines(router):
    result = outline(router, "syntax.md")
    setext = [item for item in result["headings"] if item["level"] == 2 and "source_end_line" in item]
    assert setext == [{"text": "Sub title spanning lines", "level": 2, "line": 12, "source_end_line": 14}]
    texts = [item["text"] for item in result["headings"]]
    assert "- list item" not in texts and "Para" not in texts and "title: front matter" not in texts
    assert all("source_end_line" not in item for item in result["headings"] if item["text"] != texts[0]
               and item["text"] != "Sub title spanning lines")


def test_outline_ignores_backtick_fenced_headings(extended, hostile_tree):
    write(hostile_tree, "backtick.md", "# Before\n```\n# Fake\n## Fake two\n```\n# After\n")
    assert triples(outline(extended(), "backtick.md")) == [("Before", 1, 1), ("After", 1, 6)]


def test_outline_ignores_tilde_fenced_headings(extended, hostile_tree):
    write(hostile_tree, "tilde.md", "# Before\n~~~\n# Fake\n```\n# Still fake\n~~~\n# After\n")
    assert triples(outline(extended(), "tilde.md")) == [("Before", 1, 1), ("After", 1, 7)]


def test_outline_accepts_a_longer_closing_fence(extended, hostile_tree):
    write(hostile_tree, "longer.md", "````\n# Fake\n`````\n# After\n  ~~~\n# Fake\n   ~~~~~~\n# Last\n")
    assert triples(outline(extended(), "longer.md")) == [("After", 1, 4), ("Last", 1, 8)]


def test_outline_shorter_or_different_closing_fences_do_not_close(extended, hostile_tree):
    write(hostile_tree, "shorter.md", "\n".join([
        "````", "```", "# Fake one", "~~~~", "# Fake two", "    ````", "# Fake three", "````",
        "# After", "~~~", "# Unclosed fence runs to the end",
    ]) + "\n")
    assert triples(outline(extended(), "shorter.md")) == [("After", 1, 9)]


def test_outline_handles_fence_info_strings(extended, hostile_tree):
    write(hostile_tree, "info.md", "\n".join([
        '```python title="x"',   # opens
        "# Fake",
        "```js",                 # an info string cannot close a fence
        "# Fake two",
        "```",
        "~~~ info with ``` backticks",  # tilde info strings may hold backticks
        "# Fake three",
        "~~~",
        "`` `inline` ``",        # two backticks are not a fence
        "# After",
        "``` a`b",               # a backtick in a backtick info string is inline code, not a fence
        "# Last",
    ]) + "\n")
    assert triples(outline(extended(), "info.md")) == [("After", 1, 10), ("Last", 1, 12)]


def test_outline_of_an_empty_file(extended, hostile_tree):
    write(hostile_tree, "empty.md", "")
    assert outline(extended(), "empty.md") == {
        "path": "empty.md", "headings": [], "heading_count": 0, "total_headings": 0, "total_lines": 0,
        "truncated": False, "next_start_line": None,
    }


def test_outline_of_a_document_without_headings(extended, hostile_tree):
    result = outline(extended(), "docs/nested/deep/note.txt")
    assert (result["headings"], result["total_headings"], result["total_lines"]) == ([], 0, 1)


def test_outline_refuses_binary_input(extended):
    assert error(extended(), "md.outline", path=BINARY_FILE)[1] == davellm_files.NOT_TEXT


def test_outline_refuses_non_utf8_input(extended, hostile_tree):
    hostile_tree.at("latin1.md").write_bytes(b"# Caf\xe9\n")
    hostile_tree.at("bom16.md").write_bytes("# Title\n".encode("utf-16"))
    router = extended()
    for name in ("latin1.md", "bom16.md"):
        assert error(router, "md.outline", path=name)[1] == davellm_files.NOT_TEXT


def test_outline_refuses_secret_paths(extended, hostile_tree):
    router = extended()
    for blocked in SECRET_FILES + SECRET_DIRS + ("docs/../.env", "./.ssh/config"):
        assert error(router, "md.outline", path=blocked) == ("error", PATH_NOT_ALLOWED), blocked


def test_outline_refuses_root_escapes(extended, hostile_tree):
    router = extended()
    for escape in ("../outside/secret.txt", "docs/../../outside/notes.txt",
                   str(hostile_tree.outside / "secret.txt"), "/etc/hostname"):
        assert error(router, "md.outline", path=escape) == ("error", PATH_NOT_ALLOWED), escape


def test_outline_refuses_unsafe_symlinks(extended, hostile_tree):
    router = extended()
    for link in (OUTSIDE_LINK, f"{OUTSIDE_DIR_LINK}/notes.txt", CHAIN_LINK, LOOP_LINK, SECRET_FILE_LINK,
                 f"{SECRET_DIR_LINK}/config"):
        assert error(router, "md.outline", path=link) == ("error", PATH_NOT_ALLOWED), link
    assert outline(router, INTERNAL_LINK)["path"] == "docs/guide.md"


def test_outline_limits_returned_headings(extended, hostile_tree):
    write(hostile_tree, "many.md", "".join(f"## H{number}\n" for number in range(1, 601)))
    router = extended()
    default = outline(router, "many.md")
    assert (default["heading_count"], default["total_headings"], default["truncated"]) == (200, 600, True)
    assert default["headings"][-1]["line"] == 200 and default["next_start_line"] == 201
    most = outline(router, "many.md", max_headings=500)
    assert (most["heading_count"], most["next_start_line"]) == (500, 501)
    assert outline(router, "many.md", max_headings=3)["headings"] == [
        {"text": "H1", "level": 2, "line": 1}, {"text": "H2", "level": 2, "line": 2},
        {"text": "H3", "level": 2, "line": 3}]


def test_both_tools_refuse_files_with_too_many_headings(extended, hostile_tree):
    write(hostile_tree, "limit.md", "## H\n" * MARKDOWN_MAX_HEADINGS)
    write(hostile_tree, "flood.md", "#\n" * (MARKDOWN_MAX_HEADINGS + 1))
    router = extended()
    assert outline(router, "limit.md")["total_headings"] == MARKDOWN_MAX_HEADINGS
    assert error(router, "md.outline", path="flood.md")[1] == TOO_MANY_HEADINGS
    assert error(router, "md.section", path="flood.md", heading="x")[1] == TOO_MANY_HEADINGS


def test_outline_stops_at_the_result_budget_and_points_at_file_read_lines(extended, hostile_tree):
    text = "".join(f"## H{number} {'x' * 400}\n\nbody\n\n" for number in range(1, 501))
    write(hostile_tree, "wide.md", text)
    router = extended()
    execution = run(router, "md.outline", path="wide.md", max_headings=500)
    assert execution.status == "success"
    assert len(execution.result.encode("utf-8")) <= OUTPUT_BUDGET_BYTES
    result = json.loads(execution.result)
    kept = result["heading_count"]
    assert 0 < kept < 500 and result["truncated"] and result["total_headings"] == 500
    assert all(item["text_truncated"] and len(item["text"]) == 300 for item in result["headings"])
    assert result["next_start_line"] == 4 * kept + 1
    resumed = ok(router, "file.read_lines", path="wide.md", start_line=result["next_start_line"], max_lines=1)
    assert resumed["lines"][0].startswith(f"## H{kept + 1} ")


def test_outline_shows_only_relative_paths(extended, hostile_tree):
    router = extended()
    for requested in ("docs/guide.md", str(hostile_tree.at("docs/guide.md")), INTERNAL_LINK):
        execution = run(router, "md.outline", path=requested)
        assert json.loads(execution.result)["path"] == "docs/guide.md"
        assert str(hostile_tree.base) not in execution.result
    several = extended(hostile_tree.root, hostile_tree.at("docs"))
    assert error(several, "md.outline", path="guide.md")[1] == AMBIGUOUS_RELATIVE_PATH
    assert outline(several, str(hostile_tree.at("docs/guide.md")))["path"] == "guide.md"


def test_outline_has_no_filesystem_effects(router, hostile_tree):
    before = hostile_tree.snapshot()
    for path in ("guide.md", "syntax.md", ".env", LOOP_LINK, BINARY_FILE, "missing.md", "docs"):
        run(router, "md.outline", path=path)
    assert hostile_tree.snapshot() == before


# md.section -----------------------------------------------------------------------------------

def test_section_of_a_unique_top_level_heading(router, hostile_tree):
    result = section(router, "guide.md", "Guide")
    lines = file_lines(hostile_tree, "guide.md")
    assert (result["heading"], result["level"], result["line"], result["breadcrumb"]) == ("Guide", 1, 1, ["Guide"])
    assert body(result) == (2, 33, lines[1:33])
    assert (result["ambiguous"], result["line_count"], result["truncated"], result["next_start_line"]) == (
        False, 32, False, None)


def test_section_of_a_unique_nested_heading(router, hostile_tree):
    result = section(router, "guide.md", "Linux")
    assert (result["level"], result["line"], result["breadcrumb"]) == (3, 9, ["Guide", "Install", "Linux"])
    assert body(result) == (10, 16, file_lines(hostile_tree, "guide.md")[9:16])


def test_section_at_the_end_of_the_file(router, hostile_tree):
    result = section(router, "guide.md", "Appendix")
    assert body(result) == (35, 36, ["", "Last words."])
    assert (result["truncated"], result["next_start_line"]) == (False, None)
    write(hostile_tree, "no-newline.md", "# A\ntext\n# B\nlast")
    assert body(section(router, "no-newline.md", "B")) == (4, 4, ["last"])


def test_section_with_an_empty_body(router, hostile_tree):
    write(hostile_tree, "empty-section.md", "# Doc\n## Empty\n## Next\ntext\n")
    result = section(router, "empty-section.md", "Empty")
    assert body(result) == (None, None, [])
    assert (result["line_count"], result["truncated"], result["next_start_line"]) == (0, False, None)
    write(hostile_tree, "last-heading.md", "# Doc\ntext\n## Final")
    assert body(section(router, "last-heading.md", "Final")) == (None, None, [])


def test_section_includes_subsections_by_default(router):
    result = section(router, "guide.md", "Install")
    assert result["include_subsections"] is True
    assert (result["content_start_line"], result["content_end_line"]) == (6, 20)
    assert section(router, "guide.md", "Install", include_subsections=True) == result


def test_section_without_subsections(router, hostile_tree):
    result = section(router, "guide.md", "Install", include_subsections=False)
    assert result["include_subsections"] is False
    assert body(result) == (6, 8, ["", "Install steps.", ""])


def test_next_same_level_heading_ends_the_section(router):
    assert section(router, "guide.md", "Install")["content_end_line"] == 20  # "## Configure" is line 21
    assert section(router, "guide.md", "Linux")["content_end_line"] == 16  # "### macOS" is line 17
    assert section(router, "guide.md", "Guide")["content_end_line"] == 33  # "# Appendix" is line 34


def test_parent_level_heading_ends_a_nested_section(router):
    assert section(router, "guide.md", "Notes")["content_end_line"] == 16  # "### macOS" ends a level 4
    assert section(router, "guide.md", "macOS")["content_end_line"] == 20  # "## Configure" ends a level 3


def test_deeper_headings_stay_inside_when_subsections_are_included(router):
    lines = section(router, "guide.md", "Install")["lines"]
    assert "### Linux" in lines and "#### Notes" in lines and "### macOS" in lines
    assert "## Configure" not in lines


def test_deeper_heading_ends_the_body_when_subsections_are_excluded(router):
    lines = section(router, "guide.md", "Linux", include_subsections=False)["lines"]
    assert lines == ["", "apt install dave", ""]
    assert "#### Notes" not in lines


def test_fenced_fake_headings_do_not_end_a_section(router):
    for include in (True, False):
        result = section(router, "guide.md", "Configure", include_subsections=include)
        assert (result["content_start_line"], result["content_end_line"]) == (22, 33)
        assert "## Install" in result["lines"] and "# Tilde fake" in result["lines"]


def test_backtick_fence_inside_a_section(extended, hostile_tree):
    write(hostile_tree, "fence.md", "## Install\n```\n## Fake\n# Fake too\n```\nstill install\n## Next\n")
    router = extended()
    for include in (True, False):
        assert body(section(router, "fence.md", "Install", include_subsections=include)) == (
            2, 6, ["```", "## Fake", "# Fake too", "```", "still install"])


def test_tilde_fence_inside_a_section(extended, hostile_tree):
    write(hostile_tree, "tilde.md", "## Install\n~~~~ sh\n## Fake\n~~~\n```\n~~~~\nstill install\n## Next\n")
    router = extended()
    for include in (True, False):
        result = section(router, "tilde.md", "Install", include_subsections=include)
        assert (result["content_start_line"], result["content_end_line"]) == (2, 7)


def test_duplicate_headings_are_reported_as_ambiguous_without_a_body(router):
    result = section(router, "dupes.md", "Setup")
    assert result["ambiguous"] is True and result["match_count"] == 2
    assert {"lines", "line_count", "content_start_line", "next_start_line"}.isdisjoint(result)
    assert "Linux setup." not in json.dumps(result) and "macOS setup." not in json.dumps(result)


def test_ambiguous_candidates_carry_levels_and_lines(router):
    matches = section(router, "dupes.md", "setup")["matches"]
    assert [(item["text"], item["level"], item["line"]) for item in matches] == [("Setup", 2, 3), ("Setup", 2, 9)]


def test_breadcrumbs_distinguish_duplicate_headings(router):
    result = section(router, "dupes.md", "Setup")
    assert [item["breadcrumb"] for item in result["matches"]] == [["Linux", "Setup"], ["macOS", "Setup"]]
    assert result["heading"] == "Setup" and result["path"] == "dupes.md" and result["truncated"] is False
    mixed = section(router, "syntax.md", "SETUP")
    assert [(item["level"], item["line"]) for item in mixed["matches"]] == [(2, 27), (3, 28)]


def test_ambiguity_response_stays_within_the_result_budget(extended, hostile_tree):
    write(hostile_tree, "setups.md", "".join(f"# Parent {n} {'p' * 300}\n## Setup\n" for n in range(1, 501)))
    execution = run(extended(), "md.section", path="setups.md", heading="Setup")
    assert execution.status == "success"
    assert len(execution.result.encode("utf-8")) <= OUTPUT_BUDGET_BYTES
    result = json.loads(execution.result)
    assert result["ambiguous"] and result["match_count"] == 500 and result["truncated"]
    kept = len(result["matches"])
    assert 0 < kept < 500
    assert [item["line"] for item in result["matches"]] == [2 * n for n in range(1, kept + 1)]


def test_missing_heading_is_a_fixed_error(router):
    for missing in ("Uninstall", "Tilde fake", "not a heading", "Indented code", "Fenced"):
        for path in ("guide.md", "syntax.md"):
            assert error(router, "md.section", path=path, heading=missing) == ("error", HEADING_NOT_FOUND)


def test_heading_match_normalizes_whitespace_and_markers(router, hostile_tree):
    write(hostile_tree, "spacing.md", "#   Getting \t  Started   ##\ntext\n")
    for requested in ("Getting Started", "  getting   started ", "# Getting Started", "Getting\tStarted"):
        assert section(router, "spacing.md", requested)["heading"] == "Getting \t  Started"
    assert section(router, "guide.md", "## Install")["line"] == 5
    assert section(router, "syntax.md", "Closing #s")["line"] == 21
    assert section(router, "syntax.md", "sub title spanning lines")["line"] == 12


def test_heading_match_is_exact_but_case_insensitive(router, hostile_tree):
    assert section(router, "guide.md", "INSTALL")["line"] == 5
    assert section(router, "guide.md", "MacOS")["line"] == 17
    write(hostile_tree, "german.md", "# Straße\n")
    assert section(router, "german.md", "STRASSE")["line"] == 1


def test_heading_match_is_never_partial_or_fuzzy(router):
    for near_miss in ("Instal", "Install steps", "nstall", "Linux Notes", "Install*", "Guide Install",
                      "Apendix", "Mac OS"):
        assert error(router, "md.section", path="guide.md", heading=near_miss)[1] == HEADING_NOT_FOUND


def test_section_returns_at_most_four_hundred_lines(extended, hostile_tree):
    write(hostile_tree, "long.md", "# Long\n" + "".join(f"row {number}\n" for number in range(1, 451)) + "# End\n")
    result = section(extended(), "long.md", "Long")
    assert (result["line_count"], result["truncated"]) == (400, True)
    assert result["lines"][0] == "row 1" and result["lines"][-1] == "row 400"
    assert (result["content_start_line"], result["content_end_line"], result["next_start_line"]) == (2, 451, 402)


def test_section_stops_at_the_result_budget_and_cuts_extreme_lines(extended, hostile_tree):
    wide = "w" * (davellm_files.READ_MAX_LINE_CHARS + 500)
    write(hostile_tree, "wide.md", "# Wide\n" + f"{wide}\n" * 100)
    execution = run(extended(), "md.section", path="wide.md", heading="Wide")
    assert execution.status == "success"
    assert len(execution.result.encode("utf-8")) <= OUTPUT_BUDGET_BYTES
    result = json.loads(execution.result)
    kept = result["line_count"]
    assert 0 < kept < 100 and result["truncated"]
    assert all(len(line) == davellm_files.READ_MAX_LINE_CHARS for line in result["lines"])
    assert result["cut_lines"] == list(range(2, 2 + kept))
    assert result["next_start_line"] == 2 + kept


def test_next_start_line_continues_the_section_with_file_read_lines(extended, hostile_tree):
    write(hostile_tree, "long.md", "intro\n# Long\n" + "".join(f"row {n}\n" for n in range(1, 1001)) + "# End\n")
    router = extended()
    result = section(router, "long.md", "Long")
    collected, start = list(result["lines"]), result["next_start_line"]
    while start is not None and start <= result["content_end_line"]:
        page = ok(router, "file.read_lines", path=result["path"], start_line=start,
                  max_lines=min(400, result["content_end_line"] - start + 1))
        collected += page["lines"]
        start = page["next_start_line"]
    assert collected == [f"row {n}" for n in range(1, 1001)]


def test_section_line_range_matches_the_file(router, hostile_tree):
    lines = file_lines(hostile_tree, "guide.md")
    for text, _, _ in GUIDE_HEADINGS:
        for include in (True, False):
            result = section(router, "guide.md", text, include_subsections=include)
            start, end = result["content_start_line"], result["content_end_line"]
            assert result["lines"] == lines[start - 1:end] and result["line_count"] == end - start + 1
            assert lines[result["line"] - 1].lstrip("# ").startswith(text)
            assert start == result["line"] + 1
    setext = section(router, "syntax.md", "Title One", include_subsections=False)
    assert (setext["line"], setext["source_end_line"], setext["content_start_line"]) == (4, 5, 6)


def test_section_refuses_binary_and_non_utf8_input(extended, hostile_tree):
    hostile_tree.at("latin1.md").write_bytes(b"# Caf\xe9\n")
    router = extended()
    for name in (BINARY_FILE, "latin1.md"):
        assert error(router, "md.section", path=name, heading="Caf")[1] == davellm_files.NOT_TEXT


def test_section_refuses_denylisted_paths(extended, hostile_tree):
    router = extended()
    for blocked in SECRET_FILES + SECRET_DIRS + ("docs/../.env", "./.ssh/config", SECRET_FILE_LINK):
        assert error(router, "md.section", path=blocked, heading="x") == ("error", PATH_NOT_ALLOWED), blocked


def test_section_refuses_paths_outside_the_root(extended, hostile_tree):
    router = extended()
    for escape in ("../outside/secret.txt", "docs/../../outside/notes.txt",
                   str(hostile_tree.outside / "notes.txt"), "/etc/hostname"):
        assert error(router, "md.section", path=escape, heading="x") == ("error", PATH_NOT_ALLOWED), escape


def test_section_refuses_symlink_escapes_and_loops(extended, hostile_tree):
    router = extended()
    for link in (OUTSIDE_LINK, f"{OUTSIDE_DIR_LINK}/notes.txt", CHAIN_LINK, LOOP_LINK, "links/loop_b"):
        assert error(router, "md.section", path=link, heading="x") == ("error", PATH_NOT_ALLOWED), link
    assert section(router, INTERNAL_LINK, "Setup")["path"] == "docs/guide.md"


def test_section_shows_only_relative_paths(extended, hostile_tree):
    router = extended()
    for requested in ("docs/guide.md", str(hostile_tree.at("docs/guide.md")), INTERNAL_LINK):
        for heading in ("Setup", "Missing"):
            execution = run(router, "md.section", path=requested, heading=heading)
            assert str(hostile_tree.base) not in f"{execution.result}{execution.error}"
        assert section(router, requested, "Setup")["path"] == "docs/guide.md"
    write(hostile_tree, "docs/dupes.md", DUPLICATES)
    execution = run(router, "md.section", path=str(hostile_tree.at("docs/dupes.md")), heading="Setup")
    assert json.loads(execution.result)["path"] == "docs/dupes.md"
    assert str(hostile_tree.base) not in execution.result


def test_section_never_returns_secret_sentinels(extended, hostile_tree):
    hostile_tree.at(".env").write_text(f"# Install\n{sentinel('.env')}\n")
    (hostile_tree.outside / "secret.txt").write_text(f"# Install\n{sentinel('outside/secret.txt')}\n")
    write(hostile_tree, "guide.md", GUIDE)
    router = extended()
    texts = []
    for path in (".env", SECRET_FILE_LINK, OUTSIDE_LINK, CHAIN_LINK, "guide.md", "docs/../.env"):
        for tool, arguments in (("md.section", {"heading": "Install"}), ("md.outline", {})):
            execution = run(router, tool, path=path, **arguments)
            texts += [execution.result, execution.error]
    assert not hostile_tree.leaked(*texts)


def test_section_has_no_filesystem_effects(router, hostile_tree):
    before = hostile_tree.snapshot()
    for path, heading in (("guide.md", "Install"), ("dupes.md", "Setup"), ("guide.md", "Missing"),
                          (".env", "x"), (LOOP_LINK, "x"), (BINARY_FILE, "x"), ("docs", "x")):
        for include in (True, False):
            run(router, "md.section", path=path, heading=heading, include_subsections=include)
    assert hostile_tree.snapshot() == before


# Security contract ----------------------------------------------------------------------------

def huge_markdown(tree):
    huge = tree.at("data/huge.md")
    if not huge.exists():
        huge.write_bytes(b"# Huge\n" + b"y" * davellm_files.READ_MAX_FILE_BYTES)
    return huge


def test_md_outline_passes_the_security_contract(extended, hostile_tree):
    router = extended()
    contract = PathToolContract(
        tool="md.outline", arguments=lambda path: {"path": path},
        oversized=lambda tree: {"path": str(huge_markdown(tree))},
        invalid=({}, {"path": ""}, {"path": 7}, {"path": "README.md", "max_headings": 0},
                 {"path": "README.md", "max_headings": 501}, {"path": "README.md", "extra": 1}),
    )
    assert_path_contract(contract, hostile_tree, run_path_contract(router.TOOL_REGISTRY, contract, hostile_tree))


def test_md_section_passes_the_security_contract(extended, hostile_tree):
    router = extended()
    contract = PathToolContract(
        tool="md.section", arguments=lambda path: {"path": path, "heading": "Setup"},
        allowed=("docs/guide.md", INTERNAL_LINK),
        oversized=lambda tree: {"path": str(huge_markdown(tree)), "heading": "Huge"},
        invalid=({}, {"path": "docs/guide.md"}, {"path": "docs/guide.md", "heading": ""},
                 {"path": "docs/guide.md", "heading": "x" * 501},
                 {"path": "docs/guide.md", "heading": "Setup", "include_subsections": 1},
                 {"path": "docs/guide.md", "heading": "Setup", "extra": 1}),
    )
    assert_path_contract(contract, hostile_tree, run_path_contract(router.TOOL_REGISTRY, contract, hostile_tree))


# Symlink swaps after admission (TOCTOU) --------------------------------------------------------

@pytest.fixture(params=[True, False], ids=["descriptor-walk", "open-and-verify"])
def open_strategy(request, monkeypatch):
    if request.param and not davellm_files.DESCRIPTOR_WALK:
        pytest.skip("descriptor walks are unavailable on this platform")
    monkeypatch.setattr(davellm_files, "DESCRIPTOR_WALK", request.param)
    return request.param


def swap_after_admission(router, monkeypatch, swap):
    admit = router.resolve_extended_tool_path

    def admit_then_swap(path):
        admitted = admit(path)
        swap()
        return admitted

    monkeypatch.setattr(router, "resolve_extended_tool_path", admit_then_swap)


TOOL_CALLS = [("md.outline", {}), ("md.section", {"heading": "Install"})]


@pytest.mark.parametrize("tool,arguments", TOOL_CALLS, ids=["md.outline", "md.section"])
def test_markdown_file_swapped_for_a_symlink_is_not_followed(
    extended, hostile_tree, monkeypatch, open_strategy, tool, arguments,
):
    router = extended()
    target = write(hostile_tree, "notes.md", "# Install\nordinary notes\n")
    (hostile_tree.outside / "secret.txt").write_text(f"# {sentinel('outside/secret.txt')}\n# Install\n")

    def swap():
        target.unlink()
        target.symlink_to(hostile_tree.outside / "secret.txt")

    swap_after_admission(router, monkeypatch, swap)
    before = hostile_tree.snapshot()
    execution = run(router, tool, path="notes.md", **arguments)
    assert (execution.status, execution.error) == ("error", PATH_NOT_ALLOWED)
    assert not hostile_tree.leaked(execution.result, execution.error)
    after = hostile_tree.snapshot()
    assert {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)} == {
        "root/notes.md"}  # the attacker's swap, and nothing else


@pytest.mark.parametrize("tool,arguments", TOOL_CALLS, ids=["md.outline", "md.section"])
def test_markdown_folder_swapped_for_a_symlink_is_not_followed(
    extended, hostile_tree, monkeypatch, open_strategy, tool, arguments,
):
    router = extended()
    (hostile_tree.outside / "guide.md").write_text(f"# Install\n{sentinel('outside/secret.txt')}\n")
    docs = hostile_tree.at("docs")

    def swap():
        docs.rename(hostile_tree.at("docs-moved"))
        docs.symlink_to(hostile_tree.outside, target_is_directory=True)

    swap_after_admission(router, monkeypatch, swap)
    execution = run(router, tool, path="docs/guide.md", **arguments)
    assert (execution.status, execution.error) == ("error", PATH_NOT_ALLOWED)
    assert not hostile_tree.leaked(execution.result, execution.error)


def test_markdown_file_swapped_for_a_fifo_is_refused_without_blocking(
    extended, hostile_tree, monkeypatch, open_strategy,
):
    router = extended()
    target = write(hostile_tree, "notes.md", "# Install\n")

    def swap():
        target.unlink()
        os.mkfifo(target)

    swap_after_admission(router, monkeypatch, swap)
    stop = threading.Event()

    def release_a_blocked_reader():
        # A reader stuck opening the FIFO would hang the suite; give it a writer so the test fails instead.
        while not stop.wait(2):
            try:
                os.close(os.open(target, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    watchdog = threading.Thread(target=release_a_blocked_reader, daemon=True)
    watchdog.start()
    started = time.monotonic()
    try:
        assert error(router, "md.section", path="notes.md", heading="Install")[1] == davellm_files.NOT_A_REGULAR_FILE
    finally:
        stop.set()
        watchdog.join()
    assert time.monotonic() - started < 2


# Shared parser --------------------------------------------------------------------------------

REPRESENTATIVE = {"guide.md": GUIDE, "dupes.md": DUPLICATES, "syntax.md": SYNTAX}


@pytest.mark.parametrize("name", sorted(REPRESENTATIVE))
def test_both_tools_see_exactly_the_same_headings(router, name):
    headings = outline(router, name)["headings"]
    assert headings, name
    by_text = {}
    for entry in headings:
        by_text.setdefault(" ".join(entry["text"].split()).casefold(), []).append(entry)
    for key, group in by_text.items():
        if not key:
            continue
        result = section(router, name, group[0]["text"], include_subsections=False)
        found = result["matches"] if result["ambiguous"] else [result]
        assert [(item["level"], item["line"]) for item in found] == [
            (item["level"], item["line"]) for item in group], (name, key)


@pytest.mark.parametrize("name", sorted(REPRESENTATIVE))
def test_section_boundaries_follow_the_outline(router, hostile_tree, name):
    headings = outline(router, name)["headings"]
    total = len(file_lines(hostile_tree, name))
    counts = {}
    for entry in headings:
        key = " ".join(entry["text"].split()).casefold()
        counts[key] = counts.get(key, 0) + 1
    for index, entry in enumerate(headings):
        key = " ".join(entry["text"].split()).casefold()
        if not key or counts[key] > 1:
            continue
        start = entry.get("source_end_line", entry["line"]) + 1
        later = headings[index + 1:]
        same_or_higher = next((item["line"] for item in later if item["level"] <= entry["level"]), total + 1)
        any_level = later[0]["line"] if later else total + 1
        for include, stop in ((True, same_or_higher), (False, any_level)):
            result = section(router, name, entry["text"], include_subsections=include)
            expected = (start, stop - 1) if stop - 1 >= start else (None, None)
            assert (result["content_start_line"], result["content_end_line"]) == expected, (name, entry, include)


def test_both_tools_call_the_one_shared_parser(router, monkeypatch):
    calls = []
    shared = davellm_markdown.parse_headings

    def without_install(lines, *args, **kwargs):
        calls.append(len(lines))
        return [heading for heading in shared(lines, *args, **kwargs) if heading.text != "Install"]

    monkeypatch.setattr(davellm_markdown, "parse_headings", without_install)
    assert "Install" not in [item["text"] for item in outline(router, "guide.md")["headings"]]
    assert error(router, "md.section", path="guide.md", heading="Install")[1] == HEADING_NOT_FOUND
    assert calls == [36, 36]


def test_parser_records_every_heading_field():
    headings = parse_headings(split_lines(SYNTAX))
    first, subtitle = headings[0], headings[2]
    assert (first.text, first.level, first.line, first.source_end_line, first.body_start_line,
            first.breadcrumb) == ("Title One", 1, 4, 5, 6, ("Title One",))
    assert (subtitle.line, subtitle.source_end_line, subtitle.body_start_line) == (12, 14, 15)
    assert headings[1].breadcrumb == ("Title One", "Three spaces")
    assert [heading.text for heading in headings] == [
        "Title One", "Three spaces", "Sub title spanning lines", "Closing #s", "Setup", "Setup"]


def test_parser_skips_only_a_leading_front_matter_block():
    assert [h.text for h in parse_headings(["---", "title: x", "---", "# Real"])] == ["Real"]
    assert [h.text for h in parse_headings(["---", "# Unclosed front matter"])] == ["Unclosed front matter"]
    assert [h.text for h in parse_headings(["", "---", "title: x", "---"])] == ["title: x"]


def test_parser_setext_rules():
    assert [(h.text, h.level) for h in parse_headings(["Foo", "    bar", "==="])] == [("Foo bar", 1)]
    assert parse_headings(["> quote", "---"]) == [] and parse_headings(["1. item", "==="]) == []
    assert parse_headings(["<div>", "---"]) == [] and parse_headings(["Foo", "= ="]) == []
    assert parse_headings(["Foo", "", "---"]) == [] and parse_headings(["Foo", "    ---"]) == []
    assert parse_headings(["Foo", "   ", "---"]) == [] and parse_headings(["Foo", "\t", "==="]) == []
    assert [(h.text, h.level) for h in parse_headings(["Foo", "***", "Bar", "---"])] == [("Bar", 2)]
    assert [h.text for h in parse_headings(["```", "Foo", "```", "---"])] == []


def test_parser_refuses_more_than_the_heading_ceiling():
    assert len(parse_headings(["#"] * 3, max_headings=3)) == 3
    with pytest.raises(davellm_files.FileToolError, match=TOO_MANY_HEADINGS):
        parse_headings(["#"] * 4, max_headings=3)
