"""Offline provider fixtures only: no live model, account, network send or write."""
import copy
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from test_toolpack import pack
from davellm_toolpack import ToolpackError, digest
from davellm_toolpack_cluster import execute_cluster
from davellm_toolpack_http import execute_http
from davellm_toolpack_notion import execute_notion


@pytest.mark.asyncio
async def test_cancelled_effect_still_finishes_bounded_readback(pack):
    from davellm_toolpack_http import once
    host = pack[1]
    sent, release, verified = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def send():
        sent.set()
        await release.wait()
        return {"fixture": True}
    async def verify(_record):
        verified.set()
        return True
    task = asyncio.create_task(once(host, "fixture", {}, send, verify))
    await sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.wait_for(verified.wait(), 1)
    await asyncio.sleep(0)
    assert list(host.state()["effects"].values()) == ["verified"]
    with pytest.raises(ToolpackError, match="already"):
        await once(host, "fixture", {}, send, verify)


@pytest.fixture
def transport(pack, monkeypatch):
    router, host, client, root = pack
    host.config.update({"inference_enabled": True, "expected_models": {"node-test": ["m", "missing"]},
                        "repositories": {"repo": "fixture/repo"}, "services": {"health": {"url": "https://health.test/ready"},
                            "resolume": {"url": "https://resolume.test/api/v1/composition", "kind": "resolume"},
                            "homeassistant": {"url": "http://ha.test", "entities": ["light.test"], "allowed_services": ["light.turn_on"]}},
                        "vercel_projects": {"app": "test-project"}, "google_users": ["default"], "calendars": {"cal": "primary"},
                        "drive_folders": {"folder": "folder-id"}, "drive_files": {"file": "file-id"},
                        "notifications": {"phone": {"url": "https://ntfy.test", "topic": "fixture"}},
                        "notion_databases": {"db": {"id": "2" * 32, "writable_properties": ["Name"], "users": ["default"]}}})
    for variable in ("DAVE_GITHUB_TOKEN", "DAVE_VERCEL_TOKEN", "DAVE_GOOGLE_ACCESS_TOKEN", "DAVE_HA_TOKEN"):
        host.secrets[variable] = "fixture-only-credential"
    observed = {"models": [{"name": "m", "size": 42, "size_vram": 20, "context_length": 4096, "expires_at": "later"}]}
    schema = {"Name": {"type": "title", "title": {}}, "Untouched": {"type": "number", "number": {}}}
    dbid = "22222222-2222-2222-2222-222222222222"
    pageid = "11111111-1111-1111-1111-111111111111"
    rowid = "33333333-3333-3333-3333-333333333333"
    pages = {pageid: {"id": pageid, "object": "page", "properties": {"title": {"type": "title", "title": [{"type": "text", "text": {"content": "Test"}}]}}, "last_edited_time": "t"},
             rowid: {"id": rowid, "object": "page", "parent": {"data_source_id": dbid}, "properties": {"Name": {"type": "title", "title": [{"type": "text", "text": {"content": "Old"}}]}, "Untouched": {"type": "number", "number": 7}}, "last_edited_time": "t"}}
    comments, calls, drafts, issue = [], [], {}, {}

    async def request(method, url, *, body=None, headers=None, params=None, text_response=False):
        calls.append((method, url, body, headers, params))
        if "/api/tags" in url or "/api/ps" in url:
            return copy.deepcopy(observed)
        if "/api/version" in url:
            return {"version": "1.2.3"}
        if "/releases/latest" in url:
            return {"tag_name": "v1.2.3"}
        if url.endswith("/api/chat"):
            assert "tools" not in body and 1 <= body["options"]["num_predict"] <= 512
            return {"message": {"content": "fixture answer", "tool_calls": [{"function": {"name": "must.not.execute"}}]}, "done": True, "eval_count": 2, "eval_duration": 1000000000}
        if url.endswith("/api/delete"):
            observed["models"] = []
            return {}
        if url.endswith("/api/generate"):
            if body["keep_alive"] == 0:
                observed["models"] = []
            return {"done": True}
        if url.endswith("/api/pull"):
            return {"status": "success"}
        if "api.notion.com" in url:
            path = url.split("/v1", 1)[1]
            if path.startswith("/data_sources/"):
                return {"results": [copy.deepcopy(pages[rowid])], "has_more": False} if path.endswith("/query") else {"object": "data_source", "properties": copy.deepcopy(schema), "title": [{"type": "text", "text": {"content": "Test DB"}}]}
            if path == "/comments":
                if method == "POST":
                    comments.append({"id": "comment", "rich_text": body["rich_text"]})
                    return comments[-1]
                return {"results": copy.deepcopy(comments)}
            if path == "/pages":
                identifier = "44444444-4444-4444-4444-444444444444"
                pages[identifier] = {"id": identifier, "parent": body["parent"], "properties": {key: {"type": next(iter(value)), **value} for key, value in body["properties"].items()}, "last_edited_time": "new"}
                return copy.deepcopy(pages[identifier])
            identifier = path.rsplit("/", 1)[-1]
            if method == "PATCH":
                for key, value in body["properties"].items():
                    pages[identifier]["properties"][key] = {"type": next(iter(value)), **value}
                pages[identifier]["last_edited_time"] = "updated"
            return copy.deepcopy(pages[identifier])
        if url.endswith("/check-runs"):
            return {"check_runs": [{"name": "checks", "conclusion": "success"}]}
        if "/actions/jobs/" in url:
            return {"id": 1, "check_run_url": "https://api.github.com/repos/fixture/repo/check-runs/1"}
        if url.endswith("/annotations") or url.endswith("/comments"):
            return []
        if "/pulls/" in url:
            return {"number": 1, "title": "PR", "head": {"sha": "a" * 40}, "state": "open"}
        if "/issues" in url:
            if method == "POST":
                issue.update(body, number=1)
            return dict(issue)
        if "gmail.googleapis.com" in url:
            if "/drafts" in url:
                if method == "POST":
                    drafts.update(body, id="draft")
                return copy.deepcopy(drafts)
            return {"messages": [{"id": "message"}]}
        if "calendar" in url:
            return {"items": []}
        if "drive/v3" in url:
            if params and params.get("alt") == "media":
                return "text document"
            return {"mimeType": "text/plain", "name": "File"}
        if "people.googleapis.com" in url:
            return {"results": []}
        if "ntfy.test" in url:
            return {"id": "notification"}
        return {"result": "ok"}
    monkeypatch.setattr(host, "request", request)
    monkeypatch.setattr("davellm_toolpack_http.github_job_logs", AsyncMock(return_value={"lines": ["fixture failure"], "truncated": False}))
    return router, host, calls, pages, observed, schema


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("ollama.ps", {"node": "node-test"}), ("node.diagnose", {"node": "node-test"}),
    ("node.ctx_check", {"node": "node-test", "model": "m"}),
    ("model.inventory_drift", {"node": "node-test"}),
    ("ollama.version_check", {"node": "node-test", "compare_latest": True}),
    ("route.suggest", {"text": "question"}),
    ("model.ask", {"node": "node-test", "model": "m", "text": "question", "max_tokens": 2}),
    ("cluster.benchmark", {"node": "node-test", "model": "m", "text": "question", "max_tokens": 2}),
    ("eval.run", {"node": "node-test", "model": "m", "text": "question", "expected": "fixture answer", "max_tokens": 2}),
    ("chat.summarize", {"conversation": "mine", "node": "node-test", "model": "m", "max_tokens": 2}),
    ("docs.ask", {"source": "notes", "query": "COMMS", "node": "node-test", "model": "m", "max_tokens": 2}),
    ("model.warm", {"node": "node-test", "model": "m"}),
    ("model.unload", {"node": "node-test", "model": "m"}),
    ("model.delete", {"node": "node-test", "model": "m"}),
])
async def test_cluster_handler_paths(transport, name, args):
    _, host, calls, _, _, _ = transport
    result = await execute_cluster(name, args, host)
    assert result
    assert all("ollama.test" in url or url == "https://api.github.com/repos/ollama/ollama/releases/latest" for _, url, *_ in calls)
    if name in ("node.diagnose", "model.inventory_drift"):
        assert result["inventory_drift"]["missing"] == ["missing"]
    if name in ("model.delete", "model.unload", "model.warm"):
        assert result["outcome"] == "verified"


