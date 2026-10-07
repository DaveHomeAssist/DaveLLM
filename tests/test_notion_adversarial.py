"""Adversarial probes for the Notion adapter, added run by run; each one stays as a regression test.

Run 1: uncertain writes and cancellation.
"""

import asyncio

import pytest
import respx

import davellm_notion as notion
from davellm_notion import NotionToolError, append_blocks, read_page, update_block
from fake_notion import new_id, rt
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
    ("GET", CHILDREN, "server_error_before_apply", {"times": 2}, "Nothing was written: Notion could not be reached to read the page before writing", True),
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


def test_r1_a_busy_refusal_keeps_the_earlier_record(monkeypatch):
    monkeypatch.setattr(notion, "NOTION_PAGE_WAIT_SECONDS", 0.2)
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
    assert world.refuse(read_page, page="adapter-test") == (
        "Notion refused the read (object_not_found): share the page with DaveLLM's Notion connection")


# ---- Run 3: rich-text fidelity -----------------------------------------------------

from hypothesis import HealthCheck, assume, given, settings, strategies as st  # noqa: E402

from fake_notion import ANNOTATIONS, FakeNotion, date_mention, equation, page_mention, user_mention  # noqa: E402,F401

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


# ---- Run 4: transport and secrets ----------------------------------------------------

import json as _json  # noqa: E402
import logging  # noqa: E402

import httpx  # noqa: E402

from test_notion_tools import TOKEN, _model_turns, _persistent_loop, _settled  # noqa: E402


