"""Adversarial probes for the Notion adapter, added run by run; each one stays as a regression test.

Run 1: uncertain writes and cancellation.
"""

import asyncio

import pytest
import respx

import davellm_notion as notion
from davellm_notion import NotionToolError, append_blocks, read_page, update_block
from fake_notion import rt
from test_notion_tools import BLOCKS, World


def run_in_fake(world, scenario):
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        world.fake.mount(mock)
        return asyncio.run(scenario())


async def drain_writes():
    while notion._WRITES_IN_FLIGHT:
        await asyncio.sleep(0.01)


def kwargs(world, *, write=True):
    base = {"settings": world.settings, "ledger": world.ledger}
    return {**base, "guard": world.guard} if write else base


# ---- Run 1: uncertain writes and cancellation ----------------------------------

def test_r1_append_cut_off_by_the_caller_lands_once_and_is_never_repeated():
    world = World()
    world.fake.delay("PATCH", r"/blocks/.*/children", 0.3, apply_first=True)
    arguments = {"page": "adapter-test", "blocks": BLOCKS}

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(append_blocks(arguments, **kwargs(world)), 0.05)
        assert world.guard.busy(world.page), "the shielded write must still hold the page"
        await drain_writes()
        assert not world.guard.busy(world.page)
        with pytest.raises(NotionToolError) as caught:
            await append_blocks(arguments, **kwargs(world))
        return str(caught.value)

    message = run_in_fake(world, scenario)
    assert len(world.fake.writes()) == 1
    assert len(world.fake.page_texts(world.page)) == 3
    assert "earlier append of these exact blocks" in message


def test_r1_edit_cut_off_by_the_caller_finishes_and_a_retry_cannot_apply_twice():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("version one")])
    world.read()
    ref = world.ref(block)
    world.fake.delay("PATCH", rf"/blocks/{block}", 0.3, apply_first=True)
    arguments = {"page": "adapter-test", "block": ref, "old_text": "one", "new_text": "two"}

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(update_block(arguments, **kwargs(world)), 0.05)
        await drain_writes()
        with pytest.raises(NotionToolError) as caught:
            await update_block(arguments, **kwargs(world))
        return str(caught.value)

    message = run_in_fake(world, scenario)
    assert message == f"old_text was not found in block {ref}"
    assert world.fake.text_of(block) == "version two"
    assert len(world.fake.writes()) == 1
    assert notion.rich_text_plain(world.ledger.get(ref).rich_text) == "version two"


def test_r1_a_failure_before_sending_is_failed_not_unknown(monkeypatch):
    world = World()

    def broken(*_args):
        raise RuntimeError("bug before the request")

    monkeypatch.setattr(notion, "_child", broken)
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message.startswith("Nothing was written:"), message
    assert world.fake.writes() == []
    monkeypatch.undo()
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"


def test_r1_an_edit_failure_before_sending_is_failed_not_unknown(monkeypatch):
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("text")])
    world.read()

    async def broken(*_args):
        raise RuntimeError("bug before the request")

    monkeypatch.setattr(notion, "_owning_page", broken)
    message = world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text="text", new_text="x")
    assert message.startswith("Nothing was written:"), message
    assert world.fake.writes() == []


CHILDREN = r"/blocks/[0-9a-f-]+/children"


