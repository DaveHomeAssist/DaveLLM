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