def test_r4_the_client_ignores_proxy_settings_and_never_follows_redirects(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent.pem")
    world = World()
    world.fake.add_block(world.page, "paragraph", [rt("through no proxy")])
    assert world.read()["blocks"][0]["text"] == "through no proxy"
    assert {(request.url.scheme, request.url.host, request.url.port) for request in world.fake.requests} == {
        ("https", "api.notion.com", None)}

    async def inspect_client():
        async with notion.NotionApi(TOKEN, budget_seconds=5, max_requests=1) as api:
            return api._client.follow_redirects, api._client._trust_env, dict(api._client._mounts)

    assert asyncio.run(inspect_client()) == (False, False, {})


def test_r4_a_redirected_write_is_not_followed_and_the_token_goes_nowhere_else():
    world = World()
    seen_hosts = []
    world.fake.on_request = lambda request: seen_hosts.append(request.url.host)
    world.fake.fail("PATCH", CHILDREN, "redirect")
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message.startswith("Outcome unknown:")
    assert set(seen_hosts) == {"api.notion.com"}
    assert world.fake.page_texts(world.page) == []


def test_r4_an_oversized_response_is_reported_as_such():
    world = World()
    original = world.fake.handle

    def huge(request):
        if request.url.path.endswith("/children"):
            world.fake.requests.append(request)
            return httpx.Response(200, content=b'{"results": [' + b" " * (notion.NOTION_MAX_RESPONSE_BYTES + 10) + b"]}")
        return original(request)

    world.fake.handle = huge
    assert world.refuse(read_page, page="adapter-test") == "Notion's response was larger than the 2 MB limit"


@pytest.mark.parametrize("status, code, expected", [
    (401, "unauthorized", "Notion refused the read (unauthorized): check DAVE_NOTION_TOKEN"),
    (403, "restricted_resource", "Notion refused the read (restricted_resource): share the page with DaveLLM's Notion connection"),
    (404, "object_not_found", "Notion refused the read (object_not_found): share the page with DaveLLM's Notion connection"),
    (400, "validation_error", "Notion refused the read (validation_error)"),
    (409, "conflict_error", "Notion refused the read (conflict_error)"),
])
def test_r4_refusals_name_the_code_and_the_operator_fix(status, code, expected):
    world = World()
    world.fake.fail("GET", r"/pages/.*", "reject", status=status, code=code)
    assert world.refuse(read_page, page="adapter-test") == expected


@pytest.mark.parametrize("token", ["secret\r\nX-Evil: 1", "has space", "tab\tinside", "\x00nul", "é-unicode"])
def test_r4_a_malformed_token_is_a_configuration_error_and_nothing_is_sent(token):
    world = World()
    world.settings = notion.NotionSettings.from_values(token, '{"adapter-test": "%s"}' % world.page)
    assert world.refuse(read_page, page="adapter-test") == "DAVE_NOTION_TOKEN is not a valid Notion secret"
    assert world.fake.requests == []


@pytest.mark.parametrize("code", ["<script>alert(1)</script>", "x" * 500, "Validation Error", 42, None])
def test_r4_untrusted_error_codes_are_reduced_to_a_fixed_word(code):
    world = World()
    original = world.fake.handle

    def odd(request):
        world.fake.requests.append(request)
        return httpx.Response(400, json={"object": "error", "status": 400, "code": code, "message": "page title"})

    world.fake.handle = odd
    assert world.refuse(read_page, page="adapter-test") == "Notion refused the read (error)"


def test_r4_cursors_and_ids_from_responses_cannot_steer_requests():
    world = World()
    seen = []

    def crafted(request):
        seen.append(str(request.url))
        path = request.url.path.removeprefix("/v1")
        if path == f"/pages/{world.page}":
            return httpx.Response(200, json={"object": "page", "id": world.page, "in_trash": False, "properties": {}})
        if request.url.params.get("start_cursor"):
            return httpx.Response(200, json={"results": [], "has_more": False, "next_cursor": None})
        return httpx.Response(200, json={"results": [
            {"object": "block", "id": "../../users/me", "type": "toggle", "has_children": True, "toggle": {"rich_text": []}},
            {"object": "block", "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "type": "paragraph",
             "has_children": False, "paragraph": {"rich_text": [rt("ok")]}},
        ], "has_more": True, "next_cursor": "abc&page_size=1000#frag"})

    world.fake.handle = crafted
    result = world.read()
    assert [entry["text"] for entry in result["blocks"]] == ["ok"]
    assert all("users/me" not in url for url in seen)
    assert any("start_cursor=abc%26page_size%3D1000%23frag" in url for url in seen)


@pytest.mark.parametrize("delayed_response", [False, True], ids=["immediate", "after-post"])
def test_r4_the_token_never_reaches_logs_routes_events_or_the_transcript(
    router_factory, monkeypatch, caplog, delayed_response,
):
    from threading import Event

    caplog.set_level(logging.DEBUG)
    fake = FakeNotion()
    page = fake.add_page("DaveLLM Adapter Test")
    fake.add_block(page, "to_do", [rt("Prove the adapter")], checked=False)
    monkeypatch.setenv("DAVE_ENABLE_NOTION_TOOLS", "true")
    monkeypatch.setenv("DAVE_NOTION_TOKEN", TOKEN)
    monkeypatch.setenv("DAVE_NOTION_PAGES", _json.dumps({"adapter-test": page}))
    router, client, _ = router_factory(tools=True)
    auth = {"X-API-Key": "test-only-api-key"}
    bodies = []
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock, _persistent_loop(client):
        fake.mount(mock)
        mock.get("http://ollama.test:11434/api/tags").mock(return_value=httpx.Response(
            200, json={"models": [{"name": "inventory-model:latest", "model": "inventory-model:latest"}]}))
        client.get("/nodes/node-test/models", headers=auth)
        chat = mock.post("http://ollama.test:11434/api/chat")
        responses = _model_turns(
            ("notion.page.read", {"page": "adapter-test"}),
            ("notion.page.append", {"page": "adapter-test", "blocks": [{"type": "paragraph", "text": "x"}]}),
            (None, "done"),
        )
        response_started, release_response, response_cancelled = Event(), Event(), Event()
        turns = iter(responses)

        async def after_post_response(request):
            response_started.set()
            try:
                while not release_response.is_set():
                    await asyncio.sleep(0.001)
            except asyncio.CancelledError:
                response_cancelled.set()
                raise
            return next(turns)

        chat.side_effect = after_post_response if delayed_response else responses
        try:
            created = client.post("/tools/agent/runs", headers=auth, json={
                "messages": [{"role": "user", "content": "go"}], "node_id": "node-test",
                "model": "inventory-model:latest"})
            assert created.status_code == 200
            run_id = created.json()["run_id"]
            if delayed_response:
                # Hold the scripted reply until POST returns: request teardown must not cancel it.
                assert response_started.wait(1), "The scripted model never started"
                assert not response_cancelled.is_set(), "Request teardown cancelled the scripted model"
                assert created.json()["status"] in {"created", "running"}
        finally:
            release_response.set()
        paused = _settled(client, run_id)
        pending = paused["snapshot"]["pending_call"]
        decided = client.post(f"/tools/agent/runs/{run_id}/decisions", headers=auth, json={
            **{key: pending[key] for key in ("call_id", "digest", "definition_fingerprint", "permission", "nonce")},
            "decision": "approve"})
        bodies += [paused, decided.json(),
                   client.get(f"/tools/agent/runs/{run_id}/events", headers=auth).json()]
        for route in ("/health", "/nodes", "/nodes/status", "/tools", "/monitoring/health", "/analytics/costs"):
            bodies.append(client.get(route, headers=auth).text)
    assert decided.json()["status"] == "completed"
    everything = _json.dumps(bodies) + "\n".join(record.getMessage() for record in caplog.records)
    assert TOKEN not in everything
    assert "Bearer" not in everything


def test_r4_the_manifest_never_captures_the_token(monkeypatch):
    import importlib.util
    import pathlib
    monkeypatch.setenv("DAVE_NOTION_TOKEN", TOKEN)
    monkeypatch.setenv("DAVE_ENABLE_NOTION_TOOLS", "true")
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "generate_capabilities_manifest.py"
    spec = importlib.util.spec_from_file_location("manifest_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rendered = module.render_json(module.build_manifest())
    assert TOKEN not in rendered
    assert "notion.page.read" in rendered


def test_r4_a_request_the_client_refuses_to_build_is_unsent_not_uncertain(monkeypatch):
    world = World()
    original = httpx.AsyncClient.stream

    def refuse_patch(self, method, url, **kwargs):
        if method == "PATCH":
            raise httpx.LocalProtocolError("Illegal header value")
        return original(self, method, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "stream", refuse_patch)
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message == "Nothing was written: the request could not be sent to Notion"
    monkeypatch.undo()
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"


# ---- Run 5: read-path limits -------------------------------------------------------------

import time as _time  # noqa: E402


def gets(world, suffix=""):
    return [r for r in world.fake.requests if r.method == "GET" and r.url.path.endswith(suffix)]


def test_r5_a_long_page_is_read_up_to_the_block_limit_without_listing_the_rest():
    world = World()
    for index in range(1000):
        world.fake.add_block(world.page, "paragraph", [rt(f"line {index}")])
    result = world.read()
    assert len(result["blocks"]) == notion.NOTION_READ_MAX_BLOCKS
    assert result["truncated"] is True
    assert [entry["text"] for entry in result["blocks"]][-1] == "line 299"
    assert len(gets(world, "/children")) == 3, "only the pages holding the first 300 blocks are needed"


def test_r5_blocks_whose_children_were_not_read_say_so():
    world = World()
    level0 = world.fake.add_block(world.page, "toggle", [rt("level 0")])
    level1 = world.fake.add_block(level0, "toggle", [rt("level 1")])
    level2 = world.fake.add_block(level1, "toggle", [rt("level 2")])
    world.fake.add_block(level2, "paragraph", [rt("level 3, beyond the depth limit")])
    synced = world.fake.add_block(world.page, "synced_block", body={"synced_from": None})
    world.fake.add_block(synced, "paragraph", [rt("inside a synced block")])
    meeting = world.fake.add_block(world.page, "meeting_notes", body={"title": [rt("Standup")]})
    world.fake.add_block(meeting, "paragraph", [rt("inside meeting notes")])
    entries = {entry["type"] + (entry.get("text") or ""): entry for entry in world.read()["blocks"]}
    assert entries["togglelevel 2"].get("children_not_read") is True
    assert entries["synced_block"].get("children_not_read") is True
    assert entries["meeting_notes"].get("children_not_read") is True
    assert "children_not_read" not in entries["togglelevel 0"]
    listed = {r.url.path.split("/")[-2] for r in gets(world, "/children")}
    assert synced not in listed and meeting not in listed and level2 not in listed


def test_r5_a_subtree_that_cannot_be_listed_does_not_sink_the_whole_read():
    world = World()
    world.fake.add_block(world.page, "paragraph", [rt("before")])
    broken = world.fake.add_block(world.page, "toggle", [rt("broken toggle")])
    world.fake.add_block(broken, "paragraph", [rt("hidden")])
    world.fake.add_block(world.page, "paragraph", [rt("after")])
    world.fake.fail("GET", rf"/blocks/{broken}/children", "reject", status=400, code="validation_error")
    result = world.read()
    assert [entry.get("text") for entry in result["blocks"]] == ["before", "broken toggle", "after"]
    assert result["blocks"][1].get("children_not_read") is True


def test_r5_has_more_without_a_cursor_is_reported_as_truncated():
    world = World()
    original = world.fake.handle

    def no_cursor(request):
        response = original(request)
        if request.url.path.endswith("/children"):
            body = _json.loads(response.content)
            body["has_more"], body["next_cursor"] = True, None
            return httpx.Response(200, json=body)
        return response

    world.fake.add_block(world.page, "paragraph", [rt("only page one")])
    world.fake.handle = no_cursor
    assert world.read()["truncated"] is True


def test_r5_a_cursor_that_loops_never_duplicates_blocks():
    world = World()
    looped = world.fake.add_block(world.page, "paragraph", [rt("again and again")])

    def loop(request):
        world.fake.requests.append(request)
        path = request.url.path.removeprefix("/v1")
        if path.startswith("/pages/"):
            return world.fake.route(request, path)
        return httpx.Response(200, json={"results": [world.fake.blocks[looped]], "has_more": True,
                                         "next_cursor": looped})

    world.fake.handle = loop
    result = world.read()
    assert [entry["ref"] for entry in result["blocks"]] == ["b1"]
    assert result["truncated"] is True


def test_r5_the_output_budget_keeps_document_order_and_trims_quickly(monkeypatch):
    world = World()
    for index in range(300):
        world.fake.add_block(world.page, "paragraph", [rt(f"{index:03d} " + "x" * 2000)] + [rt("y" * 1996)])
    measured = []
    real = notion._encoded
    monkeypatch.setattr(notion, "_encoded", lambda payload: measured.append(real(payload)) or measured[-1])
    result = world.read()
    encoded = len(_json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
    assert encoded <= notion.NOTION_OUTPUT_BUDGET_BYTES
    assert encoded > notion.NOTION_OUTPUT_BUDGET_BYTES - 5000, "the budget is used, not wasted"
    assert result["truncated"] is True
    assert [entry["text"][:3] for entry in result["blocks"]] == [f"{i:03d}" for i in range(len(result["blocks"]))]
    # Deterministic, not wall-clock: the work is proportional to the content, measured once
    # (about 1.2 MB here), not re-encoded after every removal (about 170 MB before).
    assert sum(measured) <= 2 * 300 * 4100, f"{sum(measured):,} bytes encoded"


def test_r5_clipped_text_is_flagged_and_still_editable_beyond_the_clip():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("a" * 4500 + " tail-marker")])
    entry = world.read()["blocks"][0]
    assert entry["clipped"] is True and len(entry["text"]) == notion.NOTION_DISPLAY_MAX_CHARS
    assert world.call(update_block, page="adapter-test", block=entry["ref"], old_text="tail-marker",
                      new_text="end")["outcome"] == "verified"
    assert world.fake.text_of(block).endswith(" end")


def test_r5_many_nested_lists_stop_at_the_request_budget_and_say_so():
    world = World()
    for index in range(40):
        toggle = world.fake.add_block(world.page, "toggle", [rt(f"toggle {index}")])
        world.fake.add_block(toggle, "paragraph", [rt(f"child {index}")])
    result = world.read()
    assert len(world.fake.requests) <= notion.NOTION_READ_MAX_REQUESTS
    assert result["truncated"] is True
    unread = [entry for entry in result["blocks"] if entry.get("children_not_read")]
    assert unread, "toggles whose children were skipped for budget must be marked"


def test_r5_tables_and_columns_read_in_order():
    world = World()
    table = world.fake.add_block(world.page, "table", body={"table_width": 2, "has_column_header": True})
    world.fake.add_block(table, "table_row", body={"cells": [[rt("Item")], [rt("Status")]]})
    world.fake.add_block(table, "table_row", body={"cells": [[rt("Adapter")], [rt("Done", bold=True)]]})
    columns = world.fake.add_block(world.page, "column_list", body={})
    left = world.fake.add_block(columns, "column", body={})
    world.fake.add_block(left, "paragraph", [rt("left column")])
    result = world.read()["blocks"]
    assert [(e["type"], e.get("text"), e.get("depth", 0)) for e in result] == [
        ("table", None, 0), ("table_row", "Item | Status", 1), ("table_row", "Adapter | Done", 1),
        ("column_list", None, 0), ("column", None, 1), ("paragraph", "left column", 2)]


# ---- Run 6: lifecycle and API integration ------------------------------------------

from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone  # noqa: E402

LIFECYCLE_AUTH = {"X-API-Key": "test-only-api-key"}
MODEL_NAME = "inventory-model:latest"
DECISION_KEYS = ("call_id", "digest", "definition_fingerprint", "permission", "nonce")


class Lifecycle:
    """A router with the Notion tools on, a scripted model, and a fake Notion behind respx."""

    def __init__(self, router_factory, monkeypatch, mock):
        self.fake = FakeNotion()
        self.page = self.fake.add_page("DaveLLM Adapter Test")
        monkeypatch.setenv("DAVE_ENABLE_NOTION_TOOLS", "true")
        monkeypatch.setenv("DAVE_NOTION_TOKEN", TOKEN)
        monkeypatch.setenv("DAVE_NOTION_PAGES", _json.dumps({"adapter-test": self.page}))
        self.router, self.client, _ = router_factory(tools=True)
        self.fake.mount(mock)
        mock.get("http://ollama.test:11434/api/tags").mock(return_value=httpx.Response(
            200, json={"models": [{"name": MODEL_NAME, "model": MODEL_NAME}]}))
        assert self.client.get("/nodes/node-test/models", headers=LIFECYCLE_AUTH).status_code == 200
        self.chat = mock.post("http://ollama.test:11434/api/chat")

    def start(self, *turns, content="go"):
        self.chat.side_effect = _model_turns(*turns)
        created = self.client.post("/tools/agent/runs", headers=LIFECYCLE_AUTH, json={
            "messages": [{"role": "user", "content": content}], "node_id": "node-test", "model": MODEL_NAME})
        assert created.status_code == 200, created.text
        return created.json()["run_id"]

    def settle(self, run_id):
        return _settled(self.client, run_id)

    def decide(self, run_id, pending, decision="approve", **overrides):
        return self.client.post(f"/tools/agent/runs/{run_id}/decisions", headers=LIFECYCLE_AUTH, json={
            **{key: pending[key] for key in DECISION_KEYS}, **overrides, "decision": decision})

    def tool_results(self, run):
        return [_json.loads(item["content"]) for item in run["snapshot"]["transcript"] if item["role"] == "tool"]


@pytest.fixture
def lifecycle(router_factory, monkeypatch):
    """Keeps one event loop for the whole test, as uvicorn does.

    A TestClient outside a ``with`` block starts a new loop per request, so a run task that
    outlives its POST is cancelled when that request's loop closes and the run is left
    "running". The persistent portal avoids the lifespan, whose summarizer would talk to the
    scripted model.
    """
    import anyio.from_thread
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock, \
            anyio.from_thread.start_blocking_portal(backend="asyncio") as portal:
        world = Lifecycle(router_factory, monkeypatch, mock)
        world.client.portal = portal
        try:
            yield world
        finally:
            world.client.portal = None


APPEND_CALL = ("notion.page.append", {"page": "adapter-test", "blocks": [{"type": "paragraph", "text": "from the run"}]})


def test_r6_a_tampered_decision_writes_nothing_and_the_real_one_writes_once(lifecycle):
    run_id = lifecycle.start(APPEND_CALL, (None, "done"))
    pending = lifecycle.settle(run_id)["snapshot"]["pending_call"]
    for field_name, value in (("digest", "0" * 64), ("nonce", "forged"), ("definition_fingerprint", "f" * 64),
                              ("permission", "read"), ("call_id", "call_other")):
        assert lifecycle.decide(run_id, pending, **{field_name: value}).status_code == 409, field_name
    assert lifecycle.fake.writes() == []
    decided = lifecycle.decide(run_id, pending)
    assert decided.json()["status"] == "completed"
    (write,) = lifecycle.fake.writes()
    assert _json.loads(write.content)["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "from the run"


def test_r6_an_expired_approval_writes_nothing(lifecycle, monkeypatch):
    run_id = lifecycle.start(APPEND_CALL, (None, "done"))
    pending = lifecycle.settle(run_id)["snapshot"]["pending_call"]

    class Later(_datetime):
        @classmethod
        def now(cls, tz=None):
            return _datetime.now(tz) + _timedelta(seconds=301)

    monkeypatch.setattr(lifecycle.router, "datetime", Later)
    response = lifecycle.decide(run_id, pending)
    assert response.status_code == 409 and response.json()["detail"] == "Pending approval expired"
    assert lifecycle.fake.writes() == []


def test_r6_an_approval_after_the_run_deadline_writes_nothing(lifecycle, monkeypatch):
    real = _timedelta
    monkeypatch.setattr(lifecycle.router, "timedelta", lambda **kwargs: real(seconds=1) if kwargs == {"minutes": 5} else real(**kwargs))
    run_id = lifecycle.start(APPEND_CALL, (None, "done"))
    pending = lifecycle.settle(run_id)["snapshot"]["pending_call"]
    _time.sleep(1.2)
    response = lifecycle.decide(run_id, pending)
    assert response.status_code in (200, 409)
    _time.sleep(0.3)
    assert lifecycle.fake.writes() == [], "no write may start once the run deadline has passed"


def test_r6_the_legacy_loop_cannot_pre_approve_a_notion_write(lifecycle):
    lifecycle.chat.side_effect = _model_turns(APPEND_CALL, (None, "done"))
    response = lifecycle.client.post("/tools/agent/run", headers=LIFECYCLE_AUTH, json={
        "messages": [{"role": "user", "content": "go"}], "node_id": "node-test", "model": MODEL_NAME,
        "approved_tools": ["notion.page.append"]})
    assert response.status_code == 200, response.text
    assert notion.NOT_IN_RUN in response.text
    assert lifecycle.fake.requests == []


def test_r6_an_edit_made_while_the_approval_waits_is_never_overwritten(lifecycle):
    block = lifecycle.fake.add_block(lifecycle.page, "paragraph", [rt("draft wording")])
    run_id = lifecycle.start(("notion.page.read", {"page": "adapter-test"}),
                             ("notion.block.update", {"page": "adapter-test", "block": "b1",
                                                      "old_text": "draft", "new_text": "final"}),
                             (None, "tried"))
    pending = lifecycle.settle(run_id)["snapshot"]["pending_call"]
    lifecycle.fake.set_text(block, [rt("draft wording, edited by Dave")])
    decided = lifecycle.decide(run_id, pending).json()
    assert decided["status"] == "completed"
    result = lifecycle.tool_results(decided)[-1]
    assert result["status"] == "error"
    assert result["error"] == "Nothing was written: block b1 changed after it was read; read the page again"
    assert lifecycle.fake.text_of(block) == "draft wording, edited by Dave"
    assert lifecycle.fake.writes() == []


def test_r6_a_model_that_edits_before_reading_recovers_within_the_run(lifecycle):
    block = lifecycle.fake.add_block(lifecycle.page, "to_do", [rt("Prove the adapter")], checked=False)
    run_id = lifecycle.start(
        ("notion.block.update", {"page": "adapter-test", "block": "b1", "checked": True,
                                 "block_text": "Prove the adapter"}),
        ("notion.page.read", {"page": "adapter-test"}),
        ("notion.block.update", {"page": "adapter-test", "block": "b1", "checked": True,
                                 "block_text": "Prove the adapter"}),
        (None, "checked"))
    # The doomed call is refused locally; only the recovered call asks for approval.
    paused = lifecycle.settle(run_id)
    assert paused["status"] == "approval_required"
    first, read = lifecycle.tool_results(paused)
    assert first["status"] == "error" and first["termination"] == "denied"
    assert first["error"].startswith("Unknown block ref b1")
    assert read["status"] == "success"
    assert lifecycle.fake.writes() == []
    decided = lifecycle.decide(run_id, paused["snapshot"]["pending_call"]).json()
    assert decided["status"] == "completed"
    assert lifecycle.fake.blocks[block]["to_do"]["checked"] is True
    assert len(lifecycle.fake.writes()) == 1


def test_r6_failed_and_unknown_outcomes_reach_the_model_as_errors_not_successes(lifecycle):
    lifecycle.fake.fail("PATCH", CHILDREN, "drop_before_apply")
    run_id = lifecycle.start(APPEND_CALL, APPEND_CALL, (None, "stopped"))
    pending = lifecycle.settle(run_id)["snapshot"]["pending_call"]
    decided = lifecycle.decide(run_id, pending).json()
    results = lifecycle.tool_results(decided)
    assert decided["status"] == "error_budget" and results[1]["termination"] == "denied"
    assert decided["snapshot"]["pending_call"] is None
    assert results[0]["status"] == "error" and results[0]["error"].startswith("Outcome unknown:")
    assert results[1]["status"] == "error" and "earlier append of these exact blocks" in results[1]["error"]
    assert len(lifecycle.fake.writes()) == 1
    events = lifecycle.client.get(f"/tools/agent/runs/{run_id}/events", headers=LIFECYCLE_AUTH).json()["events"]
    assert [e.get("status") for e in events if e["kind"] == "tool_result"][:2] == ["error", "error"]


@pytest.mark.asyncio
async def test_r6_cancelling_a_run_mid_write_lands_once_records_it_and_frees_the_page(router_factory, monkeypatch):
    fake = FakeNotion()
    page = fake.add_page("DaveLLM Adapter Test")
    monkeypatch.setenv("DAVE_ENABLE_NOTION_TOOLS", "true")
    monkeypatch.setenv("DAVE_NOTION_TOKEN", TOKEN)
    monkeypatch.setenv("DAVE_NOTION_PAGES", _json.dumps({"adapter-test": page}))
    router, _, _ = router_factory(tools=True)
    router.MODEL_INVENTORY["node-test"] = {MODEL_NAME}
    turns = iter([
        {"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function",
         "function": {"name": APPEND_CALL[0], "arguments": _json.dumps(APPEND_CALL[1])}}]}, "done": True},
        {"message": {"role": "assistant", "content": "done"}, "done": True},
    ])

    async def scripted(_messages, _schemas):
        return next(turns)

    router.HARNESS.invoke_model = scripted
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        fake.mount(mock)
        created = await router.create_agent_run(router.LifecycleRunRequest(
            messages=[{"role": "user", "content": "go"}], node_id="node-test", model=MODEL_NAME), user_id="default")
        run_id = created["run_id"]
        for _ in range(100):
            if router.HARNESS.snapshot(run_id).status == "approval_required":
                break
            await asyncio.sleep(0.01)
        pending = router.HARNESS.snapshot(run_id).snapshot.pending_call
        fake.delay("PATCH", CHILDREN, 0.4, apply_first=True)
        decision = asyncio.ensure_future(router.decide_agent_run(run_id, router.LifecycleDecisionRequest(
            call_id=pending.call_id, digest=pending.digest, definition_fingerprint=pending.definition_fingerprint,
            permission=pending.permission, nonce=pending.nonce, decision="approve"), user_id="default"))
        for _ in range(100):
            if fake.writes():
                break
            await asyncio.sleep(0.01)
        assert fake.writes(), "the approved append must have been dispatched before the cancel"
        cancelled = await router.cancel_agent_run(run_id, user_id="default")
        try:
            await decision
        except asyncio.CancelledError:
            pass
        await drain_writes()
    # The write was already with Notion, so the harness truthfully reports that it could not stop it.
    assert cancelled["status"] == "cancellation_failed"
    assert len(fake.writes()) == 1
    assert len(fake.page_texts(page)) == 1
    ledger = router.NOTION_LEDGERS.for_run(run_id)
    digest = notion._append_digest("adapter-test", notion._append_blocks_argument(APPEND_CALL[1]["blocks"]))
    assert ledger.append_outcome("adapter-test", digest) == "verified"
    # The run is terminal, so it can never call a tool again; no repeat is reachable.
    assert router.HARNESS.snapshot(run_id).status == "cancellation_failed"
    assert not router.NOTION_WRITE_GUARD.busy(page)


