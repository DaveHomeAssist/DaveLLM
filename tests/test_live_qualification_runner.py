"""Test the live runner with transport fakes; CI never contacts a model."""

import json
import time

import httpx
import pytest

import scripts.qualify_live_daveharness as runner
from daveharness import ToolRegistry
from scripts.qualify_live_daveharness import FAMILIES, Resources, cases, confined, evaluate, grade


def test_corpus_is_fixed_balanced_and_read_only_first():
    corpus = cases()
    assert len(corpus) == len({case["id"] for case in corpus}) == 500
    assert all(sum(case["family"] == family for case in corpus) == 25 for family in FAMILIES)
    assert all(not case["family"].startswith("write_") for case in corpus[:375])


def test_resource_reservation_stops_before_dispatch_and_retains_unknown_usage():
    resource = Resources(time.monotonic(), token_limit=5000)
    request = {"messages": [], "max_tokens": 512}
    reservation = resource.reserve(request)
    resource.settle(reservation, {"total_tokens": 200})
    assert resource.tokens == 200
    with pytest.raises(RuntimeError, match="token_budget"):
        resource.reserve({"messages": ["x" * 5000], "max_tokens": 512})
    assert resource.calls == 1
    expired = Resources(time.monotonic() - 10, wall_seconds=1)
    with pytest.raises(RuntimeError, match="wall_budget"):
        expired.reserve(request)
    unknown = Resources(time.monotonic())
    amount = unknown.reserve(request)
    with pytest.raises(RuntimeError, match="usage_unavailable"):
        unknown.settle(amount, {})
    assert unknown.tokens == amount


def test_disposable_root_rejects_escape_and_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(tmp_path)
    for path in ("../outside", "/etc/passwd", "link/outside"):
        with pytest.raises(ValueError, match="root_denied"):
            confined(root, path)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", FAMILIES)
async def test_runner_expected_effects_and_terminal_states_with_fake_transport(family, tmp_path):
    case = next(case for case in cases() if case["family"] == family)
    count = 0
    def respond(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        tool_seen = any(message["role"] == "tool" for message in payload["messages"])
        if family in {"final", "extract", "cancel", "root_escape"} or tool_seen:
            message = {"role": "assistant", "content": case["expected"] + " SECOND_CODE"}
        else:
            name = "file.write" if family.startswith("write_") or family == "permission_denial" else "file.read"
            args = {"path": "out.txt", "content": case["marker"]} if name == "file.write" else {"path": case["source"]}
            if family == "read_limited":
                args["max_chars"] = 5
            calls = [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
            if family == "two_reads":
                calls.append({"id": "call_2", "type": "function", "function": {"name": "file.read", "arguments": '{"path":"second.txt"}'}})
            message = {"role": "assistant", "tool_calls": calls}
            if family.startswith("fallback"):
                assert "tools" not in payload
                message = {"role": "assistant", "content": json.dumps({"tool": name, "params": args})}
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 100}})
    async with httpx.AsyncClient(base_url="http://model.test", transport=httpx.MockTransport(respond)) as client:
        row = await evaluate(case, tmp_path / case["id"], client, Resources(time.monotonic()))
    assert row["semantic_pass"], row
    assert row["unauthorized_effects"] == 0
    assert row["terminal_events"] == 1
    assert count >= 1


@pytest.fixture
def symlinked_root(tmp_path):
    """A disposable root reached through a symlink, as /tmp is on macOS (/tmp -> private/tmp)."""
    real = tmp_path.resolve() / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert link != real and link.resolve() == real
    return link, real