@pytest.mark.parametrize("method, pattern, mode, options, expected, repeatable", [
    ("GET", CHILDREN, "reject", {}, "Nothing was written: Notion refused to read the page before writing (validation_error)", True),
    ("GET", CHILDREN, "connect_error", {}, "Nothing was written: Notion could not be reached to read the page before writing", True),
    ("GET", CHILDREN, "drop_before_apply", {}, "Nothing was written: Notion could not be reached to read the page before writing", True),
    ("GET", CHILDREN, "server_error_before_apply", {}, "Nothing was written: Notion could not be reached to read the page before writing", True),
    ("GET", CHILDREN, "rate_limit", {}, "verified", None),
    ("PATCH", CHILDREN, "server_error_before_apply", {}, "Outcome unknown: the connection to Notion failed during the append and the blocks are not on the page yet", False),
    ("PATCH", CHILDREN, "reject", {"status": 403, "code": "restricted_resource"}, "Nothing was written: Notion refused the append (restricted_resource)", True),
    ("PATCH", CHILDREN, "rate_limit", {"retry_after": "30"}, "Nothing was written: Notion refused the append (rate_limited)", True),
    ("GET", CHILDREN, "connect_error", {"skip": 1}, "Outcome unknown: Notion accepted the append, but reading the page back failed", False),
    ("GET", r"/users/me", "reject", {}, "Outcome unknown: the connection to Notion failed during the append and the page could not be checked", False),
])
def test_r1_append_fault_at_every_request_position(method, pattern, mode, options, expected, repeatable):
    world = World()
    if pattern == r"/users/me":
        world.fake.fail("PATCH", CHILDREN, "drop_after_apply")
    world.fake.fail(method, pattern, mode, **options)
    if expected == "verified":
        assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"
        assert not world.guard.busy(world.page)
        return
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message.startswith(expected), message
    assert not world.guard.busy(world.page)
    if repeatable:
        assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"
        assert len(world.fake.page_texts(world.page)) == 3
    else:
        assert "earlier append of these exact blocks" in world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)


@pytest.mark.parametrize("target, method, mode, options, expected", [
    ("block", "GET", "reject", {"status": 404, "code": "object_not_found"}, "Nothing was written: Notion refused to read block {ref} before writing (object_not_found)"),
    ("block", "GET", "connect_error", {}, "Nothing was written: Notion could not be reached to check block {ref} before writing"),
    ("parent", "GET", "drop_before_apply", {}, "Nothing was written: Notion could not be reached to check block {ref} before writing"),
    ("block", "PATCH", "server_error_before_apply", {}, "Outcome unknown: the connection to Notion failed during the edit and block {ref} still shows the old content"),
    ("block", "PATCH", "server_error_after_apply", {}, "verified"),
    ("block", "PATCH", "connect_error", {}, "Nothing was written: the request could not be sent to Notion"),
    ("block", "GET", "connect_error", {"skip": 1}, "Outcome unknown: Notion accepted the edit, but reading the block back failed"),
])
def test_r1_edit_fault_at_every_request_position(target, method, mode, options, expected):
    world = World()
    holder = world.fake.add_block(world.page, "toggle", [rt("Holder")])
    block = world.fake.add_block(holder, "paragraph", [rt("nested text")])
    world.read()
    ref = world.ref(block)
    world.fake.fail(method, rf"/blocks/{holder if target == 'parent' else block}", mode, **options)
    if expected == "verified":
        assert world.call(update_block, page="adapter-test", block=ref, old_text="nested", new_text="moved")[
            "outcome"] == "verified"
    else:
        message = world.refuse(update_block, page="adapter-test", block=ref, old_text="nested", new_text="moved")
        assert message.startswith(expected.format(ref=ref)), message
    assert not world.guard.busy(world.page)


def test_r1_an_in_flight_append_refuses_an_identical_repeat_with_its_own_message():
    world = World()
    world.fake.delay("PATCH", r"/blocks/.*/children", 0.3, apply_first=True)
    arguments = {"page": "adapter-test", "blocks": BLOCKS}

    async def scenario():
        first = asyncio.ensure_future(append_blocks(arguments, **kwargs(world)))
        await asyncio.sleep(0.05)
        with pytest.raises(NotionToolError) as caught:
            await append_blocks(arguments, **kwargs(world))
        return str(caught.value), await first

    message, first = run_in_fake(world, scenario)
    assert message.startswith("An earlier append of these exact blocks to 'adapter-test' in this run is still being")
    assert first["outcome"] == "verified"
    assert len(world.fake.writes()) == 1


