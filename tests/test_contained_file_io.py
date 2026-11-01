"""DLA-03: qualified file.read, file.write and file.append keep their containment through the I/O.

Every test works only inside pytest's temporary directory. A swap "after
admission" is simulated by wrapping ``resolve_tool_path``: the path is admitted
first, then a folder (or the tool root) is replaced by a symlink to a folder
outside the root, exactly the window the audit describes. Outside files must
stay untouched, and approved in-root operations must keep working.
"""

import os
import threading
import time

import pytest

import davellm_contained_io


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "docs").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("outside secret")
    return root, outside


@pytest.fixture
def router(router_factory, tree):
    loaded, _, _ = router_factory(tools=True, tool_roots=[str(tree[0])])
    return loaded


def swap_after_admission(router, monkeypatch, swap):
    admit = router.resolve_tool_path

    def admitted_then_swapped(path):
        safe = admit(path)
        swap()
        return safe

    monkeypatch.setattr(router, "resolve_tool_path", admitted_then_swapped)


def replace_with_symlink(folder, target):
    folder.rename(folder.with_name(folder.name + "-moved"))
    folder.symlink_to(target, target_is_directory=True)


def test_in_root_reads_writes_and_appends_still_work(router, tree):
    root, _ = tree
    assert router.tool_file_write({"path": str(root / "new/deeper/out.txt"), "content": "one"}).status == "success"
    assert router.tool_file_append({"path": str(root / "new/deeper/out.txt"), "content": "\ntwo"}).status == "success"
    assert (root / "new/deeper/out.txt").read_text() == "one\ntwo"
    assert router.tool_file_write({"path": str(root / "new/deeper/out.txt"), "content": "three"}).status == "success"
    assert router.tool_file_read({"path": str(root / "new/deeper/out.txt")}).result == "three"
    (root / "docs/crlf.txt").write_bytes(b"a\r\nb")
    assert router.tool_file_read({"path": str(root / "docs/crlf.txt")}).result == "a\nb"
    # An in-root symlink is resolved at admission and its in-root target is used.
    (root / "alias.txt").symlink_to(root / "docs/crlf.txt")
    assert router.tool_file_read({"path": str(root / "alias.txt")}).result == "a\nb"
    assert router.tool_file_append({"path": str(root / "alias.txt"), "content": "!"}).status == "success"
    assert (root / "docs/crlf.txt").read_bytes() == b"a\r\nb!"


@pytest.mark.parametrize("tool", ["tool_file_write", "tool_file_append"])
def test_a_folder_swapped_for_a_symlink_after_admission_cannot_redirect_a_write(router, tree, monkeypatch, tool):
    root, outside = tree
    swap_after_admission(router, monkeypatch, lambda: replace_with_symlink(root / "docs", outside))
    result = getattr(router, tool)({"path": str(root / "docs/secret.txt"), "content": "planted"})
    assert result.status == "error"
    assert result.error == davellm_contained_io.SYMLINK_IN_PATH
    assert (outside / "secret.txt").read_text() == "outside secret"
    assert sorted(path.name for path in outside.iterdir()) == ["secret.txt"]


def test_a_folder_swapped_for_a_symlink_after_admission_cannot_redirect_a_read(router, tree, monkeypatch):
    root, outside = tree
    (root / "docs/secret.txt").write_text("in-root text")
    swap_after_admission(router, monkeypatch, lambda: replace_with_symlink(root / "docs", outside))
    result = router.tool_file_read({"path": str(root / "docs/secret.txt")})
    assert (result.status, result.result) == ("error", "")
    assert result.error == davellm_contained_io.SYMLINK_IN_PATH


def test_a_new_parent_folder_swapped_for_a_symlink_is_not_followed(router, tree, monkeypatch):
    root, outside = tree
    swap_after_admission(router, monkeypatch, lambda: (root / "made").symlink_to(outside, target_is_directory=True))
    result = router.tool_file_write({"path": str(root / "made/new.txt"), "content": "planted"})
    assert (result.status, result.error) == ("error", davellm_contained_io.SYMLINK_IN_PATH)
    assert not (outside / "new.txt").exists()


