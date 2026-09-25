#!/usr/bin/env python3
"""Generate the DaveHarness capabilities manifest from DaveLLM's live tool registry.

Writes docs/DAVEHARNESS_CAPABILITIES.json (machine-readable) and
docs/DAVEHARNESS_CAPABILITIES.md (the same data as tables). Every value comes
from the code: the tool definitions DaveLLM registers under each tool flag, the
lifecycle run request and budget defaults, the host limits, the /tools routes
and their authentication, and the public constants of DaveHarness and the
extended tool modules. Only the flag descriptions are written by hand.

Definition fingerprints are left out on purpose: they hash compiled handler
code, which embeds the checkout path and the Python version.
tests/fixtures/davellm/tool_catalog.json pins their portable inputs instead.

    python scripts/generate_capabilities_manifest.py           # rewrite both files
    python scripts/generate_capabilities_manifest.py --check   # exit 1 if either is stale
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import importlib
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator, Literal, get_args, get_origin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from daveharness import RunBudget, __version__  # noqa: E402
from daveharness.limits import PayloadLimits  # noqa: E402

MANIFEST_VERSION = 1
JSON_NAME = "DAVEHARNESS_CAPABILITIES.json"
MARKDOWN_NAME = "DAVEHARNESS_CAPABILITIES.md"
COMMAND = "python scripts/generate_capabilities_manifest.py"
TOOL_MODULES = ("davellm_shell", "davellm_files", "davellm_markdown", "davellm_git", "davellm_native_tools")
FLAGS = {
    "DAVE_ENABLE_TOOLS": "Turns on tool execution and the core tools.",
    "DAVE_ENABLE_SHELL_TOOL": "Registers shell.exec. Execution still needs DAVE_ENABLE_TOOLS.",
    "DAVE_ENABLE_EXTENDED_TOOLS": "Registers the extended read-only tools. Honored only with DAVE_ENABLE_TOOLS.",
    "DAVE_TOOL_ROOTS": "JSON array of absolute folders that file and Git tools may use.",
}
# DAVE_ENABLE_TOOLS alone gives the core tools. Each optional flag is then turned
# on by itself, so a tool lists only the flags it needs; turning every flag on
# gives the full catalog.
BASE_FLAG = "DAVE_ENABLE_TOOLS"
OPTIONAL_FLAGS = ("DAVE_ENABLE_SHELL_TOOL", "DAVE_ENABLE_EXTENDED_TOOLS")
ALL_FLAGS = "all"
HOST_LIMITS = (
    "MAX_ACTIVE_HARNESS_RUNS", "MAX_HARNESS_INPUT_BYTES", "MAX_HARNESS_MODEL_RESPONSE_BYTES",
    "HARNESS_STORE_HEADROOM_BYTES",
)
PUBLIC_CONSTANT = re.compile(r"[A-Z][A-Z0-9_]*")
_SKIP = object()


@contextlib.contextmanager
def isolated_environment() -> Iterator[Path]:
    """A scratch data directory and tool root; the caller's environment and app module come back after."""
    saved_environment = dict(os.environ)
    saved_app = sys.modules.get("app")
    try:
        with tempfile.TemporaryDirectory(prefix="davellm-manifest-") as sandbox:
            yield Path(sandbox)
    finally:
        os.environ.clear()
        os.environ.update(saved_environment)
        sys.modules.pop("app", None)
        if saved_app is not None:
            sys.modules["app"] = saved_app


def load_router(sandbox: Path, name: str, flags: dict[str, str]) -> Any:
    """Import DaveLLM with only ``flags`` set; no operator key, node, or data is read."""
    for variable in [variable for variable in os.environ if variable.startswith("DAVE_")]:
        del os.environ[variable]
    data, root = sandbox / name / "data", sandbox / name / "root"
    data.mkdir(parents=True)
    root.mkdir()
    os.environ.update({"DAVE_DATA_DIR": str(data), "DAVE_NODES": "[]", "DAVE_API_KEY": secrets.token_hex(16)})
    if flags:
        os.environ["DAVE_TOOL_ROOTS"] = json.dumps([str(root)])
    os.environ.update(flags)
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def _plain(value: Any) -> Any:
    """JSON-ready copy of a constant, or _SKIP for anything platform-dependent or opaque."""
    if isinstance(value, bool):
        return _SKIP  # feature probes such as DESCRIPTOR_WALK differ by platform
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (tuple, list, frozenset, set)):
        items = [_plain(item) for item in value]
        if any(item is _SKIP for item in items):
            return _SKIP
        return sorted(items) if isinstance(value, (frozenset, set)) else items
    return _SKIP


