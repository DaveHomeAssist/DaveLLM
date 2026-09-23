"""Versioned H1 leaf contracts without changes to the legacy HTTP wire shape."""

from dataclasses import replace
import json
from pathlib import Path

import daveharness
from daveharness import (
    CONTRACT_VERSION,
    ContractDecodeError,
    ExecutorOutcome,
    ParsedToolCall,
    PendingCall,
    ToolExecution,
    decode_contract,
    encode_contract,
    parse_tool_calls,
    run_tool,
)
from daveharness import contracts, executor
import httpx
import pytest
import respx
import tool_executor

from conftest import TEST_API_KEY, TEST_NODE_URL


FIXTURES = Path(__file__).parent / "fixtures" / "daveharness" / "v1"
STAMP = "2026-09-23T09:00:00+00:00"


def samples():
    return {
        "ParsedToolCall": ParsedToolCall(
            call_id="call_1", name="echo", arguments={"nested": [1, True, {"text": "é"}]}
        ),
        "PendingCall": PendingCall(
            call_id="call_2",
            tool_name="file.write",
            arguments={"value": "ready"},
            digest="digest_2",
            transcript_revision="revision_2",
            nonce="nonce_2",
            created_at=STAMP,
            expires_at="2026-09-23T09:05:00+00:00",
        ),
        "ToolExecution": ToolExecution(
            call_id="call_3",
            name="echo",
            status="success",
            result="ready",
            error=None,
            started_at=STAMP,
            completed_at="2026-09-23T09:00:01+00:00",
            duration_ms=1.25,
        ),
        "ExecutorOutcome": ExecutorOutcome(
            status="completed",
            transcript=[{"role": "user", "content": "ready"}],
            final_answer="done",
            steps=1,
            errors=0,
            status_message="Model returned a final answer.",
            run_id="run_4",
        ),
    }


@pytest.mark.parametrize("contract_type", list(samples()))
def test_golden_contract_is_canonical_and_round_trips(contract_type):
    value = samples()[contract_type]
    envelope = encode_contract(value)
    canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    assert canonical.encode("utf-8") == (FIXTURES / f"{contract_type}.json").read_bytes()
    assert envelope == encode_contract(value)
    assert decode_contract(json.loads(canonical)) == value
    assert encode_contract(decode_contract(envelope)) == envelope
    assert envelope["contract_version"] == CONTRACT_VERSION == 1


def test_construction_encoding_and_decoding_own_nested_json():
    source = {"nested": [{"value": "original"}]}
    call = ParsedToolCall("call_owned", "echo", source)
    source["nested"][0]["value"] = "source changed"
    assert call.arguments["nested"][0]["value"] == "original"

    envelope = encode_contract(call)
    envelope["payload"]["arguments"]["nested"][0]["value"] = "envelope changed"
    assert call.arguments["nested"][0]["value"] == "original"

    decoded = decode_contract(encode_contract(call))
    assert isinstance(decoded, ParsedToolCall)
    input_envelope = encode_contract(call)
    decoded_from_input = decode_contract(input_envelope)
    input_envelope["payload"]["arguments"]["nested"][0]["value"] = "input changed"
    assert decoded_from_input.arguments["nested"][0]["value"] == "original"
    encoded = encode_contract(decoded)
    encoded["payload"]["arguments"]["nested"][0]["value"] = "encoded changed"
    assert decoded.arguments["nested"][0]["value"] == "original"

    transcript = [{"role": "user", "content": {"items": [1]}}]
    outcome = ExecutorOutcome("completed", transcript, None, 0, 0, "done")
    transcript[0]["content"]["items"].append(2)
    assert outcome.to_dict()["transcript"][0]["content"]["items"] == [1]
    outcome.to_dict()["transcript"][0]["content"]["items"].append(3)
    assert outcome.transcript[0]["content"]["items"] == [1]


def test_encoding_orders_equivalent_nested_objects_identically():
    first = ParsedToolCall("call", "echo", {"z": {"b": 2, "a": 1}, "a": 0})
    second = ParsedToolCall("call", "echo", {"a": 0, "z": {"a": 1, "b": 2}})
    assert json.dumps(encode_contract(first), ensure_ascii=False, separators=(",", ":")) == json.dumps(
        encode_contract(second), ensure_ascii=False, separators=(",", ":")
    )


@pytest.mark.parametrize(
    "value, field",
    [
        (lambda: ParsedToolCall(" ", "echo", {}), "call_id"),
        (lambda: ParsedToolCall("call", None, {}), "name"),
        (lambda: ParsedToolCall("call", "echo", {"bad": float("nan")}), "finite"),
        (lambda: PendingCall("call", "tool", {}, "", "revision", "nonce", STAMP, STAMP), "digest"),
        (lambda: replace(samples()["PendingCall"], created_at="2026-09-23T09:00:00"), "timezone"),
        (lambda: replace(samples()["ToolExecution"], duration_ms=float("inf")), "duration_ms"),
        (lambda: replace(samples()["ToolExecution"], completed_at="yesterday"), "ISO-8601"),
        (lambda: replace(samples()["ExecutorOutcome"], steps=True), "steps"),
        (lambda: replace(samples()["ExecutorOutcome"], run_id=" "), "run_id"),
        (lambda: ParsedToolCall("call", "echo", {1: "bad"}), "string object keys"),
    ],
)
def test_constructors_reject_invalid_values(value, field):
    with pytest.raises(ValueError, match=field):
        value()