@pytest.mark.asyncio
async def test_node_alias_unreachable_context_and_no_latest_by_default(transport, monkeypatch):
    _, host, calls, _, _, _ = transport
    with pytest.raises(ToolpackError, match="alias"):
        await execute_cluster("node.diagnose", {"node": "not-configured"}, host)
    assert not calls
    async def refused(*args, **kwargs):
        raise ToolpackError("fixture refused")
    monkeypatch.setattr(host, "request", refused)
    record = await execute_cluster("node.diagnose", {"node": "node-test"}, host)
    assert record["verdict"] == "Unknown" and record["installed"] is None
    context = await execute_cluster("node.ctx_check", {"node": "node-test", "model": "m"}, host)
    assert context["observed_loaded"]["state"] == "Unknown"


@pytest.mark.asyncio
async def test_secondary_inference_budget_no_recursive_tools(transport):
    _, host, calls, _, _, _ = transport
    args = {"node": "node-test", "model": "m", "text": "test", "max_tokens": 1}
    for _ in range(3):
        result = await execute_cluster("model.ask", args, host)
        assert "tool_calls" not in result
    with pytest.raises(ToolpackError, match="budget"):
        await execute_cluster("model.ask", args, host)
    assert sum(url.endswith("/api/chat") for _, url, *_ in calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", [
    ("gh.pr.status", {"repository": "repo", "number": 1}),
    ("gh.pr.comments", {"repository": "repo", "number": 1, "limit": 2}),
    ("gh.ci.logs", {"repository": "repo", "job": 1, "limit": 2}),
    ("gh.issue.create", {"repository": "repo", "title": "fixture", "body": "body"}),
    ("pages.deploy_status", {"repository": "repo"}),
    ("vercel.deploy_status", {"project": "app", "limit": 2}),
    ("ha.state.get", {"entity": "light.test"}),
    ("ha.service.call", {"entity": "light.test", "service": "light.turn_on"}),
    ("ha.history", {"entity": "light.test", "since": datetime.now(timezone.utc).isoformat()}),
    ("ha.config.check", {}), ("service.health", {"service": "health"}),
    ("resolume.status", {"service": "resolume"}),
    ("gmail.search", {"query": "fixture", "limit": 2}),
    ("gmail.draft", {"to": "fixture@example.invalid", "subject": "Test", "body": "Draft only"}),
    ("calendar.list", {"calendar": "cal", "start": "2026-10-05T12:00:00Z", "end": "2026-10-05T13:00:00Z", "limit": 2}),
    ("calendar.suggest_time", {"calendar": "cal", "start": "2026-10-05T12:00:00Z", "end": "2026-10-05T13:00:00Z", "minutes": 30}),
    ("drive.search", {"folder": "folder", "query": "' or true", "limit": 2}),
    ("drive.read", {"file": "file"}), ("contacts.lookup", {"query": "fixture", "limit": 2}),
    ("notify.push", {"destination": "phone", "text": "Fixture notification"}),
])
async def test_fixed_provider_handlers(transport, name, args):
    _, host, calls, _, _, _ = transport
    result = await execute_http(name, args, host)
    assert result and calls
    if name in ("gh.issue.create", "gmail.draft"):
        assert result["outcome"] == "verified"
    if name == "notify.push":
        assert result["phone_acceptance"] == "Unknown"
    assert not any("/send" in url for _, url, *_ in calls)


