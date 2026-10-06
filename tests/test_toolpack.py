"""Expansion catalog, permission, preservation, utility and owner contracts."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from daveharness.schema import SchemaValidationError, validate_schema_definition
from davellm_toolpack import admitted_arguments, digest, redact, bounded_result, ToolpackError
from davellm_toolpack_catalog import SPECS, ALL_SPECS, BY_NAME
from davellm_toolpack_local import execute_local, calculator

EXPECTED = """ollama.ps model.pull model.delete model.unload model.warm node.wake node.diagnose node.disk node.ctx_check cluster.benchmark model.inventory_drift ollama.version_check route.suggest model.ask model.consensus token.count chat.summarize prompt.lint eval.run perf.read memory.recall memory.propose vector.search vector.index project.notepad.write project.brain.pin chat.read chat.export docs.ask pdf.read claude.memory.read glossary.lookup notion.search notion.db.query notion.db.create_row notion.db.update_props notion.page.create notion.comment.add notion.page.diff notion.log_session notion.routines.read notion.inbox.triage gh.pr.status gh.pr.comments gh.ci.logs gh.issue.create git.worktrees git.commit git.push test.run code.symbols code.grep_regex lint.run dep.audit pages.deploy_status vercel.deploy_status rules.lookup machine.access.lookup comms.log.append comms.log.read nextsteps.read nextsteps.update agent.claim.check runner.select ha.state.get ha.service.call ha.history ha.config.check docker.ps docker.logs mac.disk.report mac.pressure tailscale.status service.health gmail.search gmail.draft calendar.list calendar.suggest_time drive.search drive.read contacts.lookup notify.push dmx.patch_check artnet.discover osc.send resolume.status midi.map.lookup av.inventory.lookup video.probe video.gap_find audio.transcribe setlist.parse calc.eval time.convert image.describe ocr.image data.query json.validate diagram.render secret.scan""".split()


def sample(schema):
    kind = schema["type"]
    if "enum" in schema:
        return schema["enum"][0]
    if kind == "string":
        return "x" * max(1, schema.get("minLength", 1))
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "boolean":
        return False
    if kind == "array":
        return [sample(schema["items"])]
    return {key: sample(value) for key, value in schema["properties"].items()}


@pytest.fixture
def pack(router_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("DAVE_ENABLE_TOOLPACK", "true")
    monkeypatch.setenv("DAVE_NOTION_TOKEN", "ntn_" + "fixture_only_" * 4)
    monkeypatch.setenv("DAVE_NOTION_PAGES", json.dumps({"test": "1" * 32}))
    config = {"sources": {"notes": {"path": str(tmp_path / "notes.md"), "users": ["default"]}},
              "glossary": {"Dominic": "Linux runner"}, "midi": {"fader1": "volume"},
              "av_inventory": {"speaker": {"count": 2}}, "runners": {"runner": {"capabilities": ["python"]}}}
    monkeypatch.setenv("DAVE_TOOLPACK_CONFIG", json.dumps(config))
    router, client, _ = router_factory(tools=True, tool_roots=[str(tmp_path)])
    binding = router.HostRunBinding("default", "http://ollama.test:11434", "m", 128, 0, "p", None, None, {}, "fixture-run")
    token = router.HOST_RUN_CONTEXT.set(binding)
    router.PROJECTS["p"] = {"name": "Fixture", "user_id": "default", "notepad": "before"}
    router.get_project("p", "default")
    host = router.TOOLPACK_HOST
    (tmp_path / "notes.md").write_text("# Notes\nCOMMS-3 append, not replace.\nDominic fixture.\n# 📡 Recorded Agent Output\n\n## Fixture entry\n\nSafe fixture\n", encoding="utf-8")
    router.CONVERSATIONS["mine"] = {"user_id": "default", "messages": [{"role": "system", "content": "hidden"}, {"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}]}
    router.CONVERSATIONS["other"] = {"user_id": "someone-else", "messages": [{"role": "user", "content": "private"}]}
    yield router, host, client, tmp_path
    router.HOST_RUN_CONTEXT.reset(token)


def test_exact_100_candidates_and_separate_companions():
    assert len(EXPECTED) == 100
    assert {s.name for s in SPECS} == set(EXPECTED)
    assert len(ALL_SPECS) == 102
    assert all(s.schema["additionalProperties"] is False for s in ALL_SPECS)


@pytest.mark.parametrize("item", ALL_SPECS, ids=lambda s: s.name)
def test_every_schema_accepts_bounded_shape_and_rejects_unknown_fields(item):
    validate_schema_definition(item.schema)
    arguments = sample(item.schema)
    admitted_arguments(item.name, arguments)
    with pytest.raises(SchemaValidationError):
        admitted_arguments(item.name, {**arguments, "arbitrary_command": "must-never-run"})


@pytest.mark.parametrize("item", [s for s in SPECS if any(p["type"] == "array" for p in s.schema["properties"].values())], ids=lambda s: s.name)
def test_runtime_array_caps(item):
    arguments = sample(item.schema)
    key = next(k for k, value in item.schema["properties"].items() if value["type"] == "array")
    arguments[key] *= 51
    with pytest.raises(ToolpackError):
        admitted_arguments(item.name, arguments)


def test_registration_both_registries_permissions_and_provenance(pack):
    router, host, _, _ = pack
    for registry in (router.TOOL_REGISTRY, router.HARNESS_REGISTRY):
        assert set(BY_NAME) <= set(registry.public_catalog())
        for item in ALL_SPECS:
            definition = registry.get(item.name)
            assert definition.permission == item.permission
            assert definition.approval_required is item.approval
            assert definition.cancellation == "bounded"
            assert definition.async_handler is True
            assert bool(definition.preflight) is item.approval
            from daveharness import ToolPolicy, RunPolicyContext
            decision = ToolPolicy().evaluate(registry, item.name, RunPolicyContext())
            assert decision.action == ("pause" if item.approval else "allow")
    # Pinned old definitions must not acquire preflights or altered schemas.
    assert router.HARNESS_REGISTRY.get("file.read").preflight is None


@pytest.mark.parametrize("tools,flag", [(False, False), (False, True), (True, False)])
def test_expansion_is_default_off_and_requires_tools(router_factory, monkeypatch, tools, flag):
    monkeypatch.setenv("DAVE_ENABLE_TOOLPACK", "true" if flag else "false")
    router, _, _ = router_factory(tools=tools)
    assert not set(BY_NAME) & set(router.TOOL_REGISTRY.public_catalog())


@pytest.mark.asyncio
async def test_dispatch_returns_router_result_and_redacts_errors(pack):
    router, _, _, _ = pack
    result = await router.HARNESS_REGISTRY.get("calc.eval").handler({"expression": "17*3", "from_unit": "", "to_unit": ""})
    assert result.status == "success" and json.loads(result.result)["value"] == 51
    refused = await router.HARNESS_REGISTRY.get("calc.eval").handler({"expression": "__import__('os')", "from_unit": "", "to_unit": ""})
    assert refused.status == "error" and "Only numeric" in refused.error


@pytest.mark.parametrize("expression", ["__import__('os')", "x+1", "[1]*999999", "2**1000000", "9**9**9", "1e999", "(-1)**.5", "1/0"])
def test_calculator_hostile_inputs_are_bounded(expression):
    with pytest.raises((ToolpackError, ZeroDivisionError)):
        calculator(expression)


@pytest.mark.parametrize("name,args,field,expected", [
    ("calc.eval", {"expression": "2+3*4", "from_unit": "", "to_unit": ""}, "value", 14),
    ("calc.eval", {"expression": "12", "from_unit": "in", "to_unit": "ft"}, "value", 1),
    ("time.convert", {"timestamp": "2026-10-05T12:00:00Z", "zone": "America/New_York"}, "timestamp", "2026-10-05T08:00:00-04:00"),
    ("json.validate", {"document": '{"x":1}', "schema": '{"type":"object","properties":{"x":{"type":"integer"}},"additionalProperties":false}'}, "valid", True),
    ("prompt.lint", {"text": "Do this on the open page."}, "findings", []),
    ("secret.scan", {"text": "ordinary text"}, "clean", True),
    ("setlist.parse", {"text": "1. Song 3:20"}, "songs", [{"title": "Song", "seconds": 200}]),
    ("dmx.patch_check", {"fixtures": [{"name": "A", "universe": 1, "address": 1, "footprint": 16}]}, "valid", True),
    ("glossary.lookup", {"term": "Dominic"}, "matches", [{"name": "Dominic", "record": "Linux runner"}]),
    ("midi.map.lookup", {"control": "fader1"}, "matches", [{"name": "fader1", "record": "volume"}]),
    ("av.inventory.lookup", {"query": "speaker", "limit": 1}, "matches", [{"name": "speaker", "record": {"count": 2}}]),
])
def test_pure_tools_execute(pack, name, args, field, expected):
    assert execute_local(name, args, pack[1])[field] == expected


@pytest.mark.parametrize("name,args", [("chat.read", {"conversation": "other", "offset": 0, "limit": 1}), ("chat.export", {"conversation": "other"})])
def test_foreign_conversation_refused(pack, name, args):
    with pytest.raises(ToolpackError, match="owner"):
        execute_local(name, args, pack[1])


def test_native_history_and_notepad_freshness(pack):
    router, host, _, _ = pack
    read = execute_local("chat.read", {"conversation": "mine", "offset": 0, "limit": 1}, host)
    assert read["messages"] == [{"role": "user", "content": "hello"}] and read["next_offset"] == 1
    exported = execute_local("chat.export", {"conversation": "mine"}, host)
    assert "hidden" not in exported["markdown"] and not exported["file_written"]
    snapshot = execute_local("project.notepad.snapshot", {}, host)
    changed = execute_local("project.notepad.write", {"text": "after", "expected_digest": snapshot["digest"]}, host)
    assert changed["outcome"] == "verified" and router.PROJECTS["p"]["notepad"] == "after"
    with pytest.raises(ToolpackError, match="changed"):
        execute_local("project.notepad.write", {"text": "stale", "expected_digest": snapshot["digest"]}, host)
    proposed = execute_local("memory.propose", {"text": "proposal", "expected_digest": digest("after")}, host)
    assert proposed["outcome"] == "verified" and "Memory proposal: proposal" in router.PROJECTS["p"]["notepad"]


def test_owner_scoped_brain_pin(pack):
    router, host, _, _ = pack
    current = router.PROJECT_CONTEXT.get_brain("p")
    binding = router.HOST_RUN_CONTEXT.get()
    from dataclasses import replace
    router.HOST_RUN_CONTEXT.set(replace(binding, brain_revision=current["revision"]))
    result = execute_local("project.brain.pin", {"text": "safe fact", "expected_revision": current["revision"]}, host)
    assert result["revision"] > current["revision"]
    with pytest.raises(ToolpackError, match="changed"):
        execute_local("project.brain.pin", {"text": "stale", "expected_revision": current["revision"]}, host)


@pytest.mark.parametrize("name,args", [("rules.lookup", {"source": "notes", "rule": "COMMS-3"}), ("machine.access.lookup", {"source": "notes", "machine": "Dominic"}), ("comms.log.read", {"source": "notes", "limit": 2}), ("claude.memory.read", {"source": "notes", "path": "."})])
def test_configured_source_reads_and_owner_refusal(pack, name, args):
    _, host, _, _ = pack
    result = execute_local(name, args, host)
    assert result
    host.config["sources"]["notes"]["users"] = ["other"]
    with pytest.raises(ToolpackError, match="owner"):
        execute_local(name, args, host)


def test_readers_do_not_implicitly_admit_home_or_secrets(pack):
    _, host, _, root = pack
    secret = root / ".env"
    secret.write_text("DO_NOT_READ=fixture", encoding="utf-8")
    host.config["sources"]["secret"] = {"path": str(secret), "users": ["default"]}
    with pytest.raises(Exception):
        execute_local("comms.log.read", {"source": "secret", "limit": 1}, host)
    with pytest.raises(ToolpackError):
        execute_local("claude.memory.read", {"source": "notes", "path": "../../outside"}, host)


def test_redaction_caps_no_credential_or_ip_in_output():
    payload = {"token": "do-not-display", "url": "http://private/", "text": "Bearer abcdefghijklmnop at 100.100.62.111", "x": "x" * 300000}
    result = bounded_result(payload)
    assert len(result.encode()) <= 48 * 1024
    assert "do-not-display" not in result and "abcdefghijklmnop" not in result and "100.100.62.111" not in result
    assert redact("opaque-exact-secret", ("opaque-exact-secret",)) == "[redacted]"


def test_no_inference_or_jobs_enabled_by_registration(pack):
    router, host, _, _ = pack
    assert not host.config["inference_enabled"] and not host.config["jobs_enabled"]
    for name, args in (("model.ask", {"node": "node-test", "model": "m", "text": "x", "max_tokens": 1}), ("test.run", {"runner": "runner", "recipe": "test"})):
        definition = router.HARNESS_REGISTRY.get(name)
        assert "disabled" in definition.preflight(args, None)


def test_run_cleanup_drops_private_refs(pack):
    router, host, _, _ = pack
    host.state()["refs"]["r1"] = {"private": "fixture"}
    host.drop("fixture-run")
    assert not host.states


def test_notepad_concurrent_calls_cannot_both_use_the_same_snapshot(pack):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    _, host, _, _ = pack
    def write(text):
        try:
            return execute_local("project.notepad.write", {"text": text, "expected_digest": digest("before")}, host)["outcome"]
        except ToolpackError:
            return "refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(copy_context().run, write, text) for text in ("one", "two")]
        assert sorted(future.result() for future in futures) == ["refused", "verified"]


def test_perf_filters_legacy_cost_rows_by_owned_existing_conversation(pack):
    import sqlite3
    router, host, _, _ = pack
    with sqlite3.connect(router.PERFORMANCE_DB) as connection:
        connection.execute("INSERT INTO performance (user_id,model_id,tokens,cost) VALUES ('default','mine',10,0)")
        connection.execute("INSERT INTO performance (user_id,model_id,tokens,cost) VALUES ('other','private',100,1)")
    router.COST_LOG.write_text("\n".join(json.dumps({"conversation_id": name, "model": name, "tokens": 1, "cost": 0}) for name in ("mine", "other", "deleted")))
    result = execute_local("perf.read", {"limit": 10}, host)
    assert [row["model"] for row in result["performance"]] == ["mine"]
    assert [row["model"] for row in result["costs"]] == ["mine"]


def test_configured_index_is_owner_filtered_and_never_reads_active_vector_db(pack):
    import sqlite3
    router, host, _, root = pack
    database = root / "index.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE tool_documents(owner,source,path,content)")
        connection.execute("INSERT INTO tool_documents VALUES ('default','docs','a.md','alpha beta gamma')")
        connection.execute("INSERT INTO tool_documents VALUES ('other','docs','private.md','alpha beta gamma')")
    host.config["sources"]["index"] = {"path": str(database), "users": ["default"]}
    host.config["vector_database_source"] = "index"
    result = execute_local("vector.search", {"query": "alpha beta gamma", "limit": 10}, host)
    assert [row["path"] for row in result["documents"]] == ["a.md"]


def test_remaining_native_read_paths(pack):
    router, host, _, root = pack
    (root / "sample.py").write_text("class Example:\n    def work(self): pass\n")
    assert len(execute_local("code.symbols", {"path": "sample.py"}, host)["symbols"]) == 2
    assert execute_local("token.count", {"node": "node-test", "text": "hello world"}, host)["exact"] is False
    (root / "next.json").write_text('{"id":"fixture"}')
    host.config["sources"]["next"] = {"path": str(root / "next.json"), "users": ["default"]}
    assert execute_local("nextsteps.read", {"source": "next"}, host)["record"]["id"] == "fixture"
    host.config["sources"]["docs"] = {"path": str(root), "users": ["default"]}
    assert execute_local("memory.recall", {"source": "docs", "query": "Example", "limit": 3}, host)["matches"]


@pytest.mark.asyncio
async def test_endpoint_and_credentials_change_invalidates_registered_approval(pack):
    router, host, _, _ = pack
    definition = router.HARNESS_REGISTRY.get("project.notepad.write")
    host.secrets["DAVE_GITHUB_TOKEN"] = "new-fixture-only-value"
    args = {"text": "after", "expected_digest": digest("before")}
    assert "configuration changed" in definition.preflight(args, None)
    result = await definition.handler(args)
    assert result.status == "error" and router.PROJECTS["p"]["notepad"] == "before"


def test_authenticated_catalog_contains_every_candidate_and_denies_missing_key(pack):
    _, _, client, _ = pack
    assert client.get("/tools").status_code == 401
    response = client.get("/tools", headers={"X-API-Key": "test-only-api-key"})
    assert response.status_code == 200 and set(BY_NAME) <= set(response.json()["tools"])


@pytest.mark.asyncio
async def test_outer_deadline_cancels_owned_async_work(pack, monkeypatch):
    from davellm_toolpack import execute
    host = pack[1]
    cancelled = asyncio.Event()
    async def blocked(*args):
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.set()
    monkeypatch.setattr("davellm_toolpack_cluster.execute_cluster", blocked)
    monkeypatch.setattr("davellm_toolpack.TOOLPACK_TIMEOUT_SECONDS", 2.01)
    result = await asyncio.wait_for(execute("node.diagnose", host, host.configuration_digest(), {"node": "node-test"}), 1)
    assert result.status == "error" and cancelled.is_set()