def test_constructor_rejects_cyclic_nested_json():
    arguments = {}
    arguments["self"] = arguments
    with pytest.raises(ValueError, match="cycles"):
        ParsedToolCall("call", "echo", arguments)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda e: e.update(contract_version=2), "version"),
        (lambda e: e.update(contract_version=True), "version"),
        (lambda e: e.update(contract_type="RunSnapshot"), "type"),
        (lambda e: e.update(extra="bad"), "envelope"),
        (lambda e: e.pop("contract_type"), "envelope"),
        (lambda e: e["payload"].update(extra="bad"), "fields"),
        (lambda e: e["payload"].pop("call_id"), "fields"),
        (lambda e: e["payload"].update(arguments={"bad": float("inf")}), "finite"),
        (lambda e: e["payload"].update(arguments={1: "bad"}), "string object keys"),
    ],
)
def test_decode_fails_closed_on_invalid_envelopes(mutate, expected):
    envelope = encode_contract(samples()["ParsedToolCall"])
    mutate(envelope)
    with pytest.raises(ContractDecodeError, match=expected):
        decode_contract(envelope)


def test_unknown_version_is_rejected_before_value_construction(monkeypatch):
    envelope = encode_contract(samples()["ParsedToolCall"])
    envelope["contract_version"] = 2
    constructed = []
    monkeypatch.setattr(contracts, "ParsedToolCall", lambda **kwargs: constructed.append(kwargs))
    with pytest.raises(ContractDecodeError, match="version"):
        decode_contract(envelope)
    assert constructed == []


def test_decode_rejects_nonfinite_duration_and_naive_timestamp():
    for field, value, expected in (
        ("duration_ms", float("nan"), "duration_ms"),
        ("started_at", "2026-09-23T09:00:00", "timezone"),
    ):
        envelope = encode_contract(samples()["ToolExecution"])
        envelope["payload"][field] = value
        with pytest.raises(ContractDecodeError, match=expected):
            decode_contract(envelope)


def test_decode_rejects_empty_tool_name_even_when_legacy_value_allows_it():
    for value in (ParsedToolCall("call", "", {}), replace(samples()["ToolExecution"], name="")):
        with pytest.raises(ContractDecodeError, match="name"):
            encode_contract(value)
        envelope = encode_contract(samples()[type(value).__name__])
        envelope["payload"]["name"] = ""
        with pytest.raises(ContractDecodeError, match="name"):
            decode_contract(envelope)


def test_nonleaf_values_are_not_serializable():
    definition = daveharness.ToolDefinition(
        name="echo", description="Echo", parameters={"type": "object"}, handler=lambda args: args
    )
    with pytest.raises(TypeError, match="unsupported"):
        encode_contract(definition)


def test_compatibility_exports_and_legacy_shapes_remain_exact():
    for name in daveharness.__all__:
        assert getattr(tool_executor, name) is getattr(daveharness, name)
    for name in ("ParsedToolCall", "PendingCall", "ToolExecution", "ExecutorOutcome"):
        assert getattr(executor, name) is getattr(daveharness, name)

    execution = samples()["ToolExecution"]
    message = execution.tool_message()
    assert set(message) == {"role", "tool_call_id", "name", "content"}
    assert set(json.loads(message["content"])) == {
        "status", "result", "error", "started_at", "completed_at", "duration_ms", "termination"
    }
    legacy = samples()["ExecutorOutcome"].to_dict()
    assert set(legacy) == {
        "status", "transcript", "final_answer", "steps", "errors", "status_message", "pending_tool_call", "run_id"
    }
    assert "contract_version" not in legacy


@pytest.mark.asyncio
async def test_legacy_empty_tool_name_still_returns_revoked():
    execution = await run_tool("", {})
    assert execution.status == "revoked"
    assert execution.name == ""
    calls, error = parse_tool_calls({"content": '{"tool":" ","params":{}}'})
    assert error is None
    assert len(calls) == 1
    assert calls[0].name == " "


def test_agent_endpoint_keeps_legacy_response_fields(router_factory):
    _, client, _ = router_factory(tools=True)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{TEST_NODE_URL}/api/tags").mock(
            return_value=httpx.Response(200, json={"models": [{"name": "model:latest"}]})
        )
        assert client.get("/nodes/node-test/models", headers={"X-API-Key": TEST_API_KEY}).status_code == 200
        mock.post(f"{TEST_NODE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "done"}}]}
            )
        )
        response = client.post(
            "/tools/agent/run",
            headers={"X-API-Key": TEST_API_KEY},
            json={"messages": [{"role": "user", "content": "go"}], "node_id": "node-test", "model": "model:latest"},
        )
    assert response.status_code == 200
    assert set(response.json()) == {
        "status", "transcript", "final_answer", "steps", "errors", "status_message", "pending_tool_call", "run_id"
    }
