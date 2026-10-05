"""Notion adapter: scope, formatting-preserving edits, and verified/failed/unknown write outcomes.

Every test runs against tests/fake_notion.py through respx; no request reaches Notion.
"""

import asyncio
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import anyio.from_thread
import httpx
import pytest
import respx

import davellm_notion as notion
from conftest import TEST_API_KEY, TEST_NODE_URL
from davellm_notion import (
    NotionSettings, NotionToolError, PageWriteGuard, RunLedger, append_blocks, notion_id, parse_pages,
    read_page, update_block,
)
from fake_notion import (
    FakeNotion, date_mention, equation, link_preview_mention, new_id, page_mention, rt, user_mention,
)

TOKEN = "test-notion-token-not-a-real-secret"
AUTH = {"X-API-Key": TEST_API_KEY}
MODEL = "inventory-model:latest"


class World:
    """A fake workspace with one configured page (and optionally a second), a run ledger, and a guard."""

    def __init__(self, *, second_page=False):
        self.fake = FakeNotion()
        self.page = self.fake.add_page("DaveLLM Adapter Test")
        pages = {"adapter-test": self.page}
        if second_page:
            self.other = self.fake.add_page("Other Page")
            pages["other"] = self.other
        self.settings = NotionSettings.from_values(TOKEN, json.dumps(pages))
        self.ledger = RunLedger()
        self.guard = PageWriteGuard()

    def call(self, function, ledger=None, **arguments):
        kwargs = {"settings": self.settings, "ledger": self.ledger if ledger is None else ledger}
        if function is not read_page:
            kwargs["guard"] = self.guard
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            self.fake.mount(mock)
            return asyncio.run(function(arguments, **kwargs))

    def refuse(self, function, ledger=None, **arguments):
        with pytest.raises(NotionToolError) as caught:
            self.call(function, ledger, **arguments)
        message = str(caught.value)
        assert TOKEN not in message and "private detail" not in message
        return message

    def read(self, page="adapter-test"):
        return self.call(read_page, page=page)

    def ref(self, block_id):
        return self.ledger._refs_by_block[block_id]


@pytest.fixture
def world():
    return World()


# ---- configuration ------------------------------------------------------

def test_notion_ids_come_from_bare_dashed_or_notion_urls_only():
    bare = "0123456789abcdef0123456789abcdef"
    dashed = "01234567-89ab-cdef-0123-456789abcdef"
    assert notion_id(bare) == dashed
    assert notion_id(dashed.upper()) == dashed
    assert notion_id(f"https://www.notion.so/Team/Adapter-Test-{bare}?pvs=4") == dashed
    assert notion_id(f"https://dave.notion.site/{bare}") == dashed
    for bad in (None, 7, "", "not-an-id", f"http://www.notion.so/{bare}", f"https://evil.example/{bare}",
                f"https://notion.so.evil.example/{bare}", bare[:-1], bare + "0"):
        assert notion_id(bad) is None, bad


def test_page_list_must_be_a_small_object_of_names_to_distinct_ids():
    page = "0123456789abcdef0123456789abcdef"
    other = "fedcba9876543210fedcba9876543210"
    assert parse_pages(json.dumps({"adapter-test": page})) == {"adapter-test": notion_id(page)}
    for bad in ("", "[]", "{}", "not json", json.dumps({"Adapter": page}), json.dumps({"a b": page}),
                json.dumps({"x": "nope"}), json.dumps({"a": page, "b": page}),
                json.dumps({f"p{i}": page[:-2] + f"{i:02d}" for i in range(21)})):
        assert parse_pages(bad) is None, bad
    assert parse_pages(json.dumps({"a": page, "b": other})) == {"a": notion_id(page), "b": notion_id(other)}


def test_settings_refuse_clearly_and_never_show_the_token():
    page = json.dumps({"adapter-test": "0123456789abcdef0123456789abcdef"})
    assert TOKEN not in repr(NotionSettings.from_values(TOKEN, page))
    with pytest.raises(NotionToolError, match="DAVE_NOTION_TOKEN is unset"):
        NotionSettings.from_values(None, page).page("adapter-test")
    with pytest.raises(NotionToolError, match="DAVE_NOTION_PAGES is unset"):
        NotionSettings.from_values(TOKEN, None).page("adapter-test")
    with pytest.raises(NotionToolError, match="not a JSON object"):
        NotionSettings.from_values(TOKEN, "{bad").page("adapter-test")
    with pytest.raises(NotionToolError, match=r"Unknown Notion page 'project'\. Configured pages: adapter-test"):
        NotionSettings.from_values(TOKEN, page).page("project")
    with pytest.raises(NotionToolError) as caught:
        NotionSettings.from_values(TOKEN, page).page("<script>")
    assert "<script>" not in str(caught.value)