# ---- Run 7: approval UI ------------------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402

PREVIEW_SCRIPT = r"""
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
class El { constructor(t) { this.tagName = t.toUpperCase(); this.children = []; this.textContent = ""; this.className = ""; }
  appendChild(c) { this.children.push(c); return c; } append(...c) { this.children.push(...c); } }
const source = readFileSync(process.argv[1], "utf8");
const start = source.indexOf("function approvalPreview(pending) {");
const end = source.indexOf("\nfunction renderToolRun(run)", start);
const context = { document: { createElement: (t) => new El(t) } };
vm.createContext(context);
vm.runInContext(source.slice(start, end) + "\nglobalThis.approvalPreview = approvalPreview;", context);
const walk = (e) => [e.tagName, e.className, e.textContent, e.children.map(walk)];
const preview = context.approvalPreview(JSON.parse(readFileSync(0, "utf8")));
process.stdout.write(JSON.stringify(preview ? walk(preview) : null));
"""


def render_preview(pending):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    app_js = pathlib.Path(__file__).resolve().parents[1] / "static" / "app.js"
    done = subprocess.run([node, "-e", PREVIEW_SCRIPT, str(app_js)], input=_json.dumps(pending),
                          capture_output=True, text=True, timeout=30, check=True)
    return _json.loads(done.stdout)