@pytest.mark.asyncio
async def test_unallowlisted_provider_target_refused_before_network(transport):
    _, host, calls, _, _, _ = transport
    for name, args in (("gh.pr.status", {"repository": "other", "number": 1}), ("ha.service.call", {"entity": "light.other", "service": "light.turn_on"}), ("drive.read", {"file": "other"})):
        with pytest.raises(ToolpackError):
            await execute_http(name, args, host)
    assert not calls


@pytest.mark.asyncio
async def test_notifications_dedup_and_sensitive_payload_refusal(transport):
    _, host, calls, _, _, _ = transport
    await execute_http("notify.push", {"destination": "phone", "text": "Fixture"}, host)
    count = len(calls)
    with pytest.raises(ToolpackError, match="rate limited"):
        await execute_http("notify.push", {"destination": "phone", "text": "Fixture"}, host)
    with pytest.raises(ToolpackError, match="sensitive"):
        await execute_http("notify.push", {"destination": "phone", "text": "Bearer abcdefghijklmnop"}, host)
    assert len(calls) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["notion.db.query", "notion.routines.read", "notion.inbox.triage"])
async def test_notion_queries_and_run_refs(transport, name):
    _, host, _, _, _, _ = transport
    host.config["notion_databases"]["db"]["kind"] = "routines" if name == "notion.routines.read" else "inbox"
    result = await execute_notion(name, {"database": "db", "limit": 2}, host)
    assert result["rows"][0]["row"] == "r1" and result["schema_digest"]
    assert "33333333" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["notion.db.create_row", "notion.log_session"])
