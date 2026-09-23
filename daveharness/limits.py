"""Bound JSON work before copying, decoding, or serializing untrusted values."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


class PayloadLimitError(ValueError):
    """A content-free failure suitable for a bounded diagnostic."""


@dataclass(frozen=True, slots=True)
class PayloadLimits:
    max_bytes: int = 16_777_216
    max_depth: int = 32
    max_nodes: int = 100_000

    def __post_init__(self) -> None:
        for value in (self.max_bytes, self.max_depth, self.max_nodes):
            if type(value) is not int or value < 1:
                raise ValueError("payload limits must be positive integers")


DEFAULT_PAYLOAD_LIMITS = PayloadLimits()


def text_bytes(value: str, ceiling: int, *, json_string: bool = False) -> int:
    """Count bytes without first allocating an encoded copy of a large string."""
    size = 2 if json_string else 0
    if len(value) + size > ceiling:
        raise PayloadLimitError("payload_bytes")
    for character in value:
        code = ord(character)
        if 0xD800 <= code <= 0xDFFF:
            raise PayloadLimitError("invalid_unicode")
        if json_string and (character in '\\"' or character in '\b\f\n\r\t'):
            size += 2
        elif json_string and code < 32:
            size += 6
        else:
            size += 1 if code < 128 else 2 if code < 2048 else 3 if code < 65536 else 4
        if size > ceiling:
            raise PayloadLimitError("payload_bytes")
    return size


def validate_payload(value: Any, limits: PayloadLimits = DEFAULT_PAYLOAD_LIMITS) -> int:
    """Measure compact UTF-8 JSON with bounded recursion and no data copies."""
    size = 0
    nodes = 0
    ancestors: set[int] = set()

    def visit(item: Any, depth: int) -> None:
        nonlocal size, nodes
        nodes += 1
        if nodes > limits.max_nodes:
            raise PayloadLimitError("payload_nodes")
        if depth > limits.max_depth:
            raise PayloadLimitError("payload_depth")
        if isinstance(item, str):
            size += text_bytes(item, limits.max_bytes - size, json_string=True)
        elif item is None:
            size += 4
        elif isinstance(item, bool):
            size += 4 if item else 5
        elif isinstance(item, int):
            if item.bit_length() > 4096:
                raise PayloadLimitError("number_limit")
            size += len(str(item))
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise PayloadLimitError("nonfinite_number")
            size += len(repr(item))
        elif isinstance(item, (dict, list, tuple)):
            identity = id(item)
            if identity in ancestors:
                raise PayloadLimitError("payload must not contain cycles")
            if len(item) > limits.max_nodes - nodes:
                raise PayloadLimitError("payload_nodes")
            ancestors.add(identity)
            size += 2 + max(0, len(item) - 1)
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise PayloadLimitError("payload must use string object keys")
                    visit(key, depth + 1)
                    size += 1
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            ancestors.remove(identity)
        else:
            raise PayloadLimitError("non_json_value")
        if size > limits.max_bytes:
            raise PayloadLimitError("payload_bytes")

    visit(value, 0)
    return size


def bounded_loads(value: str, limits: PayloadLimits = DEFAULT_PAYLOAD_LIMITS) -> Any:
    """Reject excessive encoded size/depth before allocating decoded objects."""
    text_bytes(value, limits.max_bytes)
    depth = 0
    quoted = False
    escaped = False
    nodes = 0
    atom = False
    for character in value:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
            nodes += 1
            atom = False
        elif character in "[{":
            nodes += 1
            atom = False
            depth += 1
            if depth > limits.max_depth:
                raise PayloadLimitError("payload_depth")
        elif character in "]}":
            depth -= 1
            atom = False
        elif character in ",:" or character.isspace():
            atom = False
        elif not atom:
            nodes += 1
            atom = True
        if nodes > limits.max_nodes:
            raise PayloadLimitError("payload_nodes")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise PayloadLimitError("duplicate_object_key")
            result[key] = item
        return result

    def constant(_value: str) -> Any:
        raise PayloadLimitError("nonfinite_number")

    try:
        decoded = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except RecursionError as exc:
        raise PayloadLimitError("payload_depth") from exc
    validate_payload(decoded, limits)
    return decoded
