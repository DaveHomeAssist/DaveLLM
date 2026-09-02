import ast
from pathlib import Path

import daveharness
import pytest
import tool_executor


REPO = Path(__file__).resolve().parents[1]


def _definition(*, schema=None, handler=None, approval_required=True):
    return daveharness.ToolDefinition(
        name="mutate",
        description="Mutate a test value.",
        parameters=schema
        or {
            "type": "object",
            "properties": {"value": {"type": "string", "minLength": 1}},
            "required": ["value"],
            "additionalProperties": False,
        },
        handler=handler or (lambda args: args["value"]),
        permission="write",
        approval_required=approval_required,
    )


def test_public_api_and_legacy_compatibility_exports_are_stable():
    assert daveharness.__version__ == "0.1.0"
    expected = {
        "DEFAULT_ERROR_BUDGET",
        "DEFAULT_MODEL_TIMEOUT_SECONDS",
        "DEFAULT_STEP_LIMIT",
        "DEFAULT_TOOL_REGISTRY",
        "DEFAULT_TOOL_TIMEOUT_SECONDS",
        "ExecutorOutcome",
        "ModelInvoker",
        "ParsedToolCall",
        "SchemaValidationError",
        "ToolDefinition",
        "ToolExecution",
        "ToolHandler",
        "ToolRegistry",
        "__version__",
        "parse_tool_calls",
        "run_executor_loop",
        "run_tool",
        "utc_timestamp",
        "validate_json_schema",
    }
    assert set(daveharness.__all__) == expected
    assert tool_executor.__version__ == daveharness.__version__
    assert tool_executor.__all__ == daveharness.__all__
    for name in expected - {"__version__"}:
        assert getattr(tool_executor, name) is getattr(daveharness, name)


def test_registry_rejects_duplicate_names_without_replacing_security_policy():
    registry = daveharness.ToolRegistry()
    original_handler = lambda args: f"original:{args['value']}"
    registry.register(_definition(handler=original_handler))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            _definition(
                handler=lambda args: f"replacement:{args['value']}",
                approval_required=False,
            )
        )

    registered = registry.get("mutate")
    assert registered is not None
    assert registered.handler is original_handler
    assert registered.permission == "write"
    assert registered.approval_required is True


def test_registry_snapshots_schema_and_returns_defensive_definitions():
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string", "minLength": 1}},
        "required": ["value"],
        "additionalProperties": False,
    }
    original_handler = lambda args: args["value"]
    definition = _definition(schema=schema, handler=original_handler)
    registry = daveharness.ToolRegistry()
    registry.register(definition)

    schema["required"].clear()
    schema["properties"]["value"]["minLength"] = 0
    object.__setattr__(definition, "handler", lambda args: "replacement")
    object.__setattr__(definition, "permission", "read")
    object.__setattr__(definition, "approval_required", False)
    first_read = registry.get("mutate")
    assert first_read is not None
    assert first_read.parameters["required"] == ["value"]
    assert first_read.parameters["properties"]["value"]["minLength"] == 1
    assert first_read.handler is original_handler
    assert first_read.permission == "write"
    assert first_read.approval_required is True

    first_read.parameters["required"].clear()
    first_read.parameters["properties"]["value"]["minLength"] = 0
    second_read = registry.get("mutate")
    assert second_read is not None
    assert second_read.parameters["required"] == ["value"]
    assert second_read.parameters["properties"]["value"]["minLength"] == 1


def test_executor_package_has_no_davellm_runtime_dependencies():
    package_files = sorted((REPO / "daveharness").glob("*.py"))
    assert {path.name for path in package_files} == {
        "__init__.py",
        "_version.py",
        "executor.py",
    }

    forbidden_imports = {
        "app",
        "electron",
        "fastapi",
        "httpx",
        "numpy",
        "ollama",
        "os",
        "project_context",
        "pydantic",
        "requests",
        "sqlite3",
        "starlette",
        "tkinter",
    }
    source = "\n".join(path.read_text() for path in package_files)
    imported_roots = set()
    for path in package_files:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(forbidden_imports)
    for forbidden_text in (
        "DAVE_API_KEY",
        "DAVE_NODES",
        "DAVE_DATA_DIR",
        "os.getenv",
        "/api/tags",
        "/v1/chat/completions",
    ):
        assert forbidden_text not in source
