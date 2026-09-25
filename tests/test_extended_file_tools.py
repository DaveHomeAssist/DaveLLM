"""PR-02: file.list, file.search, and file.read_lines behind DAVE_ENABLE_EXTENDED_TOOLS.

Every test runs against disposable roots (usually the hostile tree from
tests/hostile_fs.py) and drives the tools through DaveHarness's validated
dispatcher, so schema validation is part of what is tested.
"""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import pytest

import davellm_files
from daveharness import run_tool
from davellm_files import (
    AMBIGUOUS_RELATIVE_PATH, OUTPUT_BUDGET_BYTES, PATH_NOT_ALLOWED, cursor_scope, encode_cursor,
)
from hostile_fs import (
    BINARY_FILE, CHAIN_LINK, IGNORED_FILES, INTERNAL_LINK, LOOP_LINK, OUTSIDE_DIR_LINK,
    OUTSIDE_LINK, OVERSIZED_FILE, SECRET_DIR_LINK, SECRET_DIRS, SECRET_FILE_LINK, SECRET_FILES,
    sentinel,
)
from tool_contract import PathToolContract, assert_path_contract, run_path_contract


EXTENDED_TOOLS = {"file.list", "file.search", "file.read_lines"}
DEPTH_ONE = ["README.md", "certs", "config", "data", "docs", "keys", "links", "src"]
DEPTH_TWO = DEPTH_ONE + [
    "data/blob.bin", "data/large.log", "docs/guide.md", "docs/nested", "keys/README.txt",
    INTERNAL_LINK, "src/app.py",
]


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


def paths(result, key="entries"):
    return [item["path"] for item in result[key]]