# ---- notion.page.read ---------------------------------------------------

def test_read_returns_title_blocks_refs_depth_and_formatting_flags(world):
    fake, page = world.fake, world.page
    heading = fake.add_block(page, "heading_2", [rt("Status")])
    para = fake.add_block(page, "paragraph", [rt("Plain and "), rt("bold", bold=True)])
    todo = fake.add_block(page, "to_do", [rt("Ship v1")], checked=True)
    toggle = fake.add_block(page, "toggle", [rt("Details")])
    nested = fake.add_block(toggle, "bulleted_list_item", [rt("Nested item")])
    preview = fake.add_block(page, "paragraph", [link_preview_mention("https://example.com")])
    child = fake.add_block(page, "child_page", body={"title": "Sub page"})
    fake.add_block(child, "paragraph", [rt("never read")])
    result = world.read()
    assert result["page"] == "adapter-test"
    assert result["title"] == "DaveLLM Adapter Test"
    assert result["truncated"] is False
    assert result["blocks"] == [
        {"ref": "b1", "type": "heading_2", "text": "Status"},
        {"ref": "b2", "type": "paragraph", "text": "Plain and bold", "formatted": True},
        {"ref": "b3", "type": "to_do", "checked": True, "text": "Ship v1"},
        {"ref": "b4", "type": "toggle", "text": "Details"},
        {"ref": "b5", "type": "bulleted_list_item", "depth": 1, "text": "Nested item"},
        {"ref": "b6", "type": "paragraph", "text": "https://example.com", "formatted": True, "text_editable": False},
        {"ref": "b7", "type": "child_page", "children_not_read": True, "text": "Sub page"},
    ]
    assert [world.ref(b) for b in (heading, para, todo, toggle, nested, preview, child)] == [
        "b1", "b2", "b3", "b4", "b5", "b6", "b7"]
    again = world.read()
    assert [entry["ref"] for entry in again["blocks"]] == [entry["ref"] for entry in result["blocks"]]
    for request in world.fake.requests:
        assert request.headers["Notion-Version"] == "2026-03-11"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.method == "GET"


def test_read_follows_pagination(world):
    for index in range(130):
        world.fake.add_block(world.page, "paragraph", [rt(f"line {index}")])
    world.fake.page_size_cap = 50
    result = world.read()
    assert [entry["text"] for entry in result["blocks"]] == [f"line {index}" for index in range(130)]
    assert result["truncated"] is False


def test_read_outside_a_run_is_refused_without_contacting_notion(world):
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        world.fake.mount(mock)
        with pytest.raises(NotionToolError, match="only inside a tool run"):
            asyncio.run(read_page({"page": "adapter-test"}, settings=world.settings, ledger=None))
    assert world.fake.requests == []


@pytest.mark.parametrize("mode, expected", [
    ("reject", "Notion refused the read (object_not_found): share the page with DaveLLM's Notion connection"),
    ("connect_error", "Notion could not be reached"),
    ("redirect", "Notion could not be reached"),
    ("drop_before_apply", "Notion could not be reached"),
])
def test_read_errors_are_fixed_messages(world, mode, expected):
    world.fake.fail("GET", r"/pages/.*", mode, status=404, code="object_not_found")
    assert world.refuse(read_page, page="adapter-test") == expected
    assert all(request.url.host == "api.notion.com" for request in world.fake.requests)


def test_read_refuses_a_trashed_page(world):
    world.fake.pages[world.page]["in_trash"] = True
    assert world.refuse(read_page, page="adapter-test") == "Notion page 'adapter-test' is in the trash"


# ---- notion.page.append ---------------------------------------------------

BLOCKS = [
    {"type": "heading_3", "text": "DaveLLM test run"},
    {"type": "paragraph", "text": "Appended by the adapter."},
    {"type": "to_do", "text": "Check the result", "checked": False},
]