import pathlib  # noqa: E402


@pytest.mark.parametrize("call, expected", [
    (APPEND_CALL, ["Append to Notion page adapter-test · 1 block at the end", "Paragraph: from the run"]),
    (("notion.block.update", {"page": "adapter-test", "block": "b1", "checked": True,
                              "block_text": "Prove the adapter draft"}),
     ["Edit Notion page adapter-test · block b1", "Mark this to-do as done:", "Prove the adapter draft"]),
    (("notion.block.update", {"page": "adapter-test", "block": "b1", "old_text": "draft", "new_text": "final"}),
     ["Edit Notion page adapter-test · block b1", "Before", "draft", "After", "final"]),
])
def test_r7_the_router_s_real_pending_call_renders_the_notion_preview(lifecycle, call, expected):
    lifecycle.fake.add_block(lifecycle.page, "to_do", [rt("Prove the adapter draft")], checked=False)
    turns = [("notion.page.read", {"page": "adapter-test"}), call, (None, "done")] if call[0] != APPEND_CALL[0] \
        else [call, (None, "done")]
    run_id = lifecycle.start(*turns)
    pending = lifecycle.settle(run_id)["snapshot"]["pending_call"]
    assert pending["tool_name"] == call[0]
    tree = render_preview(pending)
    assert tree is not None, f"the preview fell back to raw JSON for {call[0]}: {sorted(pending)}"

    def texts(node):
        tag, _cls, text, children = node
        return ([text] if text else []) + [t for child in children for t in texts(child)]

    assert [t for t in texts(tree) if t] == expected


