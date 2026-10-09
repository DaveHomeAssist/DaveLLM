"""Offline source identity/answer-structure fixtures, not a live model evaluation."""

import asyncio
import json
import re

import pytest

import davellm_files
from daveharness import run_tool


@pytest.fixture
def source_tools(router_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", "true")
    router, _, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])

    def call(name, **arguments):
        execution = asyncio.run(run_tool(name, arguments, registry=router.TOOL_REGISTRY))
        assert execution.status == "success", execution.error
        assert len(execution.result.encode("utf-8")) <= davellm_files.OUTPUT_BUDGET_BYTES
        return json.loads(execution.result)

    return call


def assert_identity(result, source):
    rows = result["numbered_lines"]
    assert len(rows) == result["line_count"] == len(result["lines"])
    assert [row["text"] for row in rows] == result["lines"]
    start = result.get("start_line", result.get("content_start_line"))
    if rows:
        assert [row["line"] for row in rows] == list(range(start, start + len(rows)))
    for row in rows:
        assert row["text"] == source[row["line"] - 1][:davellm_files.READ_MAX_LINE_CHARS]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_file_absolute_offsets_preserve_blank_unicode_and_verbatim_text(source_tools, tmp_path, newline):
    source = ["preface"] * 70 + ["", "  λ café\t", "```", "same", "same", "after ten, not by ten"]
    (tmp_path / "source.txt").write_bytes(newline.join(source).encode("utf-8"))
    result = source_tools("file.read_lines", path="source.txt", start_line=71, max_lines=6)
    assert_identity(result, source)
    assert result["numbered_lines"][0] == {"line": 71, "text": ""}
    assert result["numbered_lines"][-1] == {"line": 76, "text": "after ten, not by ten"}
    assert result["next_start_line"] is None


def test_search_read_window_must_contain_chosen_match_fixture(source_tools, tmp_path):
    source = ["padding"] * 100 + ["context cap = 8192", "DAVE_CHAT_NUM_CTX = configured", "Unknown runtime"]
    (tmp_path / "source.txt").write_text("\n".join(source), encoding="utf-8")
    search = source_tools("file.search", path="source.txt", query="DAVE_CHAT_NUM_CTX", max_matches=5)
    match = search["matches"][0]["line"]
    good = source_tools("file.read_lines", path="source.txt", start_line=match - 1, max_lines=3)
    bad = source_tools("file.read_lines", path="source.txt", start_line=match - 40, max_lines=40)
    assert_identity(good, source)
    assert_identity(bad, source)
    assert match in {row["line"] for row in good["numbered_lines"]}
    assert match not in {row["line"] for row in bad["numbered_lines"]}
    assert bad["next_start_line"] == match  # No hidden extra read repairs the caller's range.


@pytest.mark.parametrize("heading", ["## Browser mode", "Browser mode\n------------"])
def test_markdown_numbering_uses_real_file_offsets_including_fences_and_blanks(source_tools, tmp_path, heading):
    text = "\n".join(["preface"] * 70 + ["", heading, "", "```sh", "start server", "```", "", "Open browser", "## End"])
    (tmp_path / "guide.md").write_text(text, encoding="utf-8")
    source = text.splitlines()
    result = source_tools("md.section", path="guide.md", heading="Browser mode", include_subsections=False)
    assert_identity(result, source)
    assert result["numbered_lines"][0]["line"] == source.index("```sh")
    assert result["numbered_lines"][0]["text"] == ""


def citation_structure_ok(answer, source, expected_lines):
    """Fixture-only count/range checks; deliberately not a semantic truth grader."""
    bullets = answer.splitlines()
    if len(bullets) != 3 or not all(line.startswith("- ") for line in bullets):
        return False
    citations = [re.search(r"\[line (\d+)\]$", line) for line in bullets]
    if not all(citations):
        return False
    numbers = [int(match.group(1)) for match in citations]
    return numbers == expected_lines and set(numbers) <= {row["line"] for row in source["numbered_lines"]}


def test_three_bullet_absolute_citation_fixture_rejects_saved_failure_patterns(source_tools, tmp_path):
    source = ["preface"] * 70 + ["", "## Browser mode", "", "Start the server", "", "Open the browser", "Authentication is required"]
    (tmp_path / "guide.md").write_text("\n".join(source), encoding="utf-8")
    result = source_tools("md.section", path="guide.md", heading="Browser mode")
    expected = [74, 76, 77]
    good = "- Start the server [line 74]\n- Open the browser [line 76]\n- Authentication is required [line 77]"
    assert citation_structure_ok(good, result, expected)
    assert not citation_structure_ok("\n".join(good.splitlines()[:2]), result, expected)
    assert not citation_structure_ok(good.replace("74", "2").replace("76", "4").replace("77", "5"), result, expected)
    assert not citation_structure_ok(good.replace("74", "73"), result, expected)
    # Format-valid misinformation still passes this structural check: no safety/quality claim.
    assert citation_structure_ok(good.replace("Authentication is required", "Authentication is optional"), result, expected)


@pytest.mark.parametrize("tool", ["file.read_lines", "md.section"])
def test_numbered_output_budget_and_continuation_preserve_exact_returned_prefixes(source_tools, tmp_path, tool):
    body = ["λ\\\"" * 2000] * 100
    source = (["# Wide"] if tool == "md.section" else []) + body
    (tmp_path / "wide.md").write_text("\n".join(source), encoding="utf-8")
    arguments = {"heading": "Wide"} if tool == "md.section" else {"max_lines": 100}
    result = source_tools(tool, path="wide.md", **arguments)
    assert_identity(result, source)
    assert result["truncated"] and 0 < result["line_count"] < 100
    assert result["cut_lines"] == [row["line"] for row in result["numbered_lines"]]
    next_line = result["next_start_line"]
    assert next_line == result["numbered_lines"][-1]["line"] + 1
    page = source_tools("file.read_lines", path="wide.md", start_line=next_line, max_lines=1)
    assert_identity(page, source)
    assert page["numbered_lines"][0]["line"] == next_line


def test_empty_and_ambiguous_excerpts_do_not_invent_numbered_source(source_tools, tmp_path):
    (tmp_path / "empty.txt").write_text("")
    assert source_tools("file.read_lines", path="empty.txt")["numbered_lines"] == []
    (tmp_path / "guide.md").write_text("# Empty\n# Repeat\na\n# Repeat\nb", encoding="utf-8")
    assert source_tools("md.section", path="guide.md", heading="Empty")["numbered_lines"] == []
    assert "numbered_lines" not in source_tools("md.section", path="guide.md", heading="Repeat")