def test_append_adds_blocks_at_the_end_and_verifies_them(world):
    existing = world.fake.add_block(world.page, "paragraph", [rt("Keep me")])
    result = world.call(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert result == {"outcome": "verified", "page": "adapter-test", "blocks_added": 3, "refs": ["b1", "b2", "b3"]}
    assert world.fake.page_texts(world.page) == [
        ("paragraph", "Keep me"), ("heading_3", "DaveLLM test run"),
        ("paragraph", "Appended by the adapter."), ("to_do", "Check the result"),
    ]
    (patch,) = world.fake.writes()
    body = json.loads(patch.content)
    assert body["position"] == {"type": "end"} and "after" not in body
    assert body["children"][2]["to_do"] == {"rich_text": [{"type": "text", "text": {"content": "Check the result"}}],
                                            "checked": False}
    assert world.fake.text_of(existing) == "Keep me"


def test_append_refused_by_notion_writes_nothing_and_may_be_retried(world):
    world.fake.fail("PATCH", r"/blocks/.*/children", "reject", status=400, code="validation_error")
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message == "Nothing was written: Notion refused the append (validation_error)"
    assert world.fake.page_texts(world.page) == []
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"


@pytest.mark.parametrize("mode", ["drop_after_apply", "server_error_after_apply"])
def test_append_with_a_lost_answer_is_verified_by_reading_the_page(world, mode):
    world.fake.fail("PATCH", r"/blocks/.*/children", mode)
    result = world.call(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert result["outcome"] == "verified"
    assert len(world.fake.writes()) == 1
    assert [text for _, text in world.fake.page_texts(world.page)] == [block["text"] for block in BLOCKS]


def test_append_that_never_arrived_is_unknown_and_never_repeated(world):
    world.fake.fail("PATCH", r"/blocks/.*/children", "drop_before_apply")
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message.startswith("Outcome unknown: the connection to Notion failed during the append and the blocks "
                              "are not on the page yet. Do not repeat this write")
    again = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert again.startswith("An earlier append of these exact blocks to 'adapter-test' in this run has an unknown outcome")
    assert len(world.fake.writes()) == 1
    assert world.fake.page_texts(world.page) == []


def test_append_with_a_concurrent_human_edit_stays_unknown(world):
    original = world.fake.handle

    def human_then_lost(request):
        if request.method == "PATCH":
            world.fake.add_block(world.page, "paragraph", [rt("typed by a person")])
            world.fake.requests.append(request)
            raise httpx.ReadTimeout("lost", request=request)
        return original(request)

    world.fake.handle = human_then_lost
    message = world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS)
    assert message.startswith("Outcome unknown: the connection to Notion failed during the append and the new "
                              "blocks do not match exactly")


def test_append_connect_failure_writes_nothing(world):
    world.fake.fail("PATCH", r"/blocks/.*/children", "connect_error")
    assert world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS) == (
        "Nothing was written: the request could not be sent to Notion")
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"


def test_append_rate_limit_is_retried_once_after_retry_after(world):
    world.fake.fail("PATCH", r"/blocks/.*/children", "rate_limit", retry_after="0")
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"
    assert len(world.fake.writes()) == 2
    assert len(world.fake.page_texts(world.page)) == 3


def test_a_second_write_to_a_busy_page_is_refused(world, monkeypatch):
    monkeypatch.setattr(notion, "NOTION_PAGE_WAIT_SECONDS", 0.2)
    assert world.guard.acquire(world.page)
    assert world.refuse(append_blocks, page="adapter-test", blocks=BLOCKS) == notion.PAGE_BUSY
    assert world.fake.requests == []
    world.guard.release(world.page)
    assert world.call(append_blocks, page="adapter-test", blocks=BLOCKS)["outcome"] == "verified"


@pytest.mark.parametrize("blocks, message", [
    ([], "blocks must list at least one block"),
    ([{"type": "paragraph", "text": "x"}] * 51, "blocks may list at most 50 blocks"),
    ([{"type": "image", "text": "x"}], "Block 1 has an unsupported type"),
    ([{"type": "paragraph"}], "Block 1 needs text"),
    ([{"type": "paragraph", "text": "x" * 2001}], "Block 1 text exceeds 2000 characters"),
    ([{"type": "paragraph", "text": "x", "checked": True}], "Block 1 is not a to_do, so it cannot be checked"),
])
def test_append_arguments_are_checked_before_anything_is_sent(world, blocks, message):
    assert world.refuse(append_blocks, page="adapter-test", blocks=blocks) == message
    assert world.fake.requests == []


# ---- notion.block.update ----------------------------------------------------