def context_of(lifecycle, run_id, *, headers=LIFECYCLE_AUTH):
    return lifecycle.client.get(f"/tools/agent/runs/{run_id}/pending/notion-context", headers=headers)


def test_r7_the_card_gets_the_whole_block_before_and_after_from_the_run_ledger(lifecycle):
    lifecycle.fake.add_block(lifecycle.page, "paragraph", [rt("Status: "), rt("draft", bold=True), rt(" wording")])
    run_id = lifecycle.start(("notion.page.read", {"page": "adapter-test"}),
                             ("notion.block.update", {"page": "adapter-test", "block": "b1",
                                                      "old_text": "draft", "new_text": "final"}), (None, "done"))
    lifecycle.settle(run_id)
    response = context_of(lifecycle, run_id)
    assert response.status_code == 200, response.text
    assert response.json() == {"page": "adapter-test", "block": "b1", "type": "paragraph", "formatted": True,
                               "before": "Status: draft wording", "after": "Status: final wording"}


def test_r7_the_card_still_warns_when_preflight_falls_back_to_approval(lifecycle, monkeypatch):
    original = lifecycle.router.notion_pending_context
    unavailable = {"next": True}

    def preview(*args, **kwargs):
        if unavailable.pop("next", False):
            raise RuntimeError("preflight temporarily unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle.router, "notion_pending_context", preview)
    lifecycle.fake.add_block(lifecycle.page, "paragraph", [rt("plain "), rt("bold", bold=True)])
    run_id = lifecycle.start(("notion.page.read", {"page": "adapter-test"}),
                             ("notion.block.update", {"page": "adapter-test", "block": "b1",
                                                      "old_text": "plain bold", "new_text": "x"}), (None, "done"))
    lifecycle.settle(run_id)
    body = context_of(lifecycle, run_id).json()
    assert body["refused"].startswith("old_text in block b1 crosses a formatting change")
    unavailable["next"] = True
    other = lifecycle.start(("notion.block.update", {"page": "adapter-test", "block": "b7", "checked": True,
                                                    "block_text": "x"}), (None, "done"))
    lifecycle.settle(other)
    assert context_of(lifecycle, other).json()["refused"] == (
        "Unknown block ref b7; refs come from notion.page.read in this run")


def test_r7_the_context_route_is_scoped_to_the_run_owner_and_to_notion_calls(lifecycle):
    run_id = lifecycle.start(("notion.page.read", {"page": "adapter-test"}), (None, "done"))
    lifecycle.settle(run_id)
    assert context_of(lifecycle, run_id).status_code == 404  # no pending call
    assert context_of(lifecycle, "run_missing").status_code == 404
    assert lifecycle.client.get(f"/tools/agent/runs/{run_id}/pending/notion-context").status_code == 401


# ---- Run 8: concurrency and state --------------------------------------------------------

def second_run(world):
    return {"settings": world.settings, "ledger": notion.RunLedger(), "guard": world.guard}


def test_r8_two_runs_appending_to_one_page_take_turns_instead_of_failing():
    world = World()
    world.fake.delay("PATCH", CHILDREN, 0.2, apply_first=True)
    first = {"page": "adapter-test", "blocks": [{"type": "paragraph", "text": "from run A"}]}
    second = {"page": "adapter-test", "blocks": [{"type": "paragraph", "text": "from run B"}]}

    async def scenario():
        return await asyncio.gather(append_blocks(first, **kwargs(world)), append_blocks(second, **second_run(world)),
                                    return_exceptions=True)

    results = run_in_fake(world, scenario)
    assert [r["outcome"] if isinstance(r, dict) else str(r) for r in results] == ["verified", "verified"]
    assert [text for _, text in world.fake.page_texts(world.page)] == ["from run A", "from run B"]
    assert not world.guard.busy(world.page)


def test_r8_the_wait_for_a_busy_page_is_bounded_and_a_cancelled_waiter_takes_nothing(monkeypatch):
    monkeypatch.setattr(notion, "NOTION_PAGE_WAIT_SECONDS", 0.3, raising=False)
    world = World()
    assert world.guard.acquire(world.page)

    async def scenario():
        started = _time.perf_counter()
        with pytest.raises(NotionToolError) as caught:
            await append_blocks({"page": "adapter-test", "blocks": BLOCKS}, **kwargs(world))
        waited = _time.perf_counter() - started
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(append_blocks({"page": "adapter-test", "blocks": BLOCKS}, **kwargs(world)), 0.05)
        return str(caught.value), waited

    message, waited = run_in_fake(world, scenario)
    assert message == notion.PAGE_BUSY
    assert 0.25 <= waited < 1.0, f"waited {waited:.2f}s"
    assert world.guard.busy(world.page), "the original holder still owns the page"
    world.guard.release(world.page)
    assert world.fake.writes() == []


def test_r8_an_edit_based_on_another_run_s_stale_read_is_refused():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("shared draft")])
    other = notion.RunLedger()
    world.read()
    world.call(read_page, other, page="adapter-test")
    assert world.call(update_block, page="adapter-test", block="b1", old_text="draft", new_text="final")[
        "outcome"] == "verified"
    message = world.refuse(update_block, other, page="adapter-test", block="b1", old_text="draft", new_text="copy")
    assert message == "Nothing was written: block b1 changed after it was read; read the page again"
    assert world.fake.text_of(block) == "shared final"


def test_r8_ten_runs_writing_ten_pages_at_once_stay_separate():
    world = World()
    pages = {f"p{i}": world.fake.add_page(f"Page {i}") for i in range(10)}
    world.settings = notion.NotionSettings.from_values(TOKEN, _json.dumps(pages))
    for name in pages.values():
        world.fake.delay("PATCH", rf"/blocks/{name}/children", 0.05, apply_first=True)

    async def scenario():
        return await asyncio.gather(*(
            append_blocks({"page": name, "blocks": [{"type": "paragraph", "text": f"only for {name}"}]},
                          **second_run(world)) for name in pages))

    results = run_in_fake(world, scenario)
    assert [r["outcome"] for r in results] == ["verified"] * 10
    for name, page_id in pages.items():
        assert world.fake.page_texts(page_id) == [("paragraph", f"only for {name}")]


@pytest.mark.parametrize("failure", [RuntimeError("bug"), asyncio.CancelledError()])
def test_r8_the_page_guard_is_released_on_every_failure_inside_the_write(monkeypatch, failure):
    world = World()

    async def broken(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(notion, "_append_attempt", broken)

    async def scenario():
        try:
            await append_blocks({"page": "adapter-test", "blocks": BLOCKS}, **kwargs(world))
        except (NotionToolError, asyncio.CancelledError):
            pass
        await drain_writes()

    run_in_fake(world, scenario)
    assert not world.guard.busy(world.page)


def test_r8_dropping_a_run_s_ledger_mid_write_is_harmless():
    world = World()
    ledgers = notion.NotionLedgers()
    ledger = ledgers.for_run("run_a")
    world.fake.delay("PATCH", CHILDREN, 0.2, apply_first=True)

    async def scenario():
        task = asyncio.ensure_future(append_blocks({"page": "adapter-test", "blocks": BLOCKS},
                                                   settings=world.settings, ledger=ledger, guard=world.guard))
        await asyncio.sleep(0.05)
        ledgers.drop("run_a")
        return await task

    assert run_in_fake(world, scenario)["outcome"] == "verified"
    assert len(ledgers) == 0 and not world.guard.busy(world.page)


def test_r8_a_run_s_ledger_has_a_memory_bound():
    ledger = notion.RunLedger()
    big = [rt("x" * 2000, bold=i % 2 == 0) for i in range(100)]  # Notion's own per-block maximum
    stored = 0
    with pytest.raises(NotionToolError) as caught:
        for index in range(notion.NOTION_RUN_MAX_REFS):
            block = {"object": "block", "id": f"{index:08x}-0000-4000-8000-000000000000", "type": "paragraph",
                     "paragraph": {"rich_text": big}}
            ledger.remember("adapter-test", "0" * 8 + "-0000-4000-8000-" + "0" * 12, block)
            stored += len(_json.dumps(big))
    assert str(caught.value) == notion.LEDGER_FULL
    assert stored <= notion.NOTION_RUN_MAX_SNAPSHOT_BYTES + len(_json.dumps(big))


# ---- Run 9: outcome branches, documentation, contracts -----------------------------------

import ast  # noqa: E402
import re as _re  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_r9_an_append_to_a_page_too_long_to_check_is_refused_before_sending():
    world = World()
    for index in range(notion.NOTION_LIST_MAX_PAGES * notion.NOTION_PAGE_SIZE + 1):
        world.fake.add_block(world.page, "paragraph", [rt(str(index))])
    assert world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS) == (
        "Nothing was written: the page has more top-level blocks than this tool can check after an append")
    assert world.fake.writes() == []


