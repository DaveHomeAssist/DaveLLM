"""DAVE_ENABLE_EXTENDED_TOOLS defaults off, needs DAVE_ENABLE_TOOLS, and adds no tools yet."""

import json
from pathlib import Path

import pytest

from daveharness import ToolDefinition, ToolRegistry


BASELINE = json.loads(
    (Path(__file__).parent / "fixtures" / "davellm" / "tool_catalog.json").read_text()
)
SHELL_TOOL = "shell.exec"


def boundary(registry):
    """Every fingerprint input except the compiled-code digest, which is path dependent."""
    catalog = {}
    for name in registry.public_catalog():
        definition = registry.get(name)
        catalog[name] = {
            "description": definition.description,
            "parameters": definition.parameters,
            "permission": definition.permission,
            "approval_required": definition.approval_required,
            "timeout_seconds": definition.timeout_seconds,
            "cancellation": definition.cancellation,
            "async_handler": definition.async_handler,
            "context_handler": definition.context_handler,
            "handler": f"{definition.handler.__module__}.{definition.handler.__qualname__}",
            "handler_version": definition.handler_version,
        }
    return catalog


def fingerprints(router):
    return {
        name: {tool: getattr(router, name).get(tool).fingerprint()
               for tool in getattr(router, name).public_catalog()}
        for name in ("TOOL_REGISTRY", "HARNESS_REGISTRY")
    }


@pytest.fixture
def load(router_factory, monkeypatch, tmp_path):
    def load_router(*, extended, tools=True, shell=False):
        if extended is None:
            monkeypatch.delenv("DAVE_ENABLE_EXTENDED_TOOLS", raising=False)
        else:
            monkeypatch.setenv("DAVE_ENABLE_EXTENDED_TOOLS", extended)
        router, client, _ = router_factory(tools=tools, tool_roots=[str(tmp_path)], shell=shell)
        return router, client

    return load_router


def expected(registry_name, *, shell):
    catalog = BASELINE[registry_name]
    return {name: value for name, value in catalog.items() if shell or name != SHELL_TOOL}


def test_extended_tools_default_off_and_parse_like_other_tool_flags(load):
    router, _ = load(extended=None)
    assert router.EXTENDED_TOOLS_ENABLED is False
    for value in ("", "false", "0", "no", "off", "enabled"):
        router, _ = load(extended=value)
        assert router.EXTENDED_TOOLS_ENABLED is False, value
    for value in ("true", "1", "yes", "on", " TRUE "):
        router, _ = load(extended=value)
        assert router.EXTENDED_TOOLS_ENABLED is True, value


def test_extended_tools_cannot_activate_without_normal_tools(load, monkeypatch):
    router, client = load(extended="true", tools=False)
    assert router.EXTENDED_TOOLS_ENABLED is False
    assert client.get("/tools", headers={"X-API-Key": "test-only-api-key"}).status_code == 403

    probe = ToolDefinition(
        "probe.extended", "Registration probe", {"type": "object", "additionalProperties": False},
        lambda _args: "ok", handler_version="probe-v1",
    )

    def registered(router_module):
        monkeypatch.setattr(router_module, "TOOL_REGISTRY", ToolRegistry(
            async_handler_allowlist=router_module.ASYNC_TOOL_HANDLER_ALLOWLIST))
        monkeypatch.setattr(router_module, "HARNESS_REGISTRY", ToolRegistry(
            async_handler_allowlist=router_module.ASYNC_TOOL_HANDLER_ALLOWLIST))
        monkeypatch.setattr(router_module, "extended_tool_definitions", lambda: [probe])
        router_module.register_builtin_tools()
        return {
            "probe.extended" in router_module.TOOL_REGISTRY.public_catalog(),
            "probe.extended" in router_module.HARNESS_REGISTRY.public_catalog(),
        }

    assert registered(router) == {False}
    router, _ = load(extended="false")
    assert registered(router) == {False}
    router, _ = load(extended="true")
    assert registered(router) == {True}


@pytest.mark.parametrize("shell", [False, True])
def test_flag_off_preserves_the_qualified_catalog_exactly(load, shell):
    unset, _ = load(extended=None, shell=shell)
    off, _ = load(extended="false", shell=shell)
    for router in (unset, off):
        for registry in ("TOOL_REGISTRY", "HARNESS_REGISTRY"):
            assert boundary(getattr(router, registry)) == expected(registry, shell=shell)
    assert sorted(unset.TOOL_REGISTRY.public_catalog()) == sorted(
        BASELINE["default_tools"] + ([SHELL_TOOL] if shell else [])
    )
    assert fingerprints(unset) == fingerprints(off)


@pytest.mark.parametrize("shell", [False, True])
def test_flag_on_adds_no_tools_in_this_release(load, shell):
    off, _ = load(extended="false", shell=shell)
    on, _ = load(extended="true", shell=shell)
    assert on.EXTENDED_TOOLS_ENABLED is True
    assert on.extended_tool_definitions() == []
    for registry in ("TOOL_REGISTRY", "HARNESS_REGISTRY"):
        assert boundary(getattr(on, registry)) == expected(registry, shell=shell)
    assert fingerprints(on) == fingerprints(off)