async def test_notion_created_rows_readback(transport, name):
    _, host, _, _, _, _ = transport
    host.config["notion_databases"]["db"]["kind"] = "session_log"
    read = await execute_notion("notion.db.query", {"database": "db", "limit": 1}, host)
    result = await execute_notion(name, {"database": "db", "properties_json": '{"Name":"Created"}', "expected_digest": read["schema_digest"]}, host)
    assert result["outcome"] == "verified"


@pytest.mark.asyncio
async def test_notion_targeted_write_stale_snapshot_and_uncertain_no_retry(transport, monkeypatch):
    _, host, calls, pages, _, _ = transport
    read = await execute_notion("notion.db.query", {"database": "db", "limit": 1}, host)
    args = {"database": "db", "row": "r1", "properties_json": '{"Name":"New"}', "expected_digest": read["rows"][0]["digest"]}
    result = await execute_notion("notion.db.update_props", args, host)
    assert result["outcome"] == "verified" and pages["33333333-3333-3333-3333-333333333333"]["properties"]["Untouched"]["number"] == 7
    writes = sum(method == "PATCH" for method, *_ in calls)
    with pytest.raises(ToolpackError, match="changed"):
        await execute_notion("notion.db.update_props", args, host)
    assert sum(method == "PATCH" for method, *_ in calls) == writes
    original = host.request
    async def uncertain(method, url, **kwargs):
        if method == "POST" and url.endswith("/pages"):
            raise ToolpackError("response lost")
        return await original(method, url, **kwargs)
    monkeypatch.setattr(host, "request", uncertain)
    args = {"database": "db", "properties_json": '{"Name":"Uncertain"}', "expected_digest": read["schema_digest"]}
    assert (await execute_notion("notion.db.create_row", args, host))["outcome"] == "unknown"
    with pytest.raises(ToolpackError, match="already dispatched"):
        await execute_notion("notion.db.create_row", args, host)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["notion.page.create", "notion.comment.add"])
async def test_notion_page_effects_use_configured_parent_and_snapshot(transport, name):
    _, host, _, _, _, _ = transport
    read = await execute_notion("notion.page.diff", {"page": "test"}, host)
    args = {"page": "test", "expected_digest": read["digest"], "title" if name == "notion.page.create" else "text": "Fixture"}
    result = await execute_notion(name, args, host)
    assert result["outcome"] == "verified"


@pytest.mark.asyncio
async def test_notion_search_does_not_use_workspace_search(transport):
    _, host, calls, _, _, _ = transport
    result = await execute_notion("notion.search", {"query": "Test", "limit": 5}, host)
    assert len(result["matches"]) == 2
    assert not any(url.endswith("/search") for _, url, *_ in calls)


@pytest.mark.asyncio
async def test_pull_requires_fresh_mount_and_capacity_before_dispatch(transport, monkeypatch):
    _, host, calls, _, _, _ = transport
    host.config["allowed_model_pulls"] = {"node-test": ["m"]}
    host.config["model_storage"] = {"node-test": {"runner": "fixture", "minimum_free_bytes": 100}}
    job = AsyncMock(return_value={"free_bytes": 101, "mount_ready": False})
    monkeypatch.setattr("davellm_toolpack_jobs.execute_job", job)
    with pytest.raises(ToolpackError, match="capacity"):
        await execute_cluster("model.pull", {"node": "node-test", "model": "m"}, host)
    assert not any(url.endswith("/api/pull") for _, url, *_ in calls)
    job.return_value["mount_ready"] = True
    result = await execute_cluster("model.pull", {"node": "node-test", "model": "m"}, host)
    assert result["outcome"] == "verified"


