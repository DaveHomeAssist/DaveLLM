"""The supported JSON Schema subset for tool arguments."""

from __future__ import annotations

from typing import Any, Mapping

from .limits import PayloadLimitError, validate_payload


class SchemaValidationError(ValueError):
    """Report a JSON schema mismatch without exposing an implementation traceback."""


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise SchemaValidationError(f"Unsupported schema type '{expected}'")


def _validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Validate the JSON schema subset used by the DaveLLM tool registry."""
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_matches_type(value, item) for item in expected):
            raise SchemaValidationError(
                f"{path} must match one of: {', '.join(expected)}"
            )
    elif isinstance(expected, str) and not _matches_type(value, expected):
        raise SchemaValidationError(f"{path} must be {expected}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        raise SchemaValidationError(f"{path} must be one of: {allowed}")

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise SchemaValidationError(f"{path}.{key} is required")

        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise SchemaValidationError(
                    f"{path} contains unsupported field(s): {', '.join(unexpected)}"
                )
        for key, item in value.items():
            if key in properties:
                _validate_json_schema(item, properties[key], f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate_json_schema(item, schema["items"], f"{path}[{index}]")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise SchemaValidationError(f"{path} is shorter than {minimum} characters")
        if isinstance(maximum, int) and len(value) > maximum:
            raise SchemaValidationError(f"{path} is longer than {maximum} characters")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise SchemaValidationError(f"{path} must be at least {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise SchemaValidationError(f"{path} must be at most {maximum}")


_SUPPORTED = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum", "minLength", "maxLength", "minimum", "maximum", "description", "title", "default", "examples"})
_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})


def validate_schema_definition(schema: Mapping[str, Any]) -> None:
    """Reject unsupported constraints even in branches absent from an argument."""
    try:
        validate_payload(schema)
    except PayloadLimitError as exc:
        raise SchemaValidationError(str(exc)) from exc

    def visit(node: Any) -> None:
        if not isinstance(node, dict) or set(node) - _SUPPORTED:
            raise SchemaValidationError("unsupported schema keyword or shape")
        expected = node.get("type")
        if expected is not None:
            types = expected if isinstance(expected, list) else [expected]
            if not types or any(not isinstance(item, str) or item not in _TYPES for item in types):
                raise SchemaValidationError("unsupported schema type")
        properties = node.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaValidationError("schema properties must be an object")
        for child in properties.values():
            visit(child)
        if "items" in node:
            visit(node["items"])
        if "additionalProperties" in node and type(node["additionalProperties"]) is not bool:
            raise SchemaValidationError("additionalProperties must be boolean")
        required = node.get("required", [])
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise SchemaValidationError("required must be an array of names")
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            raise SchemaValidationError("enum must be a nonempty array")
        for key in ("minLength", "maxLength"):
            if key in node and (type(node[key]) is not int or node[key] < 0):
                raise SchemaValidationError("string bounds must be nonnegative integers")
        for key in ("minimum", "maximum"):
            if key in node and (isinstance(node[key], bool) or not isinstance(node[key], (int, float))):
                raise SchemaValidationError("numeric bounds must be finite numbers")
    visit(schema)


def validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    validate_schema_definition(schema)
    try:
        validate_payload(value)
    except PayloadLimitError as exc:
        raise SchemaValidationError(str(exc)) from exc
    _validate_json_schema(value, schema, path)