def public_constants(module_name: str) -> dict[str, Any]:
    """Every public upper-case constant a module defines itself (not ones it imports)."""
    module = importlib.import_module(module_name)
    tree = ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))
    constants = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and PUBLIC_CONSTANT.fullmatch(target.id):
                value = _plain(getattr(module, target.id))
                if value is not _SKIP:
                    constants[target.id] = value
    return dict(sorted(constants.items()))


def request_fields(model: Any) -> dict[str, Any]:
    """Required flag, default, and declared bounds of each field of a request model."""
    fields = {}
    for name, field in model.model_fields.items():
        entry: dict[str, Any] = {"required": field.is_required()}
        if not field.is_required() and field.default_factory is None:
            entry["default"] = field.default
        if get_origin(field.annotation) is Literal:
            entry["enum"] = list(get_args(field.annotation))
        for item in field.metadata:
            for key in ("ge", "gt", "le", "lt", "min_length", "max_length", "pattern"):
                if getattr(item, key, None) is not None:
                    entry[key] = getattr(item, key)
        fields[name] = entry
    return fields


def _depends_on(dependant: Any, target: Any) -> bool:
    return any(dependency.call is target or _depends_on(dependency, target) for dependency in dependant.dependencies)


def tool_routes(router: Any) -> list[dict[str, Any]]:
    routes = []
    for route in router.app.routes:
        path = getattr(route, "path", "")
        if path == "/tools" or path.startswith("/tools/"):
            routes.append({
                "path": path,
                "methods": sorted(route.methods),
                "endpoint": route.endpoint.__name__,
                "authentication": "X-API-Key" if _depends_on(route.dependant, router.require_api_key) else "none",
            })
    return sorted(routes, key=lambda route: (route["path"], route["methods"]))


def tool_entries(routers: dict[str, Any]) -> dict[str, Any]:
    full = routers[ALL_FLAGS]
    catalog = full.HARNESS_REGISTRY.public_catalog()
    if catalog != full.TOOL_REGISTRY.public_catalog():
        raise RuntimeError("TOOL_REGISTRY and HARNESS_REGISTRY publish different catalogs")
    registered = {flag: set(routers[flag].HARNESS_REGISTRY.public_catalog()) for flag in (BASE_FLAG, *OPTIONAL_FLAGS)}
    tools = {}
    for name, metadata in catalog.items():
        adders = [flag for flag in OPTIONAL_FLAGS if name in registered[flag]]
        if name not in registered[BASE_FLAG] and len(adders) != 1:
            raise RuntimeError(f"{name} is not registered by exactly one optional flag")
        tools[name] = {
            "family": name.split(".", 1)[0],
            "requires_flags": [BASE_FLAG] if name in registered[BASE_FLAG] else [BASE_FLAG, *adders],
            "context_handler": full.HARNESS_REGISTRY.get(name).context_handler,
            **metadata,
        }
    return tools


def build_manifest() -> dict[str, Any]:
    with isolated_environment() as sandbox:
        defaults = load_router(sandbox, "defaults", {})
        flag_defaults = {
            "DAVE_ENABLE_TOOLS": defaults.TOOLS_ENABLED,
            "DAVE_ENABLE_SHELL_TOOL": defaults.SHELL_TOOL_ENABLED,
            "DAVE_ENABLE_EXTENDED_TOOLS": defaults.EXTENDED_TOOLS_ENABLED,
            "DAVE_TOOL_ROOTS": [str(root) for root in defaults.TOOL_ROOTS],
        }
        base = {BASE_FLAG: "true"}
        routers = {
            BASE_FLAG: load_router(sandbox, BASE_FLAG.lower(), base),
            **{flag: load_router(sandbox, flag.lower(), {**base, flag: "true"}) for flag in OPTIONAL_FLAGS},
            ALL_FLAGS: load_router(sandbox, ALL_FLAGS, {**base, **{flag: "true" for flag in OPTIONAL_FLAGS}}),
        }
        full = routers[ALL_FLAGS]
        tools = tool_entries(routers)
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "generated_by": "scripts/generate_capabilities_manifest.py",
            "versions": {
                "davellm": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
                "daveharness": __version__,
            },
            "flags": {
                flag: {
                    "description": description,
                    "default": flag_defaults[flag],
                    "tools": sorted(name for name, tool in tools.items() if tool["requires_flags"][-1] == flag),
                }
                for flag, description in FLAGS.items()
            },
            "tools": tools,
            "runs": {
                "lifecycle_request": request_fields(full.LifecycleRunRequest),
                "decision_request": request_fields(full.LifecycleDecisionRequest),
                "budget": dataclasses.asdict(RunBudget()),
                "legacy_budget": dataclasses.asdict(RunBudget.legacy()),
                "payload_limits": dataclasses.asdict(PayloadLimits()),
            },
            "host": {
                **{name.lower(): getattr(full, name) for name in HOST_LIMITS},
                "harness_store": {
                    "max_runs": full.HARNESS_STORE.max_runs,
                    "max_bytes": full.HARNESS_STORE.max_bytes,
                    "ttl_seconds": full.HARNESS_STORE.ttl_seconds,
                },
                "async_handler_allowlist": sorted(full.ASYNC_TOOL_HANDLER_ALLOWLIST),
            },
            "routes": tool_routes(full),
            "constants": {},
        }
        harness_modules = sorted(f"daveharness.{path.stem}" for path in (ROOT / "daveharness").glob("*.py")
                                 if not path.stem.startswith("_"))
        for module in (*harness_modules, *TOOL_MODULES):
            constants = public_constants(module)
            if constants:
                manifest["constants"][module] = constants
        rendered = render_json(manifest)
        if str(sandbox) in rendered or str(ROOT) in rendered:
            raise RuntimeError("The manifest captured a local path")
    return manifest