def test_edit_inside_one_run_keeps_every_other_run_exactly(world):
    fake = world.fake
    target = fake.add_page("Linked page")
    user = new_id()
    rich = [
        rt("Status: "), rt("in progress", bold=True, color="red"), rt(" see "),
        rt("the spec", link="https://example.com/spec", italic=True), rt(" by "),
        user_mention(user, "Dave"), rt(" on "), date_mention("2026-10-01"), rt(" and "),
        page_mention(target, "Linked page"), rt(" "), equation("E=mc^2"),
    ]
    block = fake.add_block(world.page, "paragraph", rich)
    before = notion.rich_text_signature(fake.blocks[block]["paragraph"]["rich_text"])
    world.read()
    result = world.call(update_block, page="adapter-test", block=world.ref(block),
                        old_text="in progress", new_text="done")
    assert result["outcome"] == "verified" and result["block"] == world.ref(block)
    after = notion.rich_text_signature(fake.blocks[block]["paragraph"]["rich_text"])
    assert after[1][1] == "done" and after[1][2] == before[1][2]
    assert after[:1] + after[2:] == before[:1] + before[2:]
    assert fake.text_of(block) == "Status: done see the spec by @Dave on 2026-10-01 and Linked page E=mc^2"


def test_edits_crossing_formatting_or_mentions_are_refused_before_sending(world):
    block = world.fake.add_block(world.page, "paragraph",
                                 [rt("plain "), rt("bold", bold=True), rt(" "), page_mention(world.page, "Me")])
    world.read()
    ref = world.ref(block)
    for old in ("plain bold", "bold ", " Me", "Me"):
        assert "crosses a formatting change" in world.refuse(update_block, page="adapter-test", block=ref,
                                                             old_text=old, new_text="x")
    assert world.refuse(update_block, page="adapter-test", block=ref, old_text="absent", new_text="x") == \
        f"old_text was not found in block {ref}"
    assert world.fake.writes() == []


def test_split_runs_with_identical_formatting_edit_as_one(world):
    block = world.fake.add_block(world.page, "paragraph", [rt("hel", bold=True), rt("lo world", bold=True)])
    world.read()
    world.call(update_block, page="adapter-test", block=world.ref(block), old_text="hello", new_text="goodbye")
    assert world.fake.text_of(block) == "goodbye world"
    assert all(item["annotations"]["bold"] for item in world.fake.blocks[block]["paragraph"]["rich_text"])


def test_repeated_old_text_is_refused(world):
    block = world.fake.add_block(world.page, "paragraph", [rt("x and x")])
    world.read()
    assert "occurs 2 times" in world.refuse(update_block, page="adapter-test", block=world.ref(block),
                                            old_text="x", new_text="y")


def test_checking_a_to_do_sends_only_the_checked_state(world):
    block = world.fake.add_block(world.page, "to_do", [rt("Item", bold=True)], checked=False)
    world.read()
    result = world.call(update_block, page="adapter-test", block=world.ref(block), checked=True, block_text="Item")
    assert result == {"outcome": "verified", "page": "adapter-test", "block": world.ref(block),
                      "text": "Item", "checked": True}
    (patch,) = world.fake.writes()
    assert json.loads(patch.content) == {"to_do": {"checked": True}}
    assert world.refuse(update_block, page="adapter-test", block=world.ref(block), checked=True,
                        block_text="Item") == f"Block {world.ref(block)} is already checked"


def test_a_block_changed_after_the_read_is_not_overwritten(world):
    block = world.fake.add_block(world.page, "paragraph", [rt("draft text")])
    world.read()
    world.fake.set_text(block, [rt("draft text, edited by Dave")])
    message = world.refuse(update_block, page="adapter-test", block=world.ref(block),
                           old_text="draft", new_text="final")
    assert message == f"Nothing was written: block {world.ref(block)} changed after it was read; read the page again"
    assert world.fake.writes() == []
    assert world.fake.text_of(block) == "draft text, edited by Dave"


def test_a_block_moved_off_the_page_is_refused_but_nesting_on_the_page_is_fine():
    world = World(second_page=True)
    toggle = world.fake.add_block(world.page, "toggle", [rt("Holder")])
    block = world.fake.add_block(world.page, "paragraph", [rt("movable")])
    world.read()
    world.fake.move(block, toggle)
    assert world.call(update_block, page="adapter-test", block=world.ref(block),
                      old_text="movable", new_text="nested")["outcome"] == "verified"
    world.fake.move(block, world.other)
    message = world.refuse(update_block, page="adapter-test", block=world.ref(block),
                           old_text="nested", new_text="x")
    assert message == f"Nothing was written: block {world.ref(block)} is no longer on page 'adapter-test'"
    assert len(world.fake.writes()) == 1


