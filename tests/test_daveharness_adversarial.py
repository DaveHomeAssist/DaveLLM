"""Hostile payloads fail before copying, serializing, or invoking effects."""

import json
import asyncio
import time
import tracemalloc

import pytest

from daveharness import Harness, RunBudget, RunRequest, ToolDefinition, ToolRegistry, parse_tool_calls, run_tool, validate_json_schema, SchemaValidationError
from daveharness.limits import PayloadLimitError, PayloadLimits, bounded_loads, validate_payload
from daveharness.events import EventJournal, RunEvent


def test_decoder_checks_depth_nodes_and_bytes_before_json_loads(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("decoder invoked before admission")
    monkeypatch.setattr(json, "loads", forbidden)
    for payload, limits in [
        ("[" * 1000 + "0" + "]" * 1000, PayloadLimits(max_depth=32)),
        ("[" + ",".join(["0"] * 1000) + "]", PayloadLimits(max_nodes=50)),
        ('"' + "x" * 1000 + '"', PayloadLimits(max_bytes=100)),
    ]:
        with pytest.raises(PayloadLimitError):
            bounded_loads(payload, limits)


@pytest.mark.parametrize("value", [
    {"content": "\ud800"},
    {"tool_calls": {}},
    {"tool_calls": [{"id": 5, "function": {"name": "echo"}}]},
    {"tool_calls": [{"function": []}]},
    {"content": '{"tool":"echo","params":{"x":NaN}}'},
    {"content": '{"tool":"echo","params":{"x":1,"x":2}}'},
    {"content": '{"tool":12,"params":{}}'},
    {"content": chr(96) * 3 + 'json\n{"tool":"echo",}\n' + chr(96) * 3},
])
def test_malformed_calls_never_normalize(value):
    calls, error = parse_tool_calls(value)
    assert calls == [] and error


def test_duplicate_native_ids_fail_before_any_call_is_returned():
    call = {"id": "same", "function": {"name": "echo", "arguments": "{}"}}
    calls, error = parse_tool_calls({"tool_calls": [call, call]})
    assert calls == [] and error == "duplicate_call_id"


@pytest.mark.parametrize("keyword", ["$ref", "oneOf", "pattern", "minItems", "unevaluatedProperties"])
def test_unsupported_schema_constraints_fail_even_in_absent_properties(keyword):
    with pytest.raises(SchemaValidationError, match="unsupported"):
        validate_json_schema({}, {"type": "object", "properties": {"unused": {keyword: "x"}}})


def test_cycles_nonfinite_numbers_and_deep_arguments_are_bounded():
    cycle = []
    cycle.append(cycle)
    nested = {}
    for _ in range(1000):
        nested = {"child": nested}
    for value in (cycle, nested, float("inf"), 1 << 10000):
        with pytest.raises(PayloadLimitError):
            validate_payload(value)


@pytest.mark.asyncio
async def test_oversized_output_is_rejected_before_serializing():
    registry = ToolRegistry()
    registry.register(ToolDefinition("echo", "", {}, lambda _: ["x" * 1000], handler_version="1"))
    result = await run_tool("echo", {}, registry=registry, output_bytes=100)
    assert result.status == "output_limit" and result.result == ""


@pytest.mark.asyncio
async def test_final_model_answer_cannot_bypass_transcript_ceiling():
    async def model(*_):
        return {"content": "x" * 10000}
    harness = Harness(registry=ToolRegistry(), invoke_model=model)
    result = await harness.start(RunRequest.create([{"role": "user", "content": "hi"}], budget=RunBudget(transcript_bytes=500)))
    assert result.status == "budget_exceeded"
    assert result.reason_code == "transcript_limit"
    assert len(result.snapshot.transcript_json.encode()) < 500
    assert sum(event.kind == "terminal" for event in harness.events(result.snapshot.run_id)) == 1


@pytest.mark.parametrize("count", [32, 64])
def test_expected_load_and_double_headroom_reject_without_copying(count):
    value = {"content": "x" * 2_097_152}
    limits = PayloadLimits(max_bytes=1_048_576)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        for _ in range(count):
            with pytest.raises(PayloadLimitError, match="payload_bytes"):
                validate_payload(value, limits)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_048_576
    assert time.perf_counter() - started < 5


@pytest.mark.asyncio
async def test_event_queue_and_retention_enforce_byte_ceiling_before_sink_delivery():
    release = asyncio.Event()
    class SlowSink:
        async def emit(self, _event):
            await release.wait()
    journal = EventJournal(sink=SlowSink(), max_count=1000, max_bytes=1024)
    try:
        for sequence in range(1, 2001):
            journal.record(RunEvent("run", sequence, "terminal" if sequence == 2000 else "model_result",
                                    1, None, None, None, None, "2026-09-23T12:00:00+00:00"))
        assert journal._pending_bytes <= 1024
        assert sum(event.byte_size for event in journal.events("run")) <= 1024
        assert journal.events("run")[-1].kind == "terminal"
        assert journal.dropped_delivery > 0
    finally:
        release.set()
        await journal.flush()


def test_cyclic_handler_configuration_requires_explicit_version():
    state = []
    state.append(state)
    def handler(_args):
        return len(state)
    with pytest.raises(ValueError, match="handler_version"):
        ToolDefinition("read", "", {}, handler).fingerprint()
    assert ToolDefinition("read", "", {}, handler, handler_version="opaque-v1").fingerprint()