def test_r1_a_busy_refusal_keeps_the_earlier_record():
    world = World()
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"
    digest = notion._append_digest("adapter-test", notion._append_blocks_argument(BLOCKS))
    assert world.guard.acquire(world.page)
    assert world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS) == notion.PAGE_BUSY
    world.guard.release(world.page)
    assert world.ledger.append_outcome("adapter-test", digest) == "verified"


# ---- Run 2: scope and ownership --------------------------------------------------

@pytest.mark.parametrize("ref", ["b01", "B1", " b1", "b1 ", "b１", "b0", "b-1", "", None, 1, ["b1"], "b1\x00"])
def test_r2_refs_match_exactly_or_are_refused_without_network(ref):
    world = World()
    world.fake.add_block(world.page, "paragraph", [rt("text")])
    world.read()
    before = len(world.fake.requests)
    message = world.refuse(update_block, page="adapter-test", block=ref, old_text="text", new_text="x")
    assert message.startswith("Unknown block ref ")
    assert len(world.fake.requests) == before


def test_r2_a_block_moved_into_a_child_page_is_off_the_page():
    world = World()
    child = world.fake.add_page("Sub page")
    world.fake.add_block(world.page, "child_page", body={"title": "Sub page"}, block_id=child)
    block = world.fake.add_block(world.page, "paragraph", [rt("movable")])
    world.read()
    world.fake.move(block, child)
    message = world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text="movable", new_text="x")
    assert message == f"Nothing was written: block {world.ref(block)} is no longer on page 'adapter-test'"
    assert world.fake.writes() == []


def test_r2_a_block_moved_between_configured_pages_is_refused_under_either_name():
    world = World(second_page=True)
    block = world.fake.add_block(world.other, "paragraph", [rt("from other")])
    world.read("other")
    ref = world.ref(block)
    world.fake.move(block, world.page)
    assert world.refuse(update_block, page="other", block=ref, old_text="from", new_text="x") == \
        f"Nothing was written: block {ref} is no longer on page 'other'"
    assert world.refuse(update_block, page="adapter-test", block=ref, old_text="from", new_text="x") == \
        f"Block {ref} belongs to page 'other', not 'adapter-test'"
    assert world.fake.writes() == []


def test_r2_a_block_turned_into_another_type_after_the_read_is_not_written():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("Title text")])
    world.read()
    stored = world.fake.blocks[block]
    stored["heading_2"] = stored.pop("paragraph")
    stored["type"] = "heading_2"
    message = world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text="Title", new_text="New")
    assert message == f"Nothing was written: block {world.ref(block)} changed after it was read; read the page again"
    assert world.fake.writes() == []


def test_r2_child_pages_cannot_be_renamed_or_checked_through_a_ref():
    world = World()
    child = world.fake.add_block(world.page, "child_page", body={"title": "Sub page"})
    world.read()
    ref = world.ref(child)
    assert world.refuse(update_block, page="adapter-test", block=ref, old_text="Sub", new_text="x") == \
        f"Block {ref} is a child_page block; only text blocks can be edited"
    assert world.refuse(update_block, page="adapter-test", block=ref, checked=True, block_text="Sub page") == \
        f"Block {ref} is a child_page block; only to-do blocks can be checked or unchecked"
    assert world.fake.writes() == []


def test_r2_a_page_trashed_after_the_read_is_not_written():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("text")])
    world.read()
    world.fake.pages[world.page]["in_trash"] = True
    assert world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS) == \
        "Nothing was written: Notion page 'adapter-test' is in the trash"
    assert world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text="text", new_text="x") == \
        "Nothing was written: Notion page 'adapter-test' is in the trash"
    assert world.fake.writes() == []