def write_lines(path, count, *, text="line {n}", final_newline=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(text.format(n=number) for number in range(1, count + 1))
    path.write_text(body + ("\n" if final_newline and count else ""), encoding="utf-8")
    return path


# Registration ---------------------------------------------------------------------------------

def test_tools_register_only_with_both_flags(extended):
    assert not EXTENDED_TOOLS & set(extended(extended="false").TOOL_REGISTRY.public_catalog())
    assert not EXTENDED_TOOLS & set(extended(extended="true", tools=False).TOOL_REGISTRY.public_catalog())
    router = extended()
    for registry in (router.TOOL_REGISTRY, router.HARNESS_REGISTRY):
        assert EXTENDED_TOOLS <= set(registry.public_catalog())
    assert {definition.name for definition in router.extended_tool_definitions()} == EXTENDED_TOOLS


def test_extended_definitions_are_read_only_bounded_and_strict(extended):
    router = extended()
    for name in EXTENDED_TOOLS:
        definition = router.TOOL_REGISTRY.get(name)
        assert (definition.permission, definition.approval_required) == ("read_files", False)
        assert (definition.cancellation, definition.async_handler, definition.context_handler) == (
            "bounded", False, False)
        schema = definition.parameters
        assert schema["additionalProperties"] is False
        for field in schema["properties"].values():
            types = field["type"] if isinstance(field["type"], list) else [field["type"]]
            assert set(types) <= {"integer", "string", "boolean", "null"}
            if "integer" in types:
                assert {"minimum", "maximum"} <= set(field)
            if "string" in types:
                assert {"minLength", "maxLength"} <= set(field)
            if "null" in types:
                assert field_name_is_optional(schema, field)
        assert error(router, name, path="README.md", query="x", unexpected=True)[0] == "validation_error"


def field_name_is_optional(schema, field):
    name = next(key for key, value in schema["properties"].items() if value is field)
    return name not in schema.get("required", [])


def test_optional_fields_accept_null_as_their_default(extended, hostile_tree):
    router = extended()
    assert ok(router, "file.list", path=None, depth=None, max_entries=None, cursor=None) == ok(router, "file.list")
    assert ok(router, "file.search", query="Guide", path=None, case_sensitive=None, max_matches=None) == ok(
        router, "file.search", query="Guide")
    assert ok(router, "file.read_lines", path="docs/guide.md", start_line=None, max_lines=None) == ok(
        router, "file.read_lines", path="docs/guide.md")
    assert error(router, "file.read_lines", path=None)[0] == "validation_error"
    assert error(router, "file.search", query=None)[0] == "validation_error"


# Security contract ----------------------------------------------------------------------------

def test_file_list_passes_the_security_contract(extended, hostile_tree):
    router = extended()
    contract = PathToolContract(
        tool="file.list", arguments=lambda path: {"path": path, "depth": 3},
        invalid=({"depth": 4}, {"depth": -1}, {"depth": 1.5}, {"depth": "1"}, {"max_entries": 0},
                 {"max_entries": 501}, {"path": ""}, {"path": 1}, {"cursor": ""}, {"extra": True}),
    )
    assert_path_contract(contract, hostile_tree, run_path_contract(router.TOOL_REGISTRY, contract, hostile_tree))


def test_file_search_passes_the_security_contract(extended, hostile_tree):
    router = extended()
    contract = PathToolContract(
        tool="file.search", arguments=lambda path: {"path": path, "query": "e"},
        invalid=({}, {"query": ""}, {"query": "x" * 201}, {"query": 5}, {"query": "x", "mode": "regex"},
                 {"query": "x", "max_matches": 0}, {"query": "x", "max_matches": 201},
                 {"query": "x", "case_sensitive": "yes"}, {"query": "x", "path": ""}),
    )
    assert_path_contract(contract, hostile_tree, run_path_contract(router.TOOL_REGISTRY, contract, hostile_tree))


def test_file_read_lines_passes_the_security_contract(extended, hostile_tree):
    router = extended()
    huge = hostile_tree.at("data/huge.log")
    huge.write_bytes(b"y" * (davellm_files.READ_MAX_FILE_BYTES + 1))
    contract = PathToolContract(
        tool="file.read_lines", arguments=lambda path: {"path": path},
        oversized=lambda _tree: {"path": str(huge)},
        invalid=({}, {"path": ""}, {"path": 7}, {"path": "README.md", "start_line": 0},
                 {"path": "README.md", "max_lines": 0}, {"path": "README.md", "max_lines": 401},
                 {"path": "README.md", "start_line": davellm_files.READ_MAX_START_LINE + 1},
                 {"path": "README.md", "extra": 1}),
    )
    assert_path_contract(contract, hostile_tree, run_path_contract(router.TOOL_REGISTRY, contract, hostile_tree))


def test_relative_paths_anchor_at_the_single_root_and_are_refused_with_several(extended, hostile_tree):
    router = extended()
    assert ok(router, "file.read_lines", path="docs/guide.md")["path"] == "docs/guide.md"
    assert ok(router, "file.read_lines", path=str(hostile_tree.at("docs/guide.md")))["path"] == "docs/guide.md"
    for escape in ("../outside/secret.txt", "docs/../../outside/notes.txt", "./.env"):
        assert error(router, "file.read_lines", path=escape)[1] == PATH_NOT_ALLOWED
    several = extended(hostile_tree.root, hostile_tree.at("docs"))
    assert error(several, "file.list", path="docs")[1] == AMBIGUOUS_RELATIVE_PATH
    assert error(several, "file.list")[1] == AMBIGUOUS_RELATIVE_PATH
    assert ok(several, "file.list", path=str(hostile_tree.at("docs")))["path"] == "."


# file.list ------------------------------------------------------------------------------------

def test_list_shows_an_ordinary_directory_with_entry_details(extended, hostile_tree):
    result = ok(extended(), "file.list")
    assert result["path"] == "." and paths(result) == DEPTH_ONE
    readme = result["entries"][0]
    assert readme == {"path": "README.md", "name": "README.md", "type": "file",
                      "size": hostile_tree.at("README.md").stat().st_size, "modified": readme["modified"]}
    assert readme["modified"].endswith("+00:00")
    assert {entry["type"] for entry in result["entries"][1:]} == {"directory"}
    assert "size" not in result["entries"][1]
    assert (result["truncated"], result["next_cursor"], result["depth_limited"]) == (False, None, True)


def test_list_order_is_deterministic_and_breadth_first(extended):
    router = extended()
    first, second = ok(router, "file.list", depth=2), ok(router, "file.list", depth=2)
    assert first == second
    assert paths(first) == DEPTH_TWO


@pytest.mark.parametrize("depth,expected", [
    (0, ["."]),
    (1, DEPTH_ONE),
    (2, DEPTH_TWO),
    (3, DEPTH_TWO + ["docs/nested/deep"]),
])
def test_list_depth_limits(extended, depth, expected):
    result = ok(extended(), "file.list", depth=depth)
    assert paths(result) == expected
    assert result["depth_limited"] is (depth > 0)


def test_list_depth_zero_on_a_file_and_depth_above_three_rejected(extended):
    router = extended()
    single = ok(router, "file.list", path="docs/guide.md", depth=0)
    assert [(entry["path"], entry["type"]) for entry in single["entries"]] == [("docs/guide.md", "file")]
    assert error(router, "file.list", depth=4)[0] == "validation_error"


def test_list_entry_bound_and_deterministic_truncation(extended):
    router = extended()
    assert error(router, "file.list", max_entries=501)[0] == "validation_error"
    assert ok(router, "file.list", depth=3, max_entries=500)["truncated"] is False
    first = ok(router, "file.list", depth=3, max_entries=3)
    assert paths(first) == DEPTH_ONE[:3] and first["truncated"] is True
    assert first == ok(router, "file.list", depth=3, max_entries=3)


def test_list_cursor_pages_deterministically_through_the_whole_listing(extended):
    router = extended()
    complete = paths(ok(router, "file.list", depth=3, max_entries=500))
    pages, cursor = [], None
    while True:
        arguments = {"depth": 3, "max_entries": 4} | ({"cursor": cursor} if cursor else {})
        page = ok(router, "file.list", **arguments)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert [entry for page in pages for entry in paths(page)] == complete
    assert all(page["truncated"] for page in pages[:-1]) and not pages[-1]["truncated"]
    assert pages[1] == ok(router, "file.list", depth=3, max_entries=4, cursor=pages[0]["next_cursor"])


def test_list_cursor_is_bound_to_the_original_request(extended, hostile_tree):
    router = extended()
    cursor = ok(router, "file.list", depth=3, max_entries=4)["next_cursor"]
    for changed in ({"depth": 2, "max_entries": 4}, {"depth": 3, "max_entries": 5},
                    {"depth": 3, "max_entries": 4, "path": "docs"}):
        assert error(router, "file.list", cursor=cursor, **changed)[1] == "Cursor does not belong to this request"
    assert ok(router, "file.list", path=str(hostile_tree.root), depth=3, max_entries=4, cursor=cursor)


def test_list_rejects_malformed_and_out_of_range_cursors(extended, hostile_tree):
    router = extended()
    for cursor in ("garbage", "c1.4", "c2.4.0123456789abcdef", "c1.-4.0123456789abcdef", "c1.4.x"):
        message = error(router, "file.list", depth=3, max_entries=4, cursor=cursor)[1]
        assert message in {"Invalid cursor", "Cursor does not belong to this request"}
    scope = cursor_scope("file.list", {"path": str(hostile_tree.root), "depth": 3, "max_entries": 4})
    past = encode_cursor(9_999, scope)
    assert error(router, "file.list", depth=3, max_entries=4, cursor=past)[1] == "Cursor is past the end of the results"
    assert error(router, "file.list", cursor="x" * 65)[0] == "validation_error"


def test_list_omits_ignored_folders_secrets_and_unsafe_symlinks(extended, hostile_tree):
    router = extended()
    listed = paths(ok(router, "file.list", depth=3, max_entries=500))
    for ignored in IGNORED_FILES:
        top = ignored.split("/")[0]
        assert not [item for item in listed if item == top or item.startswith(top + "/")]
    for secret in (*SECRET_FILES, *SECRET_DIRS):
        assert secret not in listed and not [item for item in listed if item.startswith(secret + "/")]
    links = ok(router, "file.list", path="links")
    assert [(entry["path"], entry["type"]) for entry in links["entries"]] == [(INTERNAL_LINK, "symlink")]
    assert "size" not in links["entries"][0]


def test_list_symlink_loops_return_promptly(extended, hostile_tree):
    router = extended()
    started = time.monotonic()
    assert paths(ok(router, "file.list", path="links")) == [INTERNAL_LINK]
    assert error(router, "file.list", path=LOOP_LINK)[1] == PATH_NOT_ALLOWED
    assert time.monotonic() - started < 5


def test_list_nested_roots_display_relative_paths_deterministically(extended, hostile_tree):
    root, docs = hostile_tree.root, hostile_tree.at("docs")
    results = []
    for roots in ((root, docs), (docs, root)):
        router = extended(*roots)
        results.append((ok(router, "file.list", path=str(docs), depth=2),
                        ok(router, "file.list", path=str(root), depth=2)))
    assert results[0] == results[1]
    docs_listing, root_listing = results[0]
    assert (docs_listing["path"], paths(docs_listing)) == (".", ["guide.md", "nested", "nested/deep"])
    assert paths(root_listing) == DEPTH_TWO


def test_list_output_never_contains_absolute_paths_or_sentinels(extended, hostile_tree):
    router = extended()
    for arguments in ({}, {"depth": 3, "max_entries": 500}, {"path": str(hostile_tree.root), "depth": 3},
                      {"path": str(hostile_tree.at("docs/guide.md")), "depth": 0}):
        encoded = run(router, "file.list", **arguments).result
        assert str(hostile_tree.base) not in encoded and not hostile_tree.leaked(encoded)


def test_list_output_stays_within_the_result_budget(extended, tmp_path):
    root = (tmp_path / "wide").resolve()
    root.mkdir()
    for index in range(500):
        (root / (f"{index:03d}-" + "n" * 240)).write_text("x")
    router = extended(root)
    first = run(router, "file.list", max_entries=500)
    assert len(first.result.encode("utf-8")) <= OUTPUT_BUDGET_BYTES
    page = json.loads(first.result)
    assert page["truncated"] and 0 < len(page["entries"]) < 500
    seen = paths(page)
    while page["next_cursor"]:
        page = ok(router, "file.list", max_entries=500, cursor=page["next_cursor"])
        seen += paths(page)
    assert len(seen) == len(set(seen)) == 500


# file.search ----------------------------------------------------------------------------------

@pytest.fixture
def corpus(hostile_tree):
    notes = hostile_tree.at("notes")
    notes.mkdir()
    (notes / "literal.txt").write_text("a.b\naxb\n[x] item\n(a+)+$\n", encoding="utf-8")
    write_lines(notes / "numbered.txt", 9, text="row {n}")
    (notes / "numbered.txt").write_text(
        "alpha\nbeta\nneedle one\ngamma\ndelta\nepsilon\nNEEDLE two\r\nzeta\n", encoding="utf-8")
    return notes


def test_search_is_literal_by_default_and_offers_no_regex_mode(extended, corpus):
    router = extended()
    result = ok(router, "file.search", query="a.b", path="notes")
    assert [(match["path"], match["line"], match["text"]) for match in result["matches"]] == [
        ("notes/literal.txt", 1, "a.b")]
    assert [match["line"] for match in ok(router, "file.search", query="[x]", path="notes")["matches"]] == [3]
    assert [match["line"] for match in ok(router, "file.search", query="(a+)+$", path="notes")["matches"]] == [4]
    assert error(router, "file.search", query="a.b", mode="regex")[0] == "validation_error"
    assert error(router, "file.search", query="a.b", regex=True)[0] == "validation_error"


def test_search_regex_syntax_is_inert_even_for_a_pathological_pattern(extended, tmp_path):
    root = (tmp_path / "evil").resolve()
    root.mkdir()
    (root / "long.txt").write_text("a" * 1_000_000 + "!\n", encoding="utf-8")
    router = extended(root)
    started = time.monotonic()
    result = ok(router, "file.search", query="(a+)+$", path="long.txt")
    assert result["matches"] == [] and result["files_scanned"] == 1
    assert time.monotonic() - started < 5


def test_search_case_modes(extended, corpus):
    router = extended()
    assert [match["line"] for match in ok(router, "file.search", query="needle", path="notes")["matches"]] == [3, 7]
    sensitive = ok(router, "file.search", query="needle", path="notes", case_sensitive=True)
    assert [match["line"] for match in sensitive["matches"]] == [3]
    assert ok(router, "file.search", query="GUIDE", case_sensitive=True)["matches"] == []
    assert paths(ok(router, "file.search", query="GUIDE"), "matches") == ["docs/guide.md"]


def test_search_reports_correct_line_numbers_and_strips_carriage_returns(extended, corpus):
    matches = ok(extended(), "file.search", query="needle", path="notes/numbered.txt")["matches"]
    assert [(match["line"], match["text"]) for match in matches] == [(3, "needle one"), (7, "NEEDLE two")]


def test_search_query_length_is_bounded_and_single_line(extended):
    router = extended()
    assert ok(router, "file.search", query="q" * 200)["matches"] == []
    assert error(router, "file.search", query="q" * 201)[0] == "validation_error"
    assert error(router, "file.search", query="two\nlines")[1] == davellm_files.MULTILINE_QUERY


def test_search_returns_at_most_the_requested_matches(extended, hostile_tree):
    write_lines(hostile_tree.at("many.txt"), 300, text="hit {n}")
    router = extended()
    capped = ok(router, "file.search", query="hit", path="many.txt", max_matches=200)
    assert capped["match_count"] == len(capped["matches"]) == 200 and capped["truncated"]
    assert [match["line"] for match in capped["matches"]] == list(range(1, 201))
    exact = ok(router, "file.search", query="hit 3", path="many.txt", max_matches=200)
    assert not exact["truncated"]
    assert error(router, "file.search", query="hit", max_matches=201)[0] == "validation_error"


def test_search_scans_at_most_two_thousand_files(extended, tmp_path):
    root = (tmp_path / "wide").resolve()
    root.mkdir()
    for index in range(davellm_files.SEARCH_MAX_FILES + 5):
        (root / f"f{index:05d}.txt").write_text("unique\n" if index >= davellm_files.SEARCH_MAX_FILES else "common\n")
    result = ok(extended(root), "file.search", query="unique")
    assert result["files_scanned"] == davellm_files.SEARCH_MAX_FILES
    assert result["matches"] == [] and result["truncated"]


def test_search_skips_files_over_one_mib_and_binary_files(extended, hostile_tree):
    edge = hostile_tree.at("data/edge.log")
    edge.write_text("y" * (davellm_files.SEARCH_MAX_FILE_BYTES - 7) + "needle\n")
    hostile_tree.at("data/latin1.txt").write_bytes("caf\xe9 needle\n".encode("latin-1"))
    router = extended()
    result = ok(router, "file.search", query="needle", path="data")
    assert paths(result, "matches") == ["data/edge.log"]
    assert (result["skipped_large"], result["skipped_binary"]) == (1, 2)
    assert ok(router, "file.search", query="x", path=OVERSIZED_FILE)["skipped_large"] == 1
    assert ok(router, "file.search", query="IHDR", path=BINARY_FILE)["matches"] == []


def test_search_caps_line_text_at_three_hundred_characters_around_the_match(extended, hostile_tree):
    hostile_tree.at("wide.txt").write_text("l" * 700 + "needle" + "r" * 700 + "\n")
    match = ok(extended(), "file.search", query="needle", path="wide.txt")["matches"][0]
    assert len(match["text"]) == 300 and "needle" in match["text"] and match["text_truncated"]


def test_search_skips_ignored_folders_secrets_and_symlinks(extended, hostile_tree):
    router = extended()
    assert ok(router, "file.search", query="ignored:")["matches"] == []
    assert ok(router, "file.search", query="hostile-sentinel")["matches"] == []
    linked = ok(router, "file.search", query="Guide", path="links")
    assert (linked["matches"], linked["files_scanned"]) == ([], 0)
    for blocked in (".ssh", ".aws", "docs/nested/.ssh", SECRET_DIR_LINK, OUTSIDE_DIR_LINK, CHAIN_LINK):
        assert error(router, "file.search", query="x", path=blocked)[1] == PATH_NOT_ALLOWED


def test_search_order_is_deterministic_and_breadth_first(extended, hostile_tree):
    hostile_tree.at("zeta.txt").write_text("shared\n")
    hostile_tree.at("docs/alpha.txt").write_text("shared\n")
    router = extended()
    first = ok(router, "file.search", query="shared")
    assert first == ok(router, "file.search", query="shared")
    assert paths(first, "matches") == ["zeta.txt", "docs/alpha.txt"]


def test_search_output_never_contains_absolute_paths_or_sentinels(extended, hostile_tree):
    router = extended()
    for query in ("e", "sentinel", "outside", ".env", "BEGIN"):
        encoded = run(router, "file.search", query=query, path=str(hostile_tree.root), max_matches=200).result
        assert str(hostile_tree.base) not in encoded and not hostile_tree.leaked(encoded)


def remove_secrets(tree):
    for secret in SECRET_FILES:
        tree.at(secret).unlink()
    for directory in sorted(SECRET_DIRS, key=len, reverse=True):
        shutil.rmtree(tree.at(directory))
    for link in (SECRET_FILE_LINK, SECRET_DIR_LINK):
        tree.at(link).unlink()


def test_secret_existence_cannot_be_inferred_from_results(extended, hostile_tree):
    router = extended()
    probes = [("file.search", {"query": query}) for query in ("e", "sentinel", "x", "ssh")]
    probes += [("file.list", {"depth": depth, "max_entries": size}) for depth in (1, 2, 3) for size in (3, 500)]
    probes += [("file.list", {"path": directory, "depth": 1}) for directory in ("certs", "config", "keys", "links")]
    with_secrets = [ok(router, name, **arguments) for name, arguments in probes]
    remove_secrets(hostile_tree)
    without = [ok(router, name, **arguments) for name, arguments in probes]
    for before, after in zip(with_secrets, without):
        for entries in (before.get("entries", []), after.get("entries", [])):
            for entry in entries:
                entry.pop("modified", None)  # removing a child changes its folder's mtime
        assert before == after


def test_search_output_stays_within_the_result_budget(extended, hostile_tree):
    hostile_tree.at("accents.txt").write_text(("é" * 290 + " hit\n") * 200, encoding="utf-8")
    execution = run(extended(), "file.search", query="hit", path="accents.txt", max_matches=200)
    assert len(execution.result.encode("utf-8")) <= OUTPUT_BUDGET_BYTES
    result = json.loads(execution.result)
    assert result["truncated"] and 0 < result["match_count"] < 200


# file.read_lines ------------------------------------------------------------------------------

@pytest.fixture
def long_file(hostile_tree):
    return write_lines(hostile_tree.at("docs/long.md"), 25)


def page(router, path, start, count):
    return ok(router, "file.read_lines", path=path, start_line=start, max_lines=count)


def test_read_lines_first_middle_and_final_pages(extended, long_file):
    router = extended()
    first, middle, final = (page(router, "docs/long.md", start, 10) for start in (1, 11, 21))
    assert first["lines"] == [f"line {n}" for n in range(1, 11)]
    assert (first["start_line"], first["line_count"], first["total_lines"], first["next_start_line"]) == (1, 10, 25, 11)
    assert middle["lines"][0] == "line 11" and middle["next_start_line"] == 21
    assert final["lines"] == [f"line {n}" for n in range(21, 26)] and final["next_start_line"] is None
    assert not any(result["truncated"] for result in (first, middle, final))


def test_read_lines_small_and_edge_files(extended, hostile_tree):
    router = extended()
    hostile_tree.at("one.txt").write_text("only\n")
    hostile_tree.at("empty.txt").write_text("")
    hostile_tree.at("open.txt").write_text("a\nb")
    hostile_tree.at("crlf.txt").write_bytes(b"a\r\nb\r\n")
    hostile_tree.at("blank.txt").write_text("\n")
    expected = {"one.txt": ["only"], "empty.txt": [], "open.txt": ["a", "b"], "crlf.txt": ["a", "b"],
                "blank.txt": [""]}
    for name, lines in expected.items():
        result = ok(router, "file.read_lines", path=name)
        assert (result["lines"], result["total_lines"], result["next_start_line"]) == (lines, len(lines), None), name


def test_read_lines_exactly_four_hundred_lines_and_larger_pages_rejected(extended, hostile_tree):
    write_lines(hostile_tree.at("400.txt"), 400)
    write_lines(hostile_tree.at("450.txt"), 450)
    router = extended()
    whole = ok(router, "file.read_lines", path="400.txt", max_lines=400)
    assert whole["line_count"] == 400 and whole["next_start_line"] is None and not whole["truncated"]
    capped = ok(router, "file.read_lines", path="450.txt", max_lines=400)
    assert (capped["line_count"], capped["total_lines"], capped["next_start_line"]) == (400, 450, 401)
    assert error(router, "file.read_lines", path="450.txt", max_lines=401)[0] == "validation_error"


def test_read_lines_next_start_line_is_deterministic_and_reassembles_the_file(extended, hostile_tree):
    source = write_lines(hostile_tree.at("docs/book.md"), 1_234, text="sentence {n} of the book")
    router = extended()
    assert page(router, "docs/book.md", 1, 400) == page(router, "docs/book.md", 1, 400)
    lines, start = [], 1
    while start is not None:
        result = page(router, "docs/book.md", start, 400)
        lines += result["lines"]
        start = result["next_start_line"]
    assert "\n".join(lines) + "\n" == source.read_text()


def test_read_lines_start_beyond_end_of_file(extended, long_file):
    result = page(extended(), "docs/long.md", 100, 10)
    assert (result["lines"], result["total_lines"], result["next_start_line"], result["truncated"]) == (
        [], 25, None, False)


def test_read_lines_refuses_binary_and_non_utf8_files(extended, hostile_tree):
    router = extended()
    hostile_tree.at("latin1.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    hostile_tree.at("nul.txt").write_bytes(b"text\x00more\n")
    for name in (BINARY_FILE, "latin1.txt", "nul.txt"):
        assert error(router, "file.read_lines", path=name)[1] == davellm_files.NOT_TEXT


def test_read_lines_refuses_secrets_escapes_and_loops(extended, hostile_tree):
    router = extended()
    for blocked in (".env", ".ssh/config", "certs/server.key", SECRET_FILE_LINK, str(hostile_tree.outside / "secret.txt"),
                    OUTSIDE_LINK, CHAIN_LINK, LOOP_LINK, f"{OUTSIDE_DIR_LINK}/secret.txt", "docs/../.env"):
        status, message = error(router, "file.read_lines", path=blocked)
        assert (status, message) == ("error", PATH_NOT_ALLOWED), blocked
    assert error(router, "file.read_lines", path="docs")[1] == davellm_files.NOT_A_REGULAR_FILE
    assert error(router, "file.read_lines", path="docs/missing.md")[1] == davellm_files.FILE_NOT_FOUND


def test_read_lines_shows_only_relative_paths(extended, hostile_tree, long_file):
    router = extended()
    for requested in ("docs/long.md", str(long_file), f"{hostile_tree.root}/docs/../docs/long.md"):
        execution = run(router, "file.read_lines", path=requested)
        assert json.loads(execution.result)["path"] == "docs/long.md"
        assert str(hostile_tree.base) not in execution.result


def test_read_lines_cuts_long_lines_and_stays_within_the_result_budget(extended, hostile_tree):
    hostile_tree.at("wide.txt").write_text(("w" * 5_000 + "\n") * 400)
    execution = run(extended(), "file.read_lines", path="wide.txt", max_lines=400)
    assert len(execution.result.encode("utf-8")) <= OUTPUT_BUDGET_BYTES
    result = json.loads(execution.result)
    assert all(len(line) == davellm_files.READ_MAX_LINE_CHARS for line in result["lines"])
    assert result["truncated"] and result["cut_lines"] == list(range(1, result["line_count"] + 1))
    assert result["next_start_line"] == result["line_count"] + 1


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


def test_final_component_swapped_for_a_symlink_is_not_followed(extended, hostile_tree, monkeypatch, open_strategy):
    router = extended()
    target = hostile_tree.at("notes.txt")
    target.write_text("ordinary notes\n")

    def swap():
        target.unlink()
        target.symlink_to(hostile_tree.outside / "secret.txt")

    swap_after_admission(router, monkeypatch, swap)
    execution = run(router, "file.read_lines", path="notes.txt")
    assert (execution.status, execution.error) == ("error", PATH_NOT_ALLOWED)
    assert not hostile_tree.leaked(execution.result, execution.error)


def test_directory_swapped_for_a_symlink_is_not_followed(extended, hostile_tree, monkeypatch, open_strategy):
    router = extended()
    (hostile_tree.outside / "guide.md").write_text(sentinel("outside/secret.txt") + "\n")
    docs = hostile_tree.at("docs")

    def swap():
        docs.rename(hostile_tree.at("docs-moved"))
        docs.symlink_to(hostile_tree.outside, target_is_directory=True)

    swap_after_admission(router, monkeypatch, swap)
    execution = run(router, "file.read_lines", path="docs/guide.md")
    assert (execution.status, execution.error) == ("error", PATH_NOT_ALLOWED)
    assert not hostile_tree.leaked(execution.result, execution.error)


def test_file_swapped_for_a_fifo_is_refused_without_blocking(extended, hostile_tree, monkeypatch, open_strategy):
    router = extended()
    target = hostile_tree.at("notes.txt")
    target.write_text("ordinary notes\n")

    def swap():
        target.unlink()
        os.mkfifo(target)

    swap_after_admission(router, monkeypatch, swap)
    started = time.monotonic()
    assert error(router, "file.read_lines", path="notes.txt")[1] == davellm_files.NOT_A_REGULAR_FILE
    assert time.monotonic() - started < 5


def test_search_does_not_follow_a_file_swapped_after_the_walk(extended, hostile_tree, monkeypatch, open_strategy):
    router = extended()
    walk = davellm_files.walk_tree

    def walk_then_swap(*args, **kwargs):
        result = walk(*args, **kwargs)
        readme = hostile_tree.at("README.md")
        readme.unlink()
        readme.symlink_to(hostile_tree.outside / "secret.txt")
        return result

    monkeypatch.setattr(davellm_files, "walk_tree", walk_then_swap)
    result = ok(router, "file.search", query="sentinel")
    assert result["matches"] == [] and result["skipped_unreadable"] == 1
    assert not hostile_tree.leaked(json.dumps(result))