def test_r9_an_accepted_append_that_reads_back_differently_is_unknown():
    world = World()
    original = world.fake.response_rich_text
    world.fake.response_rich_text = lambda items: original(items) and [{**original(items)[0],
        "text": {"content": "normalized by Notion", "link": None}, "plain_text": "normalized by Notion"}]
    message = world.refuse(append_blocks, page="adapter-test", blocks=[{"type": "paragraph", "text": "as sent"}])
    assert message.startswith("Outcome unknown: Notion accepted the append, but the page does not show exactly those blocks")


def test_r9_a_lost_append_whose_anchor_block_vanished_is_unknown():
    world = World()
    anchor = world.fake.add_block(world.page, "paragraph", [rt("last block before the append")])
    original = world.fake.handle

    def delete_anchor_then_lose(request):
        if request.method == "PATCH":
            world.fake.blocks[anchor]["in_trash"] = True
            original(request)
            raise httpx.ReadTimeout("lost", request=request)
        return original(request)

    world.fake.handle = delete_anchor_then_lose
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message.startswith("Outcome unknown: the connection to Notion failed during the append and the page changed meanwhile")


@pytest.mark.parametrize("scenario, expected", [
    ("lost_then_unreadable", "Outcome unknown: the connection to Notion failed during the edit and the block could not be checked"),
    ("accepted_but_different", "Outcome unknown: Notion accepted the edit, but block b1 now reads differently"),
    ("lost_and_changed", "Outcome unknown: the connection to Notion failed during the edit and block b1 changed"),
])
def test_r9_every_uncertain_edit_ending_is_reported_as_unknown(scenario, expected):
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("first draft")])
    world.read()
    original = world.fake.handle

    def misbehave(request):
        if request.method == "PATCH":
            if scenario == "accepted_but_different":
                response = original(request)
                world.fake.set_text(block, [rt("someone else's text")])
                return response
            if scenario == "lost_and_changed":
                world.fake.requests.append(request)
                world.fake.set_text(block, [rt("someone else's text")])
                raise httpx.ReadTimeout("lost", request=request)
            world.fake.requests.append(request)
            world.fake.fail("GET", rf"/blocks/{block}", "connect_error")
            raise httpx.ReadTimeout("lost", request=request)
        return original(request)

    world.fake.handle = misbehave
    message = world.refuse(update_block, page="adapter-test", block="b1", old_text="first", new_text="second")
    assert message.startswith(expected), message


def test_r9_a_refused_page_check_and_odd_parents_are_failed_writes():
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("text")])
    world.read()
    world.fake.fail("GET", r"/pages/.*", "reject", status=403, code="restricted_resource")
    assert world.refuse(update_block, page="adapter-test", block="b1", old_text="text", new_text="x") == (
        "Nothing was written: Notion refused to read page 'adapter-test' before writing (restricted_resource): "
        "share the page with DaveLLM's Notion connection")
    world.fake.blocks[block]["parent"] = {"type": "database_id", "database_id": world.page}
    assert world.refuse(update_block, page="adapter-test", block="b1", old_text="text", new_text="x") == (
        "Nothing was written: block b1 is no longer on page 'adapter-test'")
    holder = world.fake.add_block(world.page, "toggle", [rt("holder")])
    world.fake.blocks[block]["parent"] = {"type": "block_id", "block_id": holder}
    world.fake.blocks[holder]["in_trash"] = True
    assert world.refuse(update_block, page="adapter-test", block="b1", old_text="text", new_text="x") == (
        "Nothing was written: block b1 is no longer on page 'adapter-test'")
    assert world.fake.writes() == []


def test_r9_an_edit_that_would_need_more_than_100_runs_is_refused_before_sending():
    world = World()
    runs = [rt(f"r{i} ", bold=i % 2 == 0) for i in range(99)] + [rt("x" * 1999)]
    block = world.fake.add_block(world.page, "paragraph", runs)
    world.read()
    message = world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text="x" * 1999,
                           new_text="y" * 4100)
    assert message == f"Block {world.ref(block)} would need more than 100 rich text runs"
    assert world.fake.writes() == []


def test_r9_an_exception_after_the_request_was_sent_is_unknown_not_failed(monkeypatch):
    world = World()

    def broken(*_args):
        raise RuntimeError("bug after the write")

    monkeypatch.setattr(notion, "_verified_append", broken)
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message.startswith("Outcome unknown: the append did not finish")
    assert len(world.fake.writes()) == 1


@pytest.mark.parametrize("item, what", [
    ({"type": "equation", "equation": {}, "annotations": {}, "plain_text": ""}, "an equation"),
    ({"type": "mention", "mention": {"type": "date", "date": {}}, "annotations": {}, "plain_text": "?"}, "a date mention"),
    ({"type": "mention", "mention": {"type": "page", "page": {"id": "nope"}}, "annotations": {}, "plain_text": "?"}, "a page mention"),
    ({"type": "template", "annotations": {}, "plain_text": "?"}, "rich text"),
])
def test_r9_malformed_rich_text_refuses_text_edits(item, what):
    world = World()
    block = world.fake.add_block(world.page, "paragraph", [rt("editable "), item])
    world.read()
    assert world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text="editable",
                        new_text="x") == f"Block {world.ref(block)} contains {what} that cannot be written back, so its text cannot be edited"


@pytest.mark.parametrize("arguments, message", [
    ({}, "Give old_text and new_text, or checked, or both"),
    ({"old_text": "a"}, "old_text and new_text must be given together"),
    ({"old_text": "", "new_text": "b"}, "old_text and new_text must be given together"),
    ({"old_text": "same", "new_text": "same"}, "old_text and new_text are the same"),
    ({"checked": "yes", "block_text": "t"}, "checked must be true or false"),
])
def test_r9_edit_arguments_are_checked_before_anything_is_sent(arguments, message):
    world = World()
    world.fake.add_block(world.page, "to_do", [rt("same t")], checked=False)
    world.read()
    assert world.refuse(update_block, page="adapter-test", block="b1", **arguments) == message
    assert world.fake.writes() == []


@pytest.mark.parametrize("blocks, message", [
    ([7], "Block 1 must be an object"),
    ([{"type": "to_do", "text": "t", "checked": "yes"}], "Block 1 checked must be true or false"),
])
def test_r9_append_arguments_that_bypass_the_schema_are_still_refused(blocks, message):
    world = World()
    assert world.refuse(append_blocks, page="adapter-test", blocks=blocks) == message


def test_r9_the_ledger_registry_evicts_the_least_recently_used_run():
    ledgers = notion.NotionLedgers(max_runs=2)
    a, b = ledgers.for_run("a"), ledgers.for_run("b")
    assert ledgers.for_run("a") is a
    ledgers.for_run("c")
    assert len(ledgers) == 2 and ledgers.for_run("a") is a and ledgers.for_run("b") is not b


