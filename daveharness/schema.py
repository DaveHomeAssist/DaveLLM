"""The supported JSON Schema subset for tool arguments."""

from __future__ import annotations

from typing import Any, Mapping


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


def validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
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
                validate_json_schema(item, properties[key], f"{path}.{key}")

    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_json_schema(item, schema["items"], f"{path}[{index}]")

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