def test_refs_are_bound_to_their_page_and_their_run():
    world = World(second_page=True)
    block = world.fake.add_block(world.other, "paragraph", [rt("other page text")])
    world.read("other")
    ref = world.ref(block)
    assert world.refuse(update_block, page="adapter-test", block=ref, old_text="other", new_text="x") == \
        f"Block {ref} belongs to page 'other', not 'adapter-test'"
    assert world.refuse(update_block, RunLedger(), page="other", block=ref, old_text="other", new_text="x") == \
        f"Unknown block ref {ref}; refs come from notion.page.read in this run"
    assert world.refuse(update_block, page="other", block="b999", old_text="o", new_text="x") == \
        "Unknown block ref b999; refs come from notion.page.read in this run"
    assert world.refuse(update_block, page="other", block="../x", old_text="o", new_text="x") == \
        "Unknown block ref (invalid); refs come from notion.page.read in this run"
    assert world.fake.writes() == []


def test_uncertain_edit_is_verified_when_applied_and_unknown_when_not(world):
    block = world.fake.add_block(world.page, "paragraph", [rt("first")])
    world.read()
    ref = world.ref(block)
    world.fake.fail("PATCH", rf"/blocks/{block}", "drop_after_apply")
    assert world.call(update_block, page="adapter-test", block=ref, old_text="first", new_text="second")[
        "outcome"] == "verified"
    world.fake.fail("PATCH", rf"/blocks/{block}", "drop_before_apply")
    message = world.refuse(update_block, page="adapter-test", block=ref, old_text="second", new_text="third")
    assert message.startswith(f"Outcome unknown: the connection to Notion failed during the edit and block {ref} "
                              "still shows the old content")
    assert world.fake.text_of(block) == "second"


def test_blocks_that_cannot_round_trip_refuse_text_edits_but_allow_checking(world):
    block = world.fake.add_block(world.page, "to_do", [rt("see "), link_preview_mention("https://example.com")])
    internal = world.fake.add_block(world.page, "paragraph", [rt("internal", link="/0123456789abcdef0123456789abcdef")])
    world.read()
    assert "contains a link_preview mention that cannot be written back" in world.refuse(
        update_block, page="adapter-test", block=world.ref(block), old_text="see", new_text="look")
    assert "contains an internal link" in world.refuse(
        update_block, page="adapter-test", block=world.ref(internal), old_text="internal", new_text="x")
    assert world.call(update_block, page="adapter-test", block=world.ref(block), checked=True,
                      block_text="see https://example.com")["outcome"] == "verified"


def test_a_conflict_from_notion_is_a_failed_write(world):
    block = world.fake.add_block(world.page, "paragraph", [rt("text")])
    world.read()
    world.fake.fail("PATCH", rf"/blocks/{block}", "reject", status=409, code="conflict_error")
    assert world.refuse(update_block, page="adapter-test", block=world.ref(block), old_text="text",
                        new_text="new") == "Nothing was written: Notion refused the edit (conflict_error)"


# ---- DaveLLM integration ------------------------------------------------------

NOTION_TOOLS = {"notion.page.read", "notion.page.append", "notion.block.update"}


@pytest.fixture
def notion_router(router_factory, monkeypatch):
    def load(*, enabled=True, pages=None):
        monkeypatch.setenv("DAVE_ENABLE_NOTION_TOOLS", "true" if enabled else "false")
        monkeypatch.setenv("DAVE_NOTION_TOKEN", TOKEN)
        monkeypatch.setenv("DAVE_NOTION_PAGES", json.dumps(pages or {}))
        return router_factory(tools=True)[:2]
    return load


def test_flag_registers_exactly_the_three_notion_tools(notion_router):
    off, _ = notion_router(enabled=False)
    assert NOTION_TOOLS.isdisjoint(off.HARNESS_REGISTRY.public_catalog())
    on, client = notion_router(enabled=True)
    catalog = on.HARNESS_REGISTRY.public_catalog()
    assert NOTION_TOOLS <= set(catalog)
    assert set(catalog) - NOTION_TOOLS == set(off.HARNESS_REGISTRY.public_catalog())
    for name in NOTION_TOOLS:
        definition = on.HARNESS_REGISTRY.get(name)
        assert definition.async_handler and definition.cancellation == "bounded"
        assert (definition.permission, definition.approval_required) == (
            ("read", False) if name == "notion.page.read" else ("write", True))
    for name in off.HARNESS_REGISTRY.public_catalog():
        assert off.HARNESS_REGISTRY.get(name).fingerprint() == on.HARNESS_REGISTRY.get(name).fingerprint()
    listed = client.get("/tools", headers=AUTH)
    assert listed.status_code == 200 and TOKEN not in listed.text


