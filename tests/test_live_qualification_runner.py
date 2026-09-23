"""Test the live runner with transport fakes; CI never contacts a model."""

import json
import time

import httpx
import pytest

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