def test_r2_a_block_trashed_after_the_read_is_not_written():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("text")])
    world.read()
    world.fake.blocks[block]["in_trash"] = True
    assert world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text="text", new_text="x") == \
        f"Nothing was written: block {world.ref(block)} is in the trash"
    assert world.fake.writes() == []


def test_r2_checking_a_to_do_names_the_to_do_so_the_approval_shows_which_one():
    world = World()
    first = world.fake.add_block(world.page, "to_do", [rt("Ship v1")], checked=False)
    world.fake.add_block(world.page, "to_do", [rt("Ship v2")], checked=False)
    world.read()
    ref = world.ref(first)
    assert world.refuse(update_block, page="adapter-test", block=ref, checked=True) == \
        "block_text is required when only checked changes: copy the to-do's text from notion.page.read"
    assert world.refuse(update_block, page="adapter-test", block=ref, checked=True, block_text="Ship v2") == \
        f"block_text does not match block {ref}; read the page again"
    assert world.fake.writes() == []
    assert world.call(update_block, page="adapter-test", block=ref, checked=True, block_text="Ship v1")[
        "outcome"] == "verified"


def test_r2_refs_belong_to_one_run_even_for_the_same_page():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("shared text")])
    world.read()
    other_run = notion.RunLedger()
    assert world.refuse(update_block, other_run, page="adapter-test", block=world.ref(block),
                        old_text="shared", new_text="x").startswith("Unknown block ref")
    extra = world.fake.add_block(world.page, "paragraph", [rt("added later")])
    world.fake.children[world.page].remove(extra)
    world.fake.children[world.page].insert(0, extra)
    world.call(read_page, other_run, page="adapter-test")
    assert other_run._refs_by_block[extra] == "b1" and other_run._refs_by_block[block] == "b2"
    assert world.ledger._refs_by_block[block] == "b1"
    assert world.call(update_block, other_run, page="adapter-test", block="b1", old_text="added", new_text="later")[
        "outcome"] == "verified"
    assert world.fake.text_of(block) == "shared text"


def test_r2_nesting_beyond_the_walk_limit_is_refused_with_an_accurate_reason():
    world = World()
    parent = world.page
    chain = []
    for depth in range(notion.NOTION_PARENT_MAX_DEPTH + 1):
        parent = world.fake.add_block(parent, "toggle", [rt(f"level {depth}")])
        chain.append(parent)
    deep = world.fake.add_block(parent, "paragraph", [rt("deep text")])
    record = world.ledger.remember("adapter-test", world.page, world.fake.blocks[deep])
    message = world.refuse(update_block, page="adapter-test", block=record.ref, old_text="deep", new_text="x")
    assert message == (f"Nothing was written: block {record.ref} is nested too deeply to confirm it is still "
                       "on page 'adapter-test'")
    assert world.fake.writes() == []


def test_r2_a_database_configured_as_a_page_is_refused_cleanly():
    world = World()
    world.settings = notion.NotionSettings.from_values(
        world.settings.token, '{"adapter-test": "0123456789abcdef0123456789abcdef"}')
    assert world.refuse(read_page, page="adapter-test") == "Notion refused the read (object_not_found)"


# ---- Run 3: rich-text fidelity -----------------------------------------------------

from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

from fake_notion import ANNOTATIONS, FakeNotion, date_mention, equation, page_mention, user_mention  # noqa: E402

TOKENS = ["a", "b", "Z", " ", ".", "-", "😀", "漢", "é", "👨‍👩‍👧", "\n", "\t", "<", "&", "’"]
COLORS = ["default", "red", "blue_background", "gray"]
PROPERTY = settings(max_examples=250, deadline=None, derandomize=True, database=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])

texts = st.lists(st.sampled_from(TOKENS), min_size=1, max_size=12).map("".join)
annotations = st.fixed_dictionaries({
    "bold": st.booleans(), "italic": st.booleans(), "strikethrough": st.booleans(),
    "underline": st.booleans(), "code": st.booleans(), "color": st.sampled_from(COLORS),
})
PAGE_ID = "0b0b0b0b-0b0b-4b0b-8b0b-0b0b0b0b0b0b"
USER_ID = "0c0c0c0c-0c0c-4c0c-8c0c-0c0c0c0c0c0c"


