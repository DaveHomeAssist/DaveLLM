"""Independent regression probes; all network traffic is simulated."""
import asyncio

import httpx
import pytest
import respx
import davellm_notion as notion
from test_notion_tools import World, BLOCKS


def test_cancel_before_dispatch_must_prevent_write(monkeypatch):
    world = World()
    original = notion.NotionApi.call

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def paused(self, method, path, **kwargs):
            if method == 'GET' and path.endswith('/children') and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(self, method, path, **kwargs)
        monkeypatch.setattr(notion.NotionApi, 'call', paused)
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            world.fake.mount(mock)
            caller = asyncio.create_task(notion.append_blocks(
                {'page': 'adapter-test', 'blocks': BLOCKS}, settings=world.settings,
                ledger=world.ledger, guard=world.guard))
            await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            pending = list(notion._WRITES_IN_FLIGHT)
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), 1)
            assert len(world.fake.writes()) == 0, 'PATCH dispatched after caller cancellation, during preflight GET'
    asyncio.run(scenario())


def test_request_budget_is_absolute_during_slow_response(monkeypatch):
    monkeypatch.setattr(notion, 'NOTION_MIN_REQUEST_SECONDS', 0)
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b'{', b'"ok"', b':true', b'}'):
                await asyncio.sleep(.04)
                yield chunk
    async def scenario():
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            mock.get('https://api.notion.com/v1/users/me').mock(
                return_value=httpx.Response(200, stream=SlowBody()))
            async with notion.NotionApi('fake-token', budget_seconds=.06, max_requests=1) as api:
                with pytest.raises(notion._Uncertain):
                    await api.call('GET', '/users/me')
    asyncio.run(scenario())


def test_cancel_after_dispatch_allows_readback_without_second_write(monkeypatch):
    world = World()
    original = notion.NotionApi.call
    async def scenario():
        sent = asyncio.Event()
        release = asyncio.Event()
        async def paused(self, method, path, **kwargs):
            result = await original(self, method, path, **kwargs)
            if method == 'PATCH':
                sent.set()
                await release.wait()
            return result
        monkeypatch.setattr(notion.NotionApi, 'call', paused)
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            world.fake.mount(mock)
            caller = asyncio.create_task(notion.append_blocks(
                {'page': 'adapter-test', 'blocks': BLOCKS}, settings=world.settings,
                ledger=world.ledger, guard=world.guard))
            await asyncio.wait_for(sent.wait(), 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            await asyncio.wait_for(asyncio.gather(*list(notion._WRITES_IN_FLIGHT)), 1)
            assert len(world.fake.writes()) == 1
            assert set(world.ledger._appends.values()) == {'verified'}
            assert not world.guard.busy(world.page)
    asyncio.run(scenario())


def test_cancel_before_update_dispatch_must_prevent_write(monkeypatch):
    from fake_notion import rt
    world = World()
    block_id = world.fake.add_block(world.page, 'paragraph', [rt('before')])
    world.read()
    original = notion.NotionApi.call
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def paused(self, method, path, **kwargs):
            if method == 'GET' and path == f'/blocks/{block_id}' and not entered.is_set():
                entered.set()
                await release.wait()
            return await original(self, method, path, **kwargs)
        monkeypatch.setattr(notion.NotionApi, 'call', paused)
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            world.fake.mount(mock)
            caller = asyncio.create_task(notion.update_block(
                {'page': 'adapter-test', 'block': world.ref(block_id),
                 'old_text': 'before', 'new_text': 'after'},
                settings=world.settings, ledger=world.ledger, guard=world.guard))
            await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            await asyncio.wait_for(asyncio.gather(*list(notion._WRITES_IN_FLIGHT)), 1)
            assert len(world.fake.writes()) == 0
            assert world.fake.text_of(block_id) == 'before'
    asyncio.run(scenario())


def test_cancel_during_rate_limit_wait_prevents_write_retry(monkeypatch):
    world = World()
    world.fake.fail("PATCH", r"/blocks/.*/children", "rate_limit", retry_after="0.1")
    original = notion.NotionApi._send
    async def scenario():
        rate_limited = asyncio.Event()
        async def observe(self, method, path, params, body, timeout):
            result = await original(self, method, path, params, body, timeout)
            if result[0] == 429:
                rate_limited.set()
            return result
        monkeypatch.setattr(notion.NotionApi, "_send", observe)
        with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
            world.fake.mount(mock)
            caller = asyncio.create_task(notion.append_blocks(
                {"page": "adapter-test", "blocks": BLOCKS}, settings=world.settings,
                ledger=world.ledger, guard=world.guard))
            await asyncio.wait_for(rate_limited.wait(), 1)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            await asyncio.wait_for(asyncio.gather(*list(notion._WRITES_IN_FLIGHT)), 1)
            assert len(world.fake.writes()) == 1
            assert world.fake.page_texts(world.page) == []
            assert set(world.ledger._appends.values()) == {"failed"}
    asyncio.run(scenario())
