"""Shared extended-tool helpers, exercised only against disposable pytest roots."""

import os

import pytest

from davellm_files import (
    DEFAULT_IGNORED_DIRS, bound_text, cursor_scope, decode_cursor, display_path,
    encode_cursor, is_text_file, looks_binary, paginate, relative_display, walk_tree,
)


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_paths_display_relative_to_the_deepest_root_and_never_absolute(tmp_path):
    root = (tmp_path / "root").resolve()
    nested_root = root / "projects" / "app"
    note = write(root / "docs" / "notes.md")
    source = write(nested_root / "src" / "main.py")
    outside = write(tmp_path / "outside.txt")

    assert display_path(note, [root]) == "docs/notes.md"
    assert display_path(root, [root]) == "."
    assert display_path(root / "docs" / ".." / "docs" / "notes.md", [root]) == "docs/notes.md"
    assert display_path(source, [root, nested_root]) == "src/main.py"
    assert display_path(source, [nested_root, root]) == "src/main.py"
    with pytest.raises(ValueError, match="outside"):
        display_path(outside, [root])
    (root / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        display_path(root / "escape", [root])
    assert relative_display(root / "docs" / "notes.md", root) == "docs/notes.md"
    with pytest.raises(ValueError):
        relative_display(root / ".." / "outside.txt", root)


def test_output_truncation_is_deterministic_and_reports_what_was_cut():
    text = "line one\nline two ✓\n" * 50
    first, second = bound_text(text, 100), bound_text(text, 100)
    assert first == second
    assert (first.text, first.truncated, first.omitted_chars) == (text[:100], True, len(text) - 100)
    whole = bound_text("short ✓", 7)
    assert (whole.text, whole.truncated, whole.omitted_chars) == ("short ✓", False, 0)
    for invalid in (0, -1, True, 2.5):
        with pytest.raises(ValueError):
            bound_text(text, invalid)


def test_cursors_page_deterministically_and_stay_bound_to_their_request():
    items = [f"item-{index:02d}" for index in range(25)]
    scope = cursor_scope("file.list", {"path": "docs", "depth": 2})
    assert scope == cursor_scope("file.list", {"depth": 2, "path": "docs", "cursor": "c1.10.x"})

    pages, cursor = [], None
    while True:
        page = paginate(items, page_size=10, scope=scope, cursor=cursor)
        pages.append(page)
        if not page.truncated:
            break
        cursor = page.next_cursor
    assert [len(page.items) for page in pages] == [10, 10, 5]
    assert [page.offset for page in pages] == [0, 10, 20]
    assert [item for page in pages for item in page.items] == items
    assert pages[-1].next_cursor is None and pages[0].total == 25
    assert pages[0].next_cursor == encode_cursor(10, scope)
    assert paginate(items, page_size=10, scope=scope) == pages[0]
    assert decode_cursor(pages[1].next_cursor, scope) == 20

    other_scope = cursor_scope("file.list", {"path": "src", "depth": 2})
    with pytest.raises(ValueError, match="does not belong"):
        paginate(items, page_size=10, scope=other_scope, cursor=pages[0].next_cursor)
    tampered = pages[0].next_cursor.replace(".10.", ".15.")
    with pytest.raises(ValueError, match="does not belong"):
        decode_cursor(tampered, scope)
    for malformed in ("", "c1.10", "c2.10.abc", "c1.-1.abc", "c1.١٠.abc", "c1." + "9" * 13 + ".abc"):
        with pytest.raises(ValueError, match="Invalid cursor"):
            decode_cursor(malformed, scope)
    with pytest.raises(ValueError, match="past the end"):
        paginate(items[:5], page_size=10, scope=scope, cursor=encode_cursor(10, scope))
    empty = paginate([], page_size=10, scope=scope)
    assert (empty.items, empty.truncated, empty.next_cursor) == ((), False, None)


def test_binary_detection_uses_nul_bytes_and_utf8_validity(tmp_path):
    assert looks_binary(b"text\x00more")
    assert looks_binary(b"\xff\xfe\xfd not utf-8")
    assert not looks_binary("plain text with ✓ and é\n".encode("utf-8"))
    assert not looks_binary("ab✓".encode("utf-8")[:-1])  # multibyte character cut by the sample
    assert not looks_binary(b"")

    text = tmp_path / "notes.md"
    text.write_text("# Title\n" + "✓" * 5000, encoding="utf-8")
    image = tmp_path / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert is_text_file(text, sample_bytes=1001)
    assert not is_text_file(image)
    assert is_text_file(empty)


def test_walk_stops_at_the_depth_limit_and_reports_it(tmp_path):
    root = tmp_path.resolve()
    write(root / "a" / "b" / "c" / "deep.txt")
    write(root / "top.txt")

    shallow = walk_tree(root, root, max_depth=2, max_entries=100)
    assert [(entry.display, entry.depth) for entry in shallow.entries] == [
        ("a", 1), ("top.txt", 1), ("a/b", 2),
    ]
    assert shallow.depth_limited and not shallow.truncated
    assert max(entry.depth for entry in shallow.entries) == 2

    full = walk_tree(root, root, max_depth=10, max_entries=100)
    assert [entry.display for entry in full.entries] == [
        "a", "top.txt", "a/b", "a/b/c", "a/b/c/deep.txt",
    ]
    assert not full.depth_limited
    assert {entry.kind for entry in full.entries} == {"dir", "file"}
    assert next(entry for entry in full.entries if entry.display == "top.txt").size == 1

    subtree = walk_tree(root / "a", root, max_depth=1, max_entries=100)
    assert [entry.display for entry in subtree.entries] == ["a/b"]
    for invalid in ({"max_depth": 0}, {"max_entries": 0}):
        with pytest.raises(ValueError):
            walk_tree(root, root, **{"max_depth": 1, "max_entries": 1, **invalid})
    with pytest.raises(ValueError):
        walk_tree(tmp_path / "top.txt", root, max_depth=1, max_entries=1)


def test_walk_stops_at_the_entry_limit_deterministically(tmp_path):
    root = tmp_path.resolve()
    for index in range(7):
        write(root / f"file-{index}.txt")
    write(root / "folder" / "inner.txt")

    first = walk_tree(root, root, max_depth=3, max_entries=5)
    again = walk_tree(root, root, max_depth=3, max_entries=5)
    assert first == again
    assert [entry.display for entry in first.entries] == [f"file-{index}.txt" for index in range(5)]
    assert first.truncated

    exact = walk_tree(root, root, max_depth=1, max_entries=8)
    assert len(exact.entries) == 8 and not exact.truncated

    files_only = walk_tree(root, root, max_depth=3, max_entries=8, include_dirs=False)
    assert all(entry.kind == "file" for entry in files_only.entries)
    assert [entry.display for entry in files_only.entries][-1] == "folder/inner.txt"
    assert not files_only.truncated


def test_ignored_directories_are_neither_listed_nor_entered(tmp_path):
    root = tmp_path.resolve()
    assert DEFAULT_IGNORED_DIRS == {".git", "node_modules", "venv", "__pycache__", ".trash"}
    for name in DEFAULT_IGNORED_DIRS:
        write(root / name / "hidden.txt")
    write(root / "keep" / "kept.txt")
    write(root / "venv.txt")
    (root / "linked").symlink_to(root / "keep", target_is_directory=True)

    result = walk_tree(root, root, max_depth=5, max_entries=100)
    displays = [entry.display for entry in result.entries]
    assert displays == ["keep", "linked", "venv.txt", "keep/kept.txt"]
    assert not any("hidden.txt" in display for display in displays)
    assert result.ignored_dirs == len(DEFAULT_IGNORED_DIRS)
    assert next(entry for entry in result.entries if entry.display == "linked").kind == "symlink"

    custom = walk_tree(root, root, max_depth=5, max_entries=100, ignored_dirs={"keep"})
    assert "keep" not in [entry.display for entry in custom.entries]
    assert ".git/hidden.txt" in [entry.display for entry in custom.entries]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read directories without permission bits")
def test_unreadable_directories_are_counted_not_fatal(tmp_path):
    root = tmp_path.resolve()
    locked = root / "locked"
    write(locked / "secret.txt")
    locked.chmod(0)
    try:
        result = walk_tree(root, root, max_depth=3, max_entries=10)
    finally:
        locked.chmod(0o755)
    assert [entry.display for entry in result.entries] == ["locked"]
    assert result.unreadable_dirs == 1