async def run_write_case(case, directory, on_first_call=None):
    """Run one write case against a fake model that proposes the one exact write, then answers."""
    seen = 0
    def respond(request):
        nonlocal seen
        seen += 1
        if seen == 1 and on_first_call:
            on_first_call()
        payload = json.loads(request.content)
        if any(message["role"] == "tool" for message in payload["messages"]):
            message = {"role": "assistant", "content": case["expected"]}
        else:
            args = {"path": "out.txt", "content": case["marker"]}
            message = {"role": "assistant", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "file.write", "arguments": json.dumps(args)}}]}
        return httpx.Response(200, json={"choices": [{"message": message}], "usage": {"total_tokens": 100}})
    async with httpx.AsyncClient(base_url="http://model.test", transport=httpx.MockTransport(respond)) as client:
        return await evaluate(case, directory, client, Resources(time.monotonic()))


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["write_approve", "write_unicode", "write_replay"])
async def test_approved_write_under_a_symlinked_root_is_not_an_unauthorized_effect(family, symlinked_root):
    link, real = symlinked_root
    case = next(case for case in cases() if case["family"] == family)
    row = await run_write_case(case, link / case["id"])
    assert row["unauthorized_effects"] == 0, row
    assert row["writes"] == 1 and row["semantic_pass"] and row["terminal_events"] == 1, row
    assert (real / case["id"] / "out.txt").read_text() == case["marker"]


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["wrong_path", "wrong_content"])
async def test_write_handler_flags_a_wrong_path_or_content_under_a_symlinked_root(effect, symlinked_root, monkeypatch):
    link, real = symlinked_root
    case = next(case for case in cases() if case["family"] == "write_approve")
    probes = {"wrong_path": {"path": "other.txt", "content": case["marker"]},
              "wrong_content": {"path": "out.txt", "content": "WRONG"}}
    handlers = {}
    class SpyRegistry(ToolRegistry):
        def register(self, definition):
            handlers[definition.name] = definition.handler
            super().register(definition)
    probed = []
    decide = runner.exact_decision
    def probe_then_decide(pending, run_id, choice):
        # The runner has just recorded the exact approval; offer the real handler a different effect.
        with pytest.raises(ValueError, match="effect_denied"):
            handlers["file.write"](probes[effect])
        probed.append([path.name for path in (real / case["id"]).iterdir() if path.name in {"out.txt", "other.txt"}])
        return decide(pending, run_id, choice)
    monkeypatch.setattr(runner, "ToolRegistry", SpyRegistry)
    monkeypatch.setattr(runner, "exact_decision", probe_then_decide)
    row = await run_write_case(case, link / case["id"])
    assert probed == [[]], "the refused effect must not have written anything"
    assert row["unauthorized_effects"] == 1, row  # the probe only; the exact approved write is still not flagged
    assert row["writes"] == 1 and (real / case["id"] / "out.txt").read_text() == case["marker"]


@pytest.mark.asyncio
async def test_write_handler_flags_an_output_name_that_is_a_symlink_to_another_file(symlinked_root):
    link, real = symlinked_root
    case = next(case for case in cases() if case["family"] == "write_approve")
    directory = real / case["id"]
    row = await run_write_case(case, link / case["id"], lambda: (directory / "out.txt").symlink_to("second.txt"))
    assert row["unauthorized_effects"] == 1 and row["writes"] == 0, row
    assert (directory / "second.txt").read_text() == "SECOND_CODE"


def test_grading_requires_schema_evidence_and_no_vacuous_completion():
    row = {"schema_valid_calls": 0, "unresolved_after_repair": 0, "repeated_calls": 0,
           "status": "completed", "unauthorized_effects": 0, "semantic_pass": True, "terminal_events": 1}
    report = grade([row] * 500, Resources(time.monotonic()))
    assert not report["qualified"]
    valid = dict(row, schema_valid_calls=1)
    assert grade([valid] * 500, Resources(time.monotonic()))["qualified"]
    assert not grade([dict(valid, unauthorized_effects=1)] + [valid] * 499, Resources(time.monotonic()))["qualified"]


@pytest.mark.asyncio
async def test_transport_failure_stops_batch_and_persists_full_reservation(tmp_path):
    resource = Resources(time.monotonic(), ledger=tmp_path / "authorization.json")
    def fail(_request):
        return httpx.Response(500, json={"error": "model requires more system memory"})
    async with httpx.AsyncClient(base_url="http://model.test", transport=httpx.MockTransport(fail)) as client:
        row = await evaluate(cases()[0], tmp_path / "case", client, resource)
    assert row["status"] == "model_error"
    assert resource.stopped == "transport_HTTPStatusError"
    saved = json.loads(resource.ledger.read_text())
    assert saved["tokens"] == resource.tokens > 0 and saved["calls"] == 1
    evidence = json.loads((tmp_path / "case/evidence.json").read_text())
    assert evidence["transport_failures"][0]["status_code"] == 500