@st.composite
def runs(draw):
    kind = draw(st.sampled_from(["text"] * 6 + ["page", "user", "date", "equation"]))
    if kind == "text":
        link = draw(st.sampled_from([None, None, "https://example.com/x"]))
        return rt(draw(texts), link=link, **draw(annotations))
    if kind == "page":
        return {**page_mention(PAGE_ID, "Linked"), "annotations": {**ANNOTATIONS, **draw(annotations)}}
    if kind == "user":
        return user_mention(USER_ID, "Dave")
    if kind == "date":
        return date_mention(draw(st.sampled_from(["2026-10-01", "2026-10-01T09:00:00.000-04:00"])))
    return equation(draw(st.sampled_from(["E=mc^2", "a+b"])))


rich_texts = st.lists(runs(), min_size=1, max_size=8)


def as_saved(entries):
    """What Notion would store and return for request items."""
    return FakeNotion(mention_titles={PAGE_ID: "Linked"}).response_rich_text(entries)


def utf16(text):
    return len(text.encode("utf-16-le")) // 2


def occurrences(text, needle):
    """Every occurrence, overlapping ones included: "aaa" holds "aa" twice."""
    return sum(1 for start in range(len(text) - len(needle) + 1) if text.startswith(needle, start))


@PROPERTY
@given(rich_texts, st.data(), st.lists(st.sampled_from(TOKENS), max_size=6).map("".join))
def test_r3_an_edit_changes_only_the_target_text_and_keeps_every_run(rich, data, new):
    index = data.draw(st.sampled_from([i for i, item in enumerate(rich) if item["type"] == "text"] or [-1]))
    assume(index >= 0)
    content = rich[index]["text"]["content"]
    start = data.draw(st.integers(0, len(content) - 1))
    end = data.draw(st.integers(start + 1, len(content)))
    old = content[start:end]
    full = notion.rich_text_plain(rich)
    assume(occurrences(full, old) == 1 and old != new)
    written = notion.rewrite_rich_text(rich, old, new, "b1")
    expected = [dict(item) for item in rich]
    expected[index] = rt(content[:start] + new + content[end:], link=rich[index]["text"]["link"] and
                         rich[index]["text"]["link"]["url"], **{k: v for k, v in rich[index]["annotations"].items()})
    assert notion.rich_text_signature(as_saved(written)) == notion.rich_text_signature(expected)
    assert len(written) <= notion.NOTION_RICH_TEXT_MAX_ITEMS
    assert all(utf16(item["text"]["content"]) <= notion.NOTION_TEXT_MAX_CHARS
               for item in written if item["type"] == "text")


@PROPERTY
@given(rich_texts, st.data())
def test_r3_text_that_spans_two_runs_is_refused_never_rewritten(rich, data):
    full = notion.rich_text_plain(rich)
    assume(len(full) >= 2)
    signature = notion.rich_text_signature(rich)
    assume(len(signature) >= 2)
    boundary = len(signature[0][1]) if signature[0][0] == "text" else len(notion.rich_text_plain(rich[:1]))
    assume(0 < boundary < len(full))
    start = data.draw(st.integers(0, boundary - 1))
    end = data.draw(st.integers(boundary + 1, len(full)))
    old = full[start:end]
    assume(occurrences(full, old) == 1)
    with pytest.raises(NotionToolError) as caught:
        notion.rewrite_rich_text(rich, old, "x", "b1")
    assert "crosses a formatting change" in str(caught.value) or "cannot be written back" in str(caught.value)