def _message_templates():
    """Every message the adapter can raise or report, from the source, cut at the first placeholder."""
    tree = ast.parse((REPO_ROOT / "davellm_notion.py").read_text())
    constants = {node.targets[0].id: node.value for node in tree.body
                 if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)}
    found = set()

    def text_of(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return text_of(node.func.value)
        if isinstance(node, ast.Name) and node.id in constants:
            return text_of(constants[node.id])
        if isinstance(node, ast.BinOp):
            left, right = text_of(node.left), text_of(node.right)
            return (left or "") + (right or "") if left or right else None
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "NotionToolError" and node.args:
                found.add(text_of(node.args[0]))
            if node.func.id == "_Outcome" and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value in {"failed", "unknown"}:
                found.add(("Nothing was written: " if node.args[0].value == "failed" else "Outcome unknown: ")
                          + (text_of(node.args[1]) or ""))
    templates = set()
    for text in found:
        if text:
            head = _re.split(r"\{", text, maxsplit=1)[0].strip(" '(")
            if len(head) >= 12:
                templates.add(head)
    return templates


def test_r9_every_adapter_message_is_documented():
    docs = (REPO_ROOT / "docs" / "DAVELLM_TOOLS.md").read_text()
    section = docs[docs.index("## Notion tools"):docs.index("## Paths")]
    undocumented = sorted(t for t in _message_templates() if t not in section)
    assert undocumented == [], "Notion messages missing from docs/DAVELLM_TOOLS.md:\n" + "\n".join(undocumented)


def test_r9_the_required_checks_compile_the_same_modules_as_ci():
    def modules(text):
        line = next(line for line in text.splitlines() if "py_compile" in line)
        return {word for word in line.split() if word.endswith(".py")}

    claude = modules((REPO_ROOT / "CLAUDE.md").read_text())
    ci = modules((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text())
    tracked = {path.name for path in REPO_ROOT.glob("davellm_*.py")}
    assert tracked <= claude and tracked <= ci, {"missing from CLAUDE.md": tracked - claude, "missing from CI": tracked - ci}
    assert claude == ci


# ---- Run 10: acceptance sequence and Notion's own contract ---------------------------------

def test_r10_the_live_acceptance_sequence_end_to_end(lifecycle):
    """The proposal's live test: read, approved append, verified result, targeted edit with formatting
    preserved, rejected edit with no change, out-of-scope page refused, then a re-read that matches."""
    fake, page = lifecycle.fake, lifecycle.page
    fake.add_block(page, "heading_2", [rt("DaveLLM Adapter Test")])
    status = fake.add_block(page, "paragraph", [rt("Status: "), rt("draft", bold=True), rt(" — see "),
                                                rt("spec", link="https://example.com/spec", italic=True)])
    todo = fake.add_block(page, "to_do", [rt("Prove the adapter")], checked=False)
    keep = fake.add_block(page, "callout", [rt("Do not touch this section", bold=True)])
    untouched = _json.dumps(fake.blocks[keep], sort_keys=True)

    # 1-3: read, approved append, verified result.
    run_id = lifecycle.start(("notion.page.read", {"page": "adapter-test"}),
                             ("notion.page.append", {"page": "adapter-test", "blocks": [
                                 {"type": "heading_3", "text": "DaveLLM test 2026-10-01"},
                                 {"type": "paragraph", "text": "Appended by the acceptance run."}]}),
                             ("notion.block.update", {"page": "adapter-test", "block": "b2",
                                                      "old_text": "draft", "new_text": "verified"}),
                             ("notion.block.update", {"page": "adapter-test", "block": "b3", "checked": True,
                                                      "block_text": "Prove the adapter"}),
                             ("notion.page.read", {"page": "elsewhere"}),
                             ("notion.page.read", {"page": "adapter-test"}),
                             (None, "Acceptance run complete."))
    step = lifecycle.settle(run_id)
    assert step["snapshot"]["pending_call"]["tool_name"] == "notion.page.append"
    step = lifecycle.decide(run_id, step["snapshot"]["pending_call"]).json()
    appended = _json.loads(lifecycle.tool_results(step)[-1]["result"])
    assert appended == {"outcome": "verified", "page": "adapter-test", "blocks_added": 2, "refs": ["b5", "b6"]}

    # 4: targeted edit, formatting preserved, with the card's trusted context.
    context = context_of(lifecycle, run_id).json()
    assert context["before"] == "Status: draft — see spec" and context["after"] == "Status: verified — see spec"
    step = lifecycle.decide(run_id, step["snapshot"]["pending_call"]).json()
    saved = fake.blocks[status]["paragraph"]["rich_text"]
    assert [(r["plain_text"], r["annotations"]["bold"], r["annotations"]["italic"], (r["text"]["link"] or {}).get("url"))
            for r in saved] == [("Status: ", False, False, None), ("verified", True, False, None),
                                (" — see ", False, False, None), ("spec", False, True, "https://example.com/spec")]

    # 5: rejected edit, no change.
    assert step["snapshot"]["pending_call"]["tool_name"] == "notion.block.update"
    rejected = lifecycle.decide(run_id, step["snapshot"]["pending_call"], decision="reject").json()
    assert rejected["status"] == "approval_rejected"
    assert fake.blocks[todo]["to_do"]["checked"] is False

    # 6: out-of-scope page refused (a new run continues the script), 7: re-read matches.
    run_two = lifecycle.start(("notion.page.read", {"page": "elsewhere"}),
                              ("notion.page.read", {"page": "adapter-test"}), (None, "done"))
    finished = lifecycle.settle(run_two)
    assert finished["status"] == "completed", finished["status"]
    refused, reread = lifecycle.tool_results(finished)
    assert refused["status"] == "error" and refused["error"] == (
        "Unknown Notion page 'elsewhere'. Configured pages: adapter-test")
    blocks = _json.loads(reread["result"])["blocks"]
    assert [(b["type"], b.get("text"), b.get("checked")) for b in blocks] == [
        ("heading_2", "DaveLLM Adapter Test", None), ("paragraph", "Status: verified — see spec", None),
        ("to_do", "Prove the adapter", False), ("callout", "Do not touch this section", None),
        ("heading_3", "DaveLLM test 2026-10-01", None), ("paragraph", "Appended by the acceptance run.", None)]
    assert _json.dumps(fake.blocks[keep], sort_keys=True) == untouched
    assert len(fake.writes()) == 2


def _overloaded(world, method, pattern, *, retry_after=None):
    original = world.fake.handle
    state = {"left": 1}

    def overloaded(request):
        if request.method == method and _re.fullmatch(pattern, request.url.path.removeprefix("/v1")) and state["left"]:
            state["left"] -= 1
            world.fake.requests.append(request)
            headers = {"Retry-After": retry_after} if retry_after is not None else {}
            return httpx.Response(529, json={"object": "error", "status": 529, "code": "service_overload",
                                             "message": "busy"}, headers=headers)
        return original(request)

    world.fake.handle = overloaded


def test_r10_a_write_refused_as_service_overload_was_not_carried_out():
    world = World()
    _overloaded(world, "PATCH", CHILDREN, retry_after="30")
    assert world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS) == (
        "Nothing was written: Notion refused the append (service_overload)")
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"


def test_r10_a_short_service_overload_is_retried_once_like_a_rate_limit():
    world = World()
    _overloaded(world, "PATCH", CHILDREN, retry_after="0")
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"
    assert len(world.fake.writes()) == 2 and len(world.fake.page_texts(world.page)) == 3


@pytest.mark.parametrize("status, code", [(500, "internal_server_error"), (503, "service_unavailable")])
def test_r10_a_read_survives_one_transient_server_error(status, code, monkeypatch):
    monkeypatch.setattr(notion, "NOTION_GET_RETRY_SECONDS", 0.0, raising=False)
    world = World()
    world.fake.add_block(world.page, "paragraph", [rt("still readable")])
    world.fake.fail("GET", CHILDREN, "reject", status=status, code=code)
    assert world.read()["blocks"][0]["text"] == "still readable"


def test_r10_a_write_answered_with_a_server_error_is_never_retried():
    world = World()
    world.fake.fail("PATCH", CHILDREN, "server_error_after_apply")
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"
    assert len(world.fake.writes()) == 1


def test_r10_listing_shapes_from_notion_s_own_types_are_handled():
    world = World()
    partial = new_id()
    tab = world.fake.add_block(world.page, "tab", body={})
    world.fake.add_block(tab, "paragraph", [rt("inside a tab")])
    legacy = world.fake.add_block(world.page, "transcription", body={"title": [rt("old meeting")]})
    world.fake.add_block(legacy, "paragraph", [rt("never listed")])
    original = world.fake.handle

    def with_partial(request):
        response = original(request)
        if request.method == "GET" and request.url.path.endswith(f"{world.page}/children"):
            body = _json.loads(response.content)
            body["results"].insert(0, {"object": "block", "id": partial})  # PartialBlockObjectResponse
            return httpx.Response(200, json=body)
        return response

    world.fake.handle = with_partial
    entries = world.read()["blocks"]
    assert [(e["type"], e.get("text"), e.get("depth", 0), e.get("children_not_read", False)) for e in entries] == [
        ("unsupported", None, 0, False), ("tab", None, 0, False), ("paragraph", "inside a tab", 1, False),
        ("transcription", None, 0, True)]
    assert not any(r.url.path.endswith(f"{legacy}/children") for r in world.fake.requests)


# ---- Notion approval preflight ------------------------------------------------------------

@pytest.mark.parametrize("tool", ["notion.page.append", "notion.block.update"])
@pytest.mark.parametrize("problem", ["missing_token", "invalid_token", "missing_pages", "invalid_pages", "unknown_page"])
def test_preflight_configuration_refuses_without_approval_or_handler(lifecycle, monkeypatch, tool, problem):
    router = lifecycle.router
    pages = _json.dumps({"adapter-test": lifecycle.page})
    settings = {
        "missing_token": (None, pages), "invalid_token": ("invalid secret", pages),
        "missing_pages": (TOKEN, None), "invalid_pages": (TOKEN, "[broken"),
        "unknown_page": (TOKEN, pages),
    }
    monkeypatch.setattr(router, "NOTION_SETTINGS", notion.NotionSettings.from_values(*settings[problem]))
    arguments = {"page": "elsewhere" if problem == "unknown_page" else "adapter-test"}
    arguments.update({"blocks": BLOCKS} if tool.endswith("append") else {"block": "b1", "checked": True, "block_text": "x"})
    entered = []

    async def forbidden(*_args, **_kwargs):
        entered.append(tool)
        raise AssertionError("invalid call entered handler")

    monkeypatch.setattr(router, "notion_append_blocks" if tool.endswith("append") else "notion_update_block", forbidden)
    expected = notion.pending_context(tool, arguments, settings=router.NOTION_SETTINGS, ledger=notion.RunLedger())["refused"]
    run_id = lifecycle.start((tool, arguments), (None, "done"))
    finished = lifecycle.settle(run_id)
    assert finished["status"] == "completed"
    assert finished["snapshot"]["pending_call"] is None
    result, = lifecycle.tool_results(finished)
    assert (result["status"], result["termination"], result["error"]) == ("error", "denied", expected)
    assert entered == [] and lifecycle.fake.requests == []
    assert context_of(lifecycle, run_id).status_code == 404


@pytest.mark.parametrize("problem", ["unknown_ref", "cross_page", "missing_text", "same_text", "formatting", "not_todo", "block_text"])
def test_preflight_invalid_edit_uses_run_snapshot_without_handler(lifecycle, monkeypatch, problem):
    router, fake = lifecycle.router, lifecycle.fake
    other = fake.add_page("Other")
    fake.add_block(other, "paragraph", [rt("other")])
    fake.add_block(lifecycle.page, "paragraph", [rt("plain "), rt("bold", bold=True)])
    router.NOTION_SETTINGS = notion.NotionSettings.from_values(TOKEN, _json.dumps({"adapter-test": lifecycle.page, "other": other}))
    arguments = {"page": "adapter-test", "block": "b1", "old_text": "plain", "new_text": "new"}
    changes = {
        "unknown_ref": {"block": "b999"}, "cross_page": {"page": "other"},
        "missing_text": {"old_text": "missing"}, "same_text": {"new_text": "plain"},
        "formatting": {"old_text": "plain bold"}, "not_todo": {"checked": True},
        "block_text": {"block_text": "wrong snapshot"},
    }
    arguments.update(changes[problem])
    entered = []

    async def forbidden(*_args, **_kwargs):
        entered.append(True)
        raise AssertionError("invalid edit entered handler")

    monkeypatch.setattr(router, "notion_update_block", forbidden)
    requests_at_preflight = []
    original = router.notion_pending_context

    def preview(*args, **kwargs):
        before = len(fake.requests)
        result = original(*args, **kwargs)
        requests_at_preflight.append((before, len(fake.requests), result["refused"]))
        return result

    monkeypatch.setattr(router, "notion_pending_context", preview)
    run_id = lifecycle.start(("notion.page.read", {"page": "adapter-test"}),
                             ("notion.block.update", arguments), (None, "done"))
    finished = lifecycle.settle(run_id)
    assert finished["status"] == "completed" and finished["snapshot"]["pending_call"] is None
    result = lifecycle.tool_results(finished)[-1]
    before, after, expected = requests_at_preflight[0]
    assert before == after == len(fake.requests)
    assert (result["termination"], result["error"]) == ("denied", expected)
    assert entered == [] and fake.writes() == []


@pytest.mark.parametrize("outcome", ["in_flight", "unknown", "verified"])
def test_preflight_duplicate_append_preserves_outcome_without_handler(lifecycle, monkeypatch, outcome):
    router = lifecycle.router
    get_ledger = router.NOTION_LEDGERS.for_run
    digest = notion._append_digest("adapter-test", notion._append_blocks_argument(APPEND_CALL[1]["blocks"]))
    seeded = {}

    def ledger_for_run(run_id):
        ledger = get_ledger(run_id)
        if run_id not in seeded:
            ledger.set_append_outcome("adapter-test", digest, outcome)
            seeded[run_id] = ledger
        return ledger

    entered = []

    async def forbidden(*_args, **_kwargs):
        entered.append(True)
        raise AssertionError("duplicate append entered handler")

    monkeypatch.setattr(router.NOTION_LEDGERS, "for_run", ledger_for_run)
    monkeypatch.setattr(router, "notion_append_blocks", forbidden)
    run_id = lifecycle.start(APPEND_CALL, (None, "done"))
    finished = lifecycle.settle(run_id)
    ledger = seeded[run_id]
    assert finished["status"] == "completed" and finished["snapshot"]["pending_call"] is None
    result, = lifecycle.tool_results(finished)
    assert (result["termination"], result["error"]) == ("denied", ledger.append_refusal("adapter-test", digest))
    assert ledger.append_outcome("adapter-test", digest) == outcome
    assert not ledger._appends[("adapter-test", digest)].delivered
    assert entered == [] and lifecycle.fake.requests == []


@pytest.mark.parametrize("tool", ["notion.page.append", "notion.block.update"])
def test_preflight_failure_still_requires_exact_approval_and_rejection_writes_nothing(lifecycle, monkeypatch, tool):
    fake = lifecycle.fake
    fake.add_block(lifecycle.page, "paragraph", [rt("draft", bold=True)])

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("preview unavailable")

    monkeypatch.setattr(lifecycle.router, "notion_pending_context", unavailable)
    call = APPEND_CALL if tool.endswith("append") else (tool, {"page": "adapter-test", "block": "b1", "old_text": "draft", "new_text": "final"})
    run_id = lifecycle.start(("notion.page.read", {"page": "adapter-test"}), call, (None, "done"))
    paused = lifecycle.settle(run_id)
    assert paused["status"] == "approval_required" and fake.writes() == []
    pending = paused["snapshot"]["pending_call"]
    assert pending["tool_name"] == tool
    assert all(pending[key] for key in DECISION_KEYS)
    rejected = lifecycle.decide(run_id, pending, decision="reject")
    assert rejected.json()["status"] == "approval_rejected"
    assert fake.writes() == []
