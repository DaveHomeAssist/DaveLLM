"""Opt-in DaveLLM product tools, not a change to the DaveHarness contract."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from daveharness import ToolDefinition
from daveharness.schema import validate_json_schema
from davellm_files import encode_result
from davellm_toolpack_catalog import BY_NAME, ALL_SPECS

TOOLPACK_OUTPUT_BYTES = 48 * 1024
TOOLPACK_RESPONSE_BYTES = 512 * 1024
TOOLPACK_TIMEOUT_SECONDS = 60
TOOLPACK_MAX_ARRAY = 50
TOOLPACK_MAX_RUNS = 64
TOOLPACK_MAX_REFS = 200
TOOLPACK_VERSION = "tools100.v1"
_PRIVATE_FIELDS = frozenset({"token", "access_token", "refresh_token", "authorization", "password",
                             "secret", "api_key", "url", "host", "ip", "mac", "web_url", "html_url"})
_SENSITIVE = re.compile(
    r"(?:Bearer\s+\S+|(?:sk-|secret_|ntn_|gh[pousr]_)[A-Za-z0-9_-]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16}|"
    r"(?:password|api[_-]?key|token|secret)\s*[:=]\s*[^\s,;]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----)", re.I)
_ADDRESS = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")


class ToolpackError(ValueError):
    """Only fixed model-safe messages, never provider/OS text."""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def admitted_arguments(name: str, arguments: dict[str, Any]) -> None:
    validate_json_schema(arguments, BY_NAME[name].schema)

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 16 or isinstance(value, list) and len(value) > TOOLPACK_MAX_ARRAY:
            raise ToolpackError("Input exceeds the tool budget")
        if isinstance(value, (list, dict)):
            for child in value.values() if isinstance(value, dict) else value:
                visit(child, depth + 1)
    visit(arguments)


def redact(value: Any, secrets: tuple[str, ...] = (), depth: int = 0) -> Any:
    if depth > 16:
        return "[bounded]"
    if isinstance(value, dict):
        return {str(k): "[redacted]" if str(k).casefold() in _PRIVATE_FIELDS else redact(v, secrets, depth + 1)
                for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return _ADDRESS.sub("[address]", _SENSITIVE.sub("[redacted]", value))[:16000]
    return value


def bounded_result(payload: Mapping[str, Any], secrets: tuple[str, ...] = ()) -> str:
    safe = redact(dict(payload), secrets)
    encoded = encode_result(safe)
    if len(encoded.encode()) <= TOOLPACK_OUTPUT_BYTES:
        return encoded
    # Never cut JSON in the middle or make a truncated result look complete.
    return encode_result({"truncated": True, "outcome": safe.get("outcome", "Unknown"),
                          "preview": encoded[:8000], "note": "Result exceeded the output budget"})


def fixed_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ToolpackError("Configured endpoint is invalid")
    return value.rstrip("/")


@dataclass(repr=False)
class ToolpackHost:
    router: Mapping[str, Any]
    config: dict[str, Any] = field(default_factory=dict, repr=False)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    states: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict, repr=False)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)
    notifications: OrderedDict[str, float] = field(default_factory=OrderedDict, repr=False)
    native_lock: Any = field(default_factory=threading.RLock, repr=False)
    pending_effects: set[Any] = field(default_factory=set, repr=False)

    @classmethod
    def from_environment(cls, router: Mapping[str, Any]) -> ToolpackHost:
        raw = os.getenv("DAVE_TOOLPACK_CONFIG", "{}")
        try:
            if len(raw) > 65536:
                raise ValueError
            config = json.loads(raw)
            if not isinstance(config, dict):
                raise ValueError
        except (ValueError, RecursionError):
            # Fail closed at call time without preventing the existing app from starting.
            config = {"invalid": True}
        names = ("DAVE_API_KEY", "DAVE_NOTION_TOKEN", "DAVE_GITHUB_TOKEN", "DAVE_VERCEL_TOKEN",
                 "DAVE_GOOGLE_ACCESS_TOKEN", "DAVE_HA_TOKEN", "DAVE_NTFY_TOKEN")
        config["inference_enabled"] = os.getenv("DAVE_ENABLE_TOOL_INFERENCE", "").lower() == "true"
        config["jobs_enabled"] = os.getenv("DAVE_ENABLE_TOOL_JOBS", "").lower() == "true"
        return cls(router, config, {name: os.getenv(name, "") for name in names})

    def binding(self) -> Any:
        context = self.router.get("HOST_RUN_CONTEXT")
        binding = context.get() if context is not None else None
        if binding is None or not binding.run_id:
            raise ToolpackError("This tool requires a DaveLLM lifecycle run")
        return binding

    def state(self) -> dict[str, Any]:
        run = self.binding().run_id
        if run not in self.states:
            if len(self.states) >= TOOLPACK_MAX_RUNS:
                raise ToolpackError("Tool state budget is full; finish another run first")
            self.states[run] = {"refs": {}, "effects": {}, "snapshots": {}}
        return self.states[run]

    def drop(self, run_id: str) -> None:
        self.states.pop(run_id, None)
        self.locks.pop(run_id, None)

    def effect_lock(self, namespace: str, target: str) -> asyncio.Lock:
        key = namespace + ":" + target
        if key not in self.locks:
            if len(self.locks) >= 200:
                raise ToolpackError("Destination lock budget is full")
            self.locks[key] = asyncio.Lock()
        return self.locks[key]

    def configuration_digest(self) -> str:
        settings = self.router.get("NOTION_SETTINGS")
        return digest({"configuration": self.config, "credentials": self.secrets,
                       "nodes": [{"id": node.id, "url": node.url} for node in self.router.get("NODE_CONFIGS", [])],
                       "notion": {"pages": dict(settings.pages), "token": settings.token} if settings else None})

    def configured(self, group: str, alias: str) -> Any:
        if self.config.get("invalid"):
            raise ToolpackError("DAVE_TOOLPACK_CONFIG is invalid")
        value = self.config.get(group, {}).get(alias)
        if value is None:
            raise ToolpackError("Target is not configured or allowlisted")
        if isinstance(value, dict) and "users" in value and self.binding().user_id not in value["users"]:
            raise ToolpackError("Target is not available to this run owner")
        return value

    def node(self, alias: str) -> Any:
        node = next((node for node in self.router["NODE_CONFIGS"] if node.id == alias), None)
        if node is None:
            raise ToolpackError("Node alias is not configured")
        return node

    async def request(self, method: str, url: str, *, body: Any = None,
                      headers: Mapping[str, str] | None = None, params: Any = None,
                      text_response: bool = False) -> Any:
        async def perform() -> Any:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
                async with client.stream(method, url, json=body, headers=headers, params=params) as response:
                    if not 200 <= response.status_code < 300:
                        raise ToolpackError("Provider refused the request")
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > TOOLPACK_RESPONSE_BYTES:
                            raise ToolpackError("Provider response exceeded the budget")
                    if text_response:
                        return chunks.decode("utf-8", "replace")
                    if not chunks:
                        return {}
                    try:
                        return json.loads(chunks)
                    except (ValueError, RecursionError):
                        raise ToolpackError("Provider returned malformed data") from None
        try:
            return await asyncio.wait_for(perform(), 12)
        except (httpx.HTTPError, TimeoutError):
            raise ToolpackError("Provider could not be reached within the deadline") from None


async def execute(name: str, host: ToolpackHost, configuration_digest: str, arguments: dict[str, Any]) -> Any:
    ToolResult = host.router["ToolResult"]
    try:
        if host.configuration_digest() != configuration_digest:
            raise ToolpackError("Tool configuration changed; start a fresh configured registry")
        admitted_arguments(name, arguments)
        item = BY_NAME[name]
        if item.backend == "cluster":
            from davellm_toolpack_cluster import execute_cluster
            work = execute_cluster(name, arguments, host)
        elif item.backend == "http":
            from davellm_toolpack_http import execute_http
            work = execute_http(name, arguments, host)
        elif item.backend == "notion":
            from davellm_toolpack_notion import execute_notion
            work = execute_notion(name, arguments, host)
        elif item.backend == "job":
            from davellm_toolpack_jobs import execute_job
            work = execute_job(name, arguments, host)
        else:
            from davellm_toolpack_local import execute_local
            work = asyncio.to_thread(execute_local, name, arguments, host)
        payload = await asyncio.wait_for(work, TOOLPACK_TIMEOUT_SECONDS - 2)
        return ToolResult(tool=name, status="success", result=bounded_result(payload, tuple(host.secrets.values())))
    except ToolpackError as exc:
        return ToolResult(tool=name, status="error", result="", error=str(exc))
    except Exception:
        return ToolResult(tool=name, status="error", result="", error="Tool refused or failed; no provider details exposed")


def preflight(name, host, configuration_digest, arguments, _context):
    try:
        if host.configuration_digest() != configuration_digest:
            raise ToolpackError("Tool configuration changed; start a fresh configured registry")
        admitted_arguments(name, arguments)
        host.binding()
        item = BY_NAME[name]
        if item.backend == "job":
            if host.config.get("jobs_enabled") is not True:
                raise ToolpackError("Tool jobs are disabled")
            target = host.configured("runners", arguments["runner"])
            if name not in target.get("tools", []) or host.binding().user_id not in target.get("users", []):
                raise ToolpackError("Runner is not approved for this owner and operation")
        elif item.backend == "cluster":
            if "node" in arguments:
                host.node(arguments["node"])
            if name in ("model.ask", "model.consensus", "cluster.benchmark", "eval.run", "chat.summarize", "docs.ask", "image.describe") and host.config.get("inference_enabled") is not True:
                raise ToolpackError("Tool inference is disabled")
        return None
    except ToolpackError as exc:
        return str(exc)
    except Exception:
        return "Call configuration or arguments are not available"


TOOLPACK_SELECTION_LIMIT = 200


def selected_specs(config: Mapping[str, Any]) -> tuple:
    """The specs named by the optional `enabled_tools` config key; all of them when it is absent.

    Every registered schema reaches the model on each agent step, and the full pack is
    about 11,000 tokens, so a selection keeps tool runs inside a small model's context.
    Entries are exact names or a `prefix.*` family. A malformed selection, or an entry
    that matches nothing, registers no expansion tool rather than all of them.
    """
    if config.get("invalid") or "enabled_tools" not in config:
        return ALL_SPECS
    selection = config["enabled_tools"]
    if (not isinstance(selection, list) or len(selection) > TOOLPACK_SELECTION_LIMIT
            or not all(isinstance(entry, str) and entry for entry in selection)):
        return ()
    chosen: set[str] = set()
    for entry in selection:
        if entry.endswith(".*"):
            matches = {s.name for s in ALL_SPECS if s.name.startswith(entry[:-1])}
        else:
            matches = {entry} & set(BY_NAME)
        if not matches:
            return ()
        chosen |= matches
    return tuple(s for s in ALL_SPECS if s.name in chosen)


def definitions(host: ToolpackHost) -> list[ToolDefinition]:
    configuration = host.configuration_digest()
    return [ToolDefinition(name=s.name, description=s.description, parameters=s.schema,
                           handler=partial(execute, s.name, host, configuration), permission=s.permission,
                           approval_required=s.approval, timeout_seconds=TOOLPACK_TIMEOUT_SECONDS,
                           cancellation="bounded", async_handler=True, handler_version=TOOLPACK_VERSION + ":" + configuration,
                           preflight=partial(preflight, s.name, host, configuration) if s.approval else None,
                           preflight_version=TOOLPACK_VERSION + ":" + configuration if s.approval else "")
            for s in selected_specs(host.config)]