def render_json(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def _code(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return "`" + text.replace("|", "\\|") + "`"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines + [""]


def _type(schema: dict[str, Any]) -> str:
    kind = schema.get("type", "any")
    return " or ".join(kind) if isinstance(kind, list) else str(kind)


def _constraints(schema: dict[str, Any], skip: tuple[str, ...]) -> str:
    return ", ".join(f"{key} {_code(value)}" for key, value in sorted(schema.items()) if key not in skip) or "—"


def render_markdown(manifest: dict[str, Any]) -> str:
    tools = manifest["tools"]
    versions = manifest["versions"]
    by_permission: dict[str, list[str]] = {}
    for name, tool in tools.items():
        by_permission.setdefault(tool["permission"], []).append(name)
    approval = [name for name, tool in tools.items() if tool["approval_required"]]
    closed = all(tool["parameters"].get("additionalProperties") is False for tool in tools.values())
    lines = [
        "# DaveHarness capabilities manifest",
        "",
        f"<!-- Generated by {manifest['generated_by']}. Do not edit by hand. -->",
        "",
        f"Generated from DaveLLM's tool registry by `{manifest['generated_by']}`. The same data is in "
        f"[{JSON_NAME}]({JSON_NAME}). Do not edit either file: change the code, then run `{COMMAND}`. "
        "Tests fail when the committed files are stale.",
        "",
        f"DaveLLM `{versions['davellm']}` · DaveHarness `{versions['daveharness']}` · "
        f"manifest version {manifest['manifest_version']}",
        "",
        "## Summary",
        "",
        f"- {len(tools)} tools with every flag on: "
        + ", ".join(f"{len(flag['tools'])} from `{name}`" for name, flag in manifest["flags"].items() if flag["tools"])
        + ".",
        f"- Exact-call approval required: {', '.join(_code(name) for name in approval) or 'none'}.",
        "- Tools by permission: "
        + "; ".join(f"`{permission}` {len(names)}" for permission, names in sorted(by_permission.items()))
        + ".",
        *(["- Every tool schema rejects unknown arguments (`additionalProperties: false`)."] if closed else []),
        "- Definition fingerprints are not listed: they depend on the checkout path and Python version. "
        "`tests/fixtures/davellm/tool_catalog.json` pins their portable inputs.",
        "",
        "## Flags",
        "",
    ]
    lines += _table(["Flag", "Default", "Tools", "Description"], [
        [f"`{name}`", _code(flag["default"]), ", ".join(_code(tool) for tool in flag["tools"]) or "—", _cell(flag["description"])]
        for name, flag in manifest["flags"].items()
    ])
    lines += ["## Tools", ""]
    lines += _table(["Tool", "Enabled by", "Permission", "Approval", "Timeout (s)", "Cancellation", "Handler", "Arguments"], [
        [
            f"[`{name}`](#{name.replace('.', '')})",
            f"`{tool['requires_flags'][-1]}`",
            f"`{tool['permission']}`",
            "exact call" if tool["approval_required"] else "—",
            _code(tool["timeout_seconds"]),
            f"`{tool['cancellation']}`",
            ("async" if tool["async_handler"] else "sync") + (", run context" if tool["context_handler"] else ""),
            ", ".join(
                f"**`{argument}`**" if argument in tool["parameters"].get("required", []) else f"`{argument}`"
                for argument in tool["parameters"].get("properties", {})
            ) or "—",
        ]
        for name, tool in tools.items()
    ])
    lines += ["Required arguments are in bold. Every tool also needs `DAVE_ENABLE_TOOLS` to run.", ""]
    for name, tool in tools.items():
        parameters = tool["parameters"]
        lines += [f"### {name}", "", _cell(tool["description"]), ""]
        properties = parameters.get("properties", {})
        if properties:
            lines += _table(["Argument", "Type", "Required", "Constraints", "Description"], [
                [
                    f"`{argument}`", _type(schema), "yes" if argument in parameters.get("required", []) else "no",
                    _constraints(schema, ("type", "description")), _cell(schema.get("description", "")) or "—",
                ]
                for argument, schema in properties.items()
            ])
        else:
            lines += ["No arguments.", ""]
        extra = _constraints(parameters, ("type", "properties", "required") + (("additionalProperties",) if closed else ()))
        if extra != "—":
            lines += [f"Schema: {extra}.", ""]
    runs = manifest["runs"]
    lines += [
        "## Runs", "", "### Lifecycle run request", "",
        "Body of `POST /tools/agent/runs`. Validators in `app.py` add checks not listed here, "
        "such as the number and size of `messages`.", "",
    ]
    lines += _request_table(runs["lifecycle_request"])
    lines += ["### Decision request", "", "Body of `POST /tools/agent/runs/{run_id}/decisions`.", ""]
    lines += _request_table(runs["decision_request"])
    lines += [
        "### Run budget", "",
        "Lifecycle runs start from these defaults, with `model_steps` and `errors` taken from the request's "
        "`step_limit` and `error_budget`. The legacy `/tools/agent/run` and `/tools/agent/resume` routes keep "
        "their original step and error limits only. `null` means no limit.", "",
    ]
    lines += _table(["Limit", "Lifecycle runs", "Legacy routes"], [
        [f"`{key}`", _code(value), _code(runs["legacy_budget"][key])] for key, value in runs["budget"].items()
    ])
    lines += ["### Payload limits", "", "DaveHarness's default ceiling for any JSON payload it admits.", ""]
    lines += _table(["Limit", "Value"], [[f"`{key}`", _code(value)] for key, value in runs["payload_limits"].items()])
    host = manifest["host"]
    lines += ["## Host limits", ""]
    lines += _table(["Limit", "Value"], [
        *[[f"`{key}`", _code(value)] for key, value in host.items() if key != "harness_store"],
        *[[f"`harness_store.{key}`", _code(value)] for key, value in host["harness_store"].items()],
    ])
    lines += ["## Routes", ""]
    lines += _table(["Method", "Path", "Authentication", "Endpoint"], [
        [", ".join(route["methods"]), f"`{route['path']}`", route["authentication"], f"`{route['endpoint']}`"]
        for route in manifest["routes"]
    ])
    lines += ["## Constants", "", "Public constants each module defines, including the fixed refusal messages.", ""]
    for module, constants in manifest["constants"].items():
        lines += [f"### {module}", ""]
        lines += _table(["Name", "Value"], [[f"`{name}`", _code(value)] for name, value in constants.items()])
    return "\n".join(lines).rstrip("\n") + "\n"


def _request_table(fields: dict[str, Any]) -> list[str]:
    return _table(["Field", "Required", "Default", "Constraints"], [
        [
            f"`{name}`", "yes" if field["required"] else "no",
            _code(field["default"]) if "default" in field else "—",
            _constraints(field, ("required", "default")),
        ]
        for name, field in fields.items()
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="exit 1 if the committed files differ from the code")
    parser.add_argument("--docs-dir", type=Path, default=ROOT / "docs", help="write or check the files in this folder instead of docs/")
    arguments = parser.parse_args(argv)
    manifest = build_manifest()
    outputs = {
        arguments.docs_dir / JSON_NAME: render_json(manifest),
        arguments.docs_dir / MARKDOWN_NAME: render_markdown(manifest),
    }
    if arguments.check:
        stale = [path for path, text in outputs.items()
                 if not path.is_file() or path.read_text(encoding="utf-8") != text]
        for path in stale:
            print(f"{path.name} is out of date; run: {COMMAND}", file=sys.stderr)
        if not stale:
            print("Capabilities manifest is current.")
        return 1 if stale else 0
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
        print(f"Wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