def test_notion_tools_outside_a_lifecycle_run_never_reach_notion(notion_router):
    router, client = notion_router(pages={"adapter-test": new_id()})
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        route = mock.route(host="api.notion.com")
        result = client.post("/tools/execute", headers=AUTH,
                             json={"tool": "notion.page.read", "params": {"page": "adapter-test"}}).json()
    assert result["status"] == "error"
    assert result["error"] == notion.NOT_IN_RUN
    assert not route.called


def _model_turns(*turns):
    responses = []
    for name, arguments in turns:
        if name is None:
            responses.append(httpx.Response(200, json={"message": {"role": "assistant", "content": arguments},
                                                       "done": True}))
        else:
            responses.append(httpx.Response(200, json={"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": f"call_{len(responses)}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)}}],
            }, "done": True}))
    return responses


def _settled(client, run_id):
    for _ in range(200):
        response = client.get(f"/tools/agent/runs/{run_id}", headers=AUTH).json()
        if response["status"] not in {"created", "running"}:
            return response
        time.sleep(0.01)
    raise AssertionError("Run did not settle")


@contextmanager
def _persistent_loop(client):
    """Keep run tasks alive across requests without starting the background summarizer."""
    with anyio.from_thread.start_blocking_portal(backend="asyncio") as portal:
        client.portal = portal
        try:
            yield
        finally:
            client.portal = None


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_lifecycle_run_reads_then_edits_only_after_approval(notion_router, decision):
    fake = FakeNotion()
    page = fake.add_page("DaveLLM Adapter Test")
    keep = fake.add_block(page, "paragraph", [rt("Do not touch", bold=True)])
    todo = fake.add_block(page, "to_do", [rt("Prove the adapter")], checked=False)
    router, client = notion_router(pages={"adapter-test": page})
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock, _persistent_loop(client):
        fake.mount(mock)
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(return_value=httpx.Response(
            200, json={"models": [{"name": MODEL, "model": MODEL}]}))
        assert client.get("/nodes/node-test/models", headers=AUTH).status_code == 200
        chat = mock.post(f"{TEST_NODE_URL}/api/chat")
        chat.side_effect = _model_turns(
            ("notion.page.read", {"page": "adapter-test"}),
            ("notion.block.update", {"page": "adapter-test", "block": "b2", "checked": True,
                                     "block_text": "Prove the adapter"}),
            (None, "Checked it."),
        )
        created = client.post("/tools/agent/runs", headers=AUTH, json={
            "messages": [{"role": "user", "content": "Check the adapter item"}],
            "node_id": "node-test", "model": MODEL,
        })
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        paused = _settled(client, run_id)
        assert paused["status"] == "approval_required"
        pending = paused["snapshot"]["pending_call"]
        assert pending["tool_name"] == "notion.block.update" and pending["permission"] == "write"
        assert fake.writes() == []
        decided = client.post(f"/tools/agent/runs/{run_id}/decisions", headers=AUTH, json={
            **{key: pending[key] for key in ("call_id", "digest", "definition_fingerprint", "permission", "nonce")},
            "decision": decision,
        })
        assert decided.status_code == 200, decided.text
    if decision == "approve":
        assert decided.json()["status"] == "completed"
        assert fake.blocks[todo]["to_do"]["checked"] is True
        assert len(fake.writes()) == 1
        tool_messages = [item for item in decided.json()["snapshot"]["transcript"] if item["role"] == "tool"]
        envelope = json.loads(tool_messages[-1]["content"])
        assert envelope["status"] == "success" and envelope["termination"] == "completed"
        assert json.loads(envelope["result"])["outcome"] == "verified"
    else:
        assert decided.json()["status"] == "approval_rejected"
        assert fake.writes() == []
        assert fake.blocks[todo]["to_do"]["checked"] is False
    assert fake.text_of(keep) == "Do not touch"
    assert all(TOKEN not in json.dumps(item) for item in decided.json()["snapshot"]["transcript"])
    assert run_id in router.NOTION_LEDGERS._ledgers
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    router.HARNESS_STORE.clock = lambda: later
    assert client.get(f"/tools/agent/runs/{run_id}", headers=AUTH).status_code == 410
    assert run_id not in router.NOTION_LEDGERS._ledgers