def test_the_tool_root_swapped_for_a_symlink_after_admission_is_refused(router, tree, monkeypatch):
    root, outside = tree
    swap_after_admission(router, monkeypatch, lambda: replace_with_symlink(root, outside))
    result = router.tool_file_write({"path": str(root / "secret.txt"), "content": "planted"})
    assert (result.status, result.error) == ("error", davellm_contained_io.SYMLINK_IN_PATH)
    assert (outside / "secret.txt").read_text() == "outside secret"


@pytest.mark.parametrize("tool, params", [
    ("tool_file_read", {}),
    ("tool_file_write", {"content": "planted"}),
    ("tool_file_append", {"content": "planted"}),
])
def test_a_hard_link_to_an_outside_file_is_refused(router, tree, tool, params):
    root, outside = tree
    os.link(outside / "secret.txt", root / "docs/linked.txt")
    result = getattr(router, tool)({"path": str(root / "docs/linked.txt"), **params})
    assert (result.status, result.result, result.error) == ("error", "", davellm_contained_io.MULTIPLE_LINKS)
    assert (outside / "secret.txt").read_text() == "outside secret"


def finishes_promptly(call):
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(call()), daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(5)
    return outcome, time.monotonic() - started


@pytest.mark.parametrize("tool, params, release", [
    ("tool_file_read", {}, os.O_WRONLY | os.O_NONBLOCK),
    ("tool_file_write", {"content": "planted"}, os.O_RDONLY | os.O_NONBLOCK),
])
def test_a_fifo_target_is_refused_without_blocking(router, tree, tool, params, release):
    root, _ = tree
    pipe = root / "docs/pipe"
    os.mkfifo(pipe)
    outcome, elapsed = finishes_promptly(lambda: getattr(router, tool)({"path": str(pipe), **params}))
    try:  # release an open stuck on the pipe, so a regression fails instead of hanging the suite
        os.close(os.open(pipe, release))
    except OSError:
        pass
    assert [(item.status, item.error) for item in outcome] == [("error", davellm_contained_io.NOT_REGULAR)]
    assert elapsed < 5


def test_reads_over_the_limit_are_refused_on_the_opened_file(router, tree, monkeypatch):
    root, _ = tree
    (root / "docs/big.txt").write_text("x" * 32)
    monkeypatch.setattr(router, "MAX_FILE_SIZE", 16)
    result = router.tool_file_read({"path": str(root / "docs/big.txt")})
    assert (result.status, result.error) == ("error", "File too large (max 0MB)")


def test_paths_outside_every_root_are_refused_by_the_io_layer(tree):
    root, outside = tree
    for path in (outside / "secret.txt", root):
        with pytest.raises(PermissionError, match="outside DAVE_TOOL_ROOTS"):
            davellm_contained_io.contained_read_text(path, [root], 1024)


def test_the_path_checked_fallback_keeps_the_same_refusals(tree, monkeypatch):
    root, outside = tree
    monkeypatch.setattr(davellm_contained_io, "DESCRIPTOR_IO", False)
    davellm_contained_io.contained_write_text(root / "fresh/out.txt", [root], "ok", append=False)
    davellm_contained_io.contained_write_text(root / "fresh/out.txt", [root], "!", append=True)
    assert davellm_contained_io.contained_read_text(root / "fresh/out.txt", [root], 1024) == "ok!"
    os.link(outside / "secret.txt", root / "docs/linked.txt")
    with pytest.raises(PermissionError, match="more than one hard link"):
        davellm_contained_io.contained_write_text(root / "docs/linked.txt", [root], "planted", append=False)
    replace_with_symlink(root / "docs", outside)
    with pytest.raises(PermissionError, match="symbolic link"):
        davellm_contained_io.contained_read_text(root / "docs/secret.txt", [root], 1024)
    with pytest.raises(FileNotFoundError):
        davellm_contained_io.contained_read_text(root / "missing.txt", [root], 1024)
    assert (outside / "secret.txt").read_text() == "outside secret"