@pytest.mark.asyncio
async def test_consensus_distinct_models_and_vision_bounded_payload(transport, pack):
    _, host, calls, _, observed, _ = transport
    observed["models"].append({"name": "second"})
    result = await execute_cluster("model.consensus", {"targets": [{"node": "node-test", "model": model} for model in ("m", "second")], "text": "Fixture", "max_tokens": 2}, host)
    assert len(result["answers"]) == 2
    image = pack[3] / "fixture.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    answer = await execute_cluster("image.describe", {"node": "node-test", "model": "m", "path": str(image), "text": "Fixture", "max_tokens": 2}, host)
    assert answer["text"] == "fixture answer"
    assert any(body.get("messages", [{}])[0].get("images") for method, url, body, *_ in calls if url.endswith("/api/chat"))


@pytest.mark.asyncio
async def test_wake_and_osc_use_only_configured_packets_no_real_network(transport, monkeypatch):
    _, host, _, _, _, _ = transport
    host.config["wake_targets"] = {"node-test": {"mac": "00:11:22:33:44:55", "broadcast": "192.168.1.255"}}
    host.config["osc_targets"] = {"desk": {"host": "192.168.1.10", "port": 9000, "addresses": ["/fixture"]}}
    packets = []
    class Sender:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def settimeout(self, *args): pass
        def sendto(self, packet, destination): packets.append((packet, destination))
    monkeypatch.setattr("socket.socket", lambda *args: Sender())
    assert (await execute_cluster("node.wake", {"node": "node-test"}, host))["packet_sent"]
    assert packets[0][0] == b"\xff" * 6 + bytes.fromhex("001122334455") * 16
    result = await execute_http("osc.send", {"target": "desk", "address": "/fixture", "value": "go"}, host)
    assert result["outcome"] == "unknown" and len(packets[-1][0]) % 4 == 0
    with pytest.raises(ToolpackError):
        await execute_http("osc.send", {"target": "desk", "address": "/not-allowed", "value": "go"}, host)
    assert len(packets) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", [302, 307])
async def test_github_log_signed_download_is_dns_pinned_without_auth(pack, monkeypatch, redirect):
    import httpx
    import davellm_toolpack_http as module
    from contextlib import asynccontextmanager
    host = pack[1]
    requests, pins = [], []
    location = "https://signed-public.test/log?signature=fixture"
    def response(request):
        requests.append(request)
        return httpx.Response(redirect, headers={"location": location})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(response), **kwargs))
    @asynccontextmanager
    async def public_client(**kwargs):
        yield object()
    @asynccontextmanager
    async def public_download(client, url, addresses):
        pins.append((url, addresses))
        yield httpx.Response(200, content=b"first\nlast\n")
    monkeypatch.setattr(module, "public_http_client", public_client)
    monkeypatch.setattr(module, "public_stream", public_download)
    monkeypatch.setitem(host.router, "validate_public_url", lambda url: ("93.184.216.34",))
    result = await module.github_job_logs("https://api.github.com/repos/fixture/repo/actions/jobs/1/logs", {"Authorization": "Bearer fixture-secret"}, host, 1)
    assert result == {"lines": ["last"], "truncated": True}
    assert len(requests) == 1 and requests[0].url.host == "api.github.com"
    assert pins == [(location, ("93.184.216.34",))]


@pytest.mark.asyncio
async def test_inbox_routes_are_proposals_only_and_configured(transport):
    _, host, calls, _, _, _ = transport
    config = host.config["notion_databases"]["db"]
    config.update(kind="inbox", routing_rules=[{"contains": "old", "project": "Configured project"}])
    result = await execute_notion("notion.inbox.triage", {"database": "db", "limit": 5}, host)
    assert result["proposals"][0]["projects"] == ["Configured project"]
    assert not any(method in ("PATCH", "DELETE") or method == "POST" and url.endswith("/pages") for method, url, *_ in calls)
