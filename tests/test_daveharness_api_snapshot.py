"""Reviewed public signatures and serialized fields; regeneration is deliberate."""

import inspect
import json
from dataclasses import fields, is_dataclass
from pathlib import Path

import daveharness


def public_surface():
    surface = {}
    for name in sorted(daveharness.__all__):
        value = getattr(daveharness, name)
        entry = {}
        if callable(value):
            try:
                signature = inspect.signature(value)
            except (ValueError, TypeError):
                signature = None
            if signature is not None:
                entry["parameters"] = [
                    {"name": item.name, "kind": item.kind.name,
                     "required": item.default is inspect.Parameter.empty}
                    for item in signature.parameters.values()
                ]
        if isinstance(value, type) and is_dataclass(value):
            entry["fields"] = [item.name for item in fields(value)]
        if name in {"RUN_STATUSES", "TERMINAL_RUN_STATUSES", "KNOWN_PERMISSIONS"}:
            entry["values"] = sorted(value)
        surface[name] = entry
    return surface


def test_public_api_matches_reviewed_snapshot():
    expected = json.loads((Path(__file__).parent / "fixtures/daveharness/public_api.json").read_text())
    assert public_surface() == expected