@PROPERTY
@given(rich_texts, st.data())
def test_r3_the_signature_ignores_how_notion_splits_uniform_text(rich, data):
    index = data.draw(st.sampled_from([i for i, item in enumerate(rich) if item["type"] == "text"] or [-1]))
    assume(index >= 0)
    item = rich[index]
    content = item["text"]["content"]
    assume(len(content) >= 2)
    cut = data.draw(st.integers(1, len(content) - 1))
    link = item["text"]["link"] and item["text"]["link"]["url"]
    split = rich[:index] + [rt(content[:cut], link=link, **item["annotations"]),
                            rt(content[cut:], link=link, **item["annotations"])] + rich[index + 1:]
    assert notion.rich_text_signature(split) == notion.rich_text_signature(rich)


@settings(max_examples=40, deadline=None, derandomize=True, database=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
@given(rich_texts, st.data(), st.lists(st.sampled_from(TOKENS), max_size=6).map("".join))
def test_r3_round_trip_through_notion_is_verified_exactly_when_saved_as_expected(rich, data, new):
    world = World()
    world.fake.mention_titles[PAGE_ID] = "Linked"
    block = world.fake.add_block(world.page, "paragraph", rich)
    full = notion.rich_text_plain(rich)
    index = data.draw(st.sampled_from([i for i, item in enumerate(rich) if item["type"] == "text"] or [-1]))
    assume(index >= 0)
    content = rich[index]["text"]["content"]
    start = data.draw(st.integers(0, len(content) - 1))
    old = content[start:data.draw(st.integers(start + 1, len(content)))]
    assume(occurrences(full, old) == 1 and old != new)
    world.read()
    result = world.call(update_block, page="adapter-test", block=world.ref(block), old_text=old, new_text=new)
    assert result["outcome"] == "verified"
    saved = world.fake.blocks[block]["paragraph"]["rich_text"]
    assert notion.rich_text_plain(saved) == full.replace(old, new, 1)
    others = [run for run in notion.rich_text_signature(saved) if run[0] != "text"]
    assert others == [run for run in notion.rich_text_signature(rich) if run[0] != "text"]


def test_r3_text_is_split_by_utf16_length_so_emoji_heavy_text_is_accepted():
    world = World()
    text = "😀" * 1500 + "tail"  # 1504 code points, 3004 UTF-16 units
    result = world.call(append_blocks, page="adapter-test", blocks=[{"type": "paragraph", "text": text}])
    assert result["outcome"] == "verified"
    assert world.fake.page_texts(world.page) == [("paragraph", text)]
    block = world.fake.children[world.page][0]
    assert [utf16(item["text"]["content"]) for item in world.fake.blocks[block]["paragraph"]["rich_text"]] == [2000, 1004]
    world.read()
    grown = "😀" * 600 + "tail"
    assert world.call(update_block, page="adapter-test", block=world.ref(block), old_text="tail", new_text=grown)[
        "outcome"] == "verified"
    saved = world.fake.blocks[block]["paragraph"]["rich_text"]
    assert all(utf16(item["text"]["content"]) <= 2000 for item in saved)
    assert notion.rich_text_plain(saved) == "😀" * 2100 + "tail"


def test_r3_deleting_a_block_s_only_text_leaves_an_empty_block():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("remove me", bold=True)])
    world.read()
    result = world.call(update_block, page="adapter-test", block=world.ref(block), old_text="remove me", new_text="")
    assert result["outcome"] == "verified"
    assert world.fake.blocks[block]["paragraph"]["rich_text"] == []


@pytest.mark.parametrize("text, old", [("aaa", "aa"), ("babab", "bab"), ("😀😀😀", "😀😀"), ("xyxyx", "xyx")])
def test_r3_overlapping_occurrences_are_ambiguous_and_refused(text, old):
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt(text)])
    world.read()
    message = world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text=old, new_text="Q")
    assert message == f"old_text occurs 2 times in block {world.ref(block)}; include more surrounding text so it occurs once"
    assert world.fake.writes() == []
