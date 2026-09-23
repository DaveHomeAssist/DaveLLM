"""Reproducible generated contracts, normalization, schemas and transitions."""

import json

from hypothesis import given, settings, strategies as st
import pytest

from daveharness import (
    ParsedToolCall, RunSnapshot, TERMINAL_RUN_STATUSES,
    decode_contract, encode_contract, parse_tool_calls, transition_run,
    validate_json_schema, SchemaValidationError,
)
from daveharness.limits import validate_payload


settings.register_profile("qualification", max_examples=100, derandomize=True, deadline=None)
settings.load_profile("qualification")
text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=80)
scalars = st.none() | st.booleans() | st.integers(-10**12, 10**12) | st.floats(allow_nan=False, allow_infinity=False) | text
json_values = st.recursive(scalars, lambda children: st.lists(children, max_size=5) | st.dictionaries(text, children, max_size=5), max_leaves=30)
objects = st.dictionaries(text, json_values, max_size=5)
STAMP = "2026-09-23T12:00:00+00:00"
FENCE = chr(96) * 3


@given(objects)
def test_generated_contract_roundtrip_and_detachment(arguments):
    call = ParsedToolCall("call_1", "echo", arguments)
    wire = encode_contract(call)
    assert encode_contract(decode_contract(wire)) == wire
    arguments["later"] = "mutation"
    assert call.arguments != arguments


@given(json_values)
def test_generated_json_size_matches_actual_encoding(value):
    assert validate_payload(value) == len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


@given(objects, st.sampled_from(["", FENCE + "json\n", FENCE + "\n"]))
def test_native_and_fenced_parser_normalize_same_arguments(arguments, fence):
    native, native_error = parse_tool_calls({"tool_calls": [{"id": "call_1", "function": {"name": "echo", "arguments": json.dumps(arguments)}}]})
    body = json.dumps({"tool": "echo", "params": arguments}, ensure_ascii=False)
    fallback, fallback_error = parse_tool_calls({"content": fence + body + ("\n" + FENCE if fence else "")})
    assert native_error is fallback_error is None
    assert native[0].arguments == fallback[0].arguments == arguments
    assert native[0].name == fallback[0].name == "echo"


@given(st.integers(-10000, 10000), st.integers(0, 1000))
def test_generated_schema_bounds(value, ceiling):
    schema = {"type": "integer", "minimum": 0, "maximum": ceiling}
    if 0 <= value <= ceiling:
        validate_json_schema(value, schema)
    else:
        with pytest.raises(SchemaValidationError):
            validate_json_schema(value, schema)


@given(st.sampled_from(sorted(TERMINAL_RUN_STATUSES - {"approval_rejected"})), st.sampled_from(sorted(TERMINAL_RUN_STATUSES)))
def test_generated_terminal_transitions_are_immutable(terminal, target):
    created = RunSnapshot.create(run_id="run_1", transcript=[], created_at=STAMP)
    running = transition_run(created, "running", updated_at=STAMP, steps=1)
    if terminal in {"cancelled", "cancellation_failed"}:
        running = transition_run(running, "cancelling", updated_at=STAMP)
    ended = transition_run(running, terminal, updated_at=STAMP)
    assert ended.optimistic_version > created.optimistic_version
    if target == terminal:
        assert transition_run(ended, target, updated_at=STAMP) is ended
    else:
        with pytest.raises(ValueError, match="terminal"):
            transition_run(ended, target, updated_at=STAMP)
    with pytest.raises(ValueError, match="terminal"):
        transition_run(ended, terminal, updated_at=STAMP, steps=ended.steps + 1)
