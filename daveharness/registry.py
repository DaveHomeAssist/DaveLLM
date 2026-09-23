"""Tool definitions and the runtime registry."""

from __future__ import annotations

import copy
import functools
import hashlib
import inspect
import json
import marshal
import math
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Collection, cast

from .schema import validate_schema_definition
from .limits import validate_payload

DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
VALID_CANCELLATION_MODES = frozenset({"bounded", "abandon"})

ToolHandler = Callable[..., Any | Awaitable[Any]]


def _stable_handler_value(value: Any, *, checked: bool = False) -> Any:
    """Capture declared handler configuration without exposing it in the digest."""
    if not checked:
        validate_payload(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [_stable_handler_value(item, checked=True) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _stable_handler_value(item, checked=True) for key, item in value.items()}
    raise ValueError("Opaque handler state requires an explicit handler_version")


def _handler_provenance(handler: ToolHandler, handler_version: str) -> dict[str, Any]:
    if isinstance(handler, functools.partial):
        try:
            bound = {
                "args": _stable_handler_value(handler.args),
                "keywords": _stable_handler_value(handler.keywords),
            }
        except ValueError as exc:
            if not handler_version:
                raise ValueError("Opaque handler state requires an explicit handler_version") from exc
            bound = {"opaque_version": handler_version}
        return {
            "partial_of": _handler_provenance(cast(ToolHandler, handler.func), handler_version),
            "bound": bound,
        }
    target = handler if inspect.isfunction(handler) or inspect.ismethod(handler) else type(handler).__call__
    code = getattr(target, "__code__", None)
    if code is None and not handler_version and not inspect.isbuiltin(handler):
        raise ValueError("Handler code provenance is unavailable")
    closure = getattr(target, "__closure__", None) or ()
    try:
        state = {
            "defaults": _stable_handler_value(getattr(target, "__defaults__", None)),
            "keywords": _stable_handler_value(getattr(target, "__kwdefaults__", None)),
            "closure": [_stable_handler_value(cell.cell_contents) for cell in closure],
            "instance": _stable_handler_value(vars(handler.__self__)) if inspect.ismethod(handler)
            else _stable_handler_value(vars(handler)) if not inspect.isfunction(handler) and not inspect.isbuiltin(handler)
            else None,
        }
    except (TypeError, ValueError) as exc:
        if not handler_version:
            raise ValueError("Opaque handler state requires an explicit handler_version") from exc
        state = {"opaque_version": handler_version}
    return {
        "callable_module": getattr(handler, "__module__", type(handler).__module__),
        "callable_name": getattr(handler, "__qualname__", type(handler).__qualname__),
        "module": getattr(target, "__module__", type(target).__module__),
        "qualname": getattr(target, "__qualname__", type(target).__qualname__),
        "code_sha256": hashlib.sha256(marshal.dumps(code)).hexdigest() if code is not None else None,
        "state": state,
        "handler_version": handler_version,
    }


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Describe one runtime-loadable tool and its trust boundary."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    permission: str = "read"
    approval_required: bool = False
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    cancellation: str = "abandon"
    async_handler: bool = False
    handler_version: str = ""
    context_handler: bool = False

    def fingerprint(self) -> str:
        """Digest the declared effect boundary, including handler code provenance."""
        payload = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "permission": self.permission,
            "approval_required": self.approval_required,
            "timeout_seconds": self.timeout_seconds,
            "cancellation": self.cancellation,
            "async_handler": self.async_handler,
            "context_handler": self.context_handler,
            "handler": _handler_provenance(self.handler, self.handler_version),
        }
        validate_payload(payload)
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.parameters),
            },
        }

    def public_metadata(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "parameters": copy.deepcopy(self.parameters),
            "permission": self.permission,
            "approval_required": self.approval_required,
            "timeout_seconds": self.timeout_seconds,
            "cancellation": self.cancellation,
            "async_handler": self.async_handler,
        }


class ToolRegistry:
    """Own enabled tool definitions and support registration or revocation at runtime."""

    def __init__(
        self,
        *,
        async_handler_allowlist: Collection[str] = (),
    ) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._registered_names: set[str] = set()
        self._registration_revisions: dict[str, int] = {}
        self._fingerprints: dict[str, str] = {}
        self._lock = threading.RLock()
        self.instance_id = uuid.uuid4().hex
        self._async_handler_allowlist = frozenset(async_handler_allowlist)

    def register(self, definition: ToolDefinition) -> None:
        validate_schema_definition(definition.parameters)
        if not definition.name or not definition.name.strip():
            raise ValueError("Tool name is required")
        if isinstance(definition.timeout_seconds, bool) or not isinstance(definition.timeout_seconds, (int, float)) or not math.isfinite(definition.timeout_seconds) or definition.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be greater than zero")
        if (
            not isinstance(definition.cancellation, str)
            or definition.cancellation not in VALID_CANCELLATION_MODES
        ):
            raise ValueError(
                "Tool cancellation must be either 'bounded' or 'abandon'"
            )
        write_permission = (
            definition.permission == "write"
            or definition.permission.startswith("write_")
        )
        if (
            definition.approval_required or write_permission
        ) and definition.cancellation == "abandon":
            raise ValueError(
                "Approval-required and write tools must declare bounded cancellation"
            )
        handler_is_async = inspect.iscoroutinefunction(definition.handler)
        if handler_is_async and not definition.async_handler:
            raise ValueError("Coroutine tool handlers require async_handler=True")
        if definition.async_handler and not handler_is_async:
            raise ValueError("async_handler=True requires a coroutine function")
        if handler_is_async and definition.name not in self._async_handler_allowlist:
            raise ValueError(
                f"Coroutine tool '{definition.name}' is not in the async handler allowlist"
            )
        if definition.context_handler:
            try:
                inspect.signature(definition.handler).bind({}, object())
            except (TypeError, ValueError) as exc:
                raise ValueError("context handler must accept arguments and context") from exc
        with self._lock:
            if definition.name in self._definitions:
                raise ValueError(f"Tool '{definition.name}' is already registered")
            snapshot = self._snapshot(definition)
            fingerprint = snapshot.fingerprint()
            self._definitions[definition.name] = snapshot
            self._fingerprints[definition.name] = fingerprint
            self._registered_names.add(definition.name)
            self._registration_revisions[definition.name] = self._registration_revisions.get(definition.name, 0) + 1

    @staticmethod
    def _snapshot(definition: ToolDefinition) -> ToolDefinition:
        """Copy security-relevant definition data at the registry boundary."""
        return ToolDefinition(
            name=definition.name,
            description=definition.description,
            parameters=copy.deepcopy(definition.parameters),
            handler=definition.handler,
            permission=definition.permission,
            approval_required=definition.approval_required,
            timeout_seconds=definition.timeout_seconds,
            cancellation=definition.cancellation,
            async_handler=definition.async_handler,
            handler_version=definition.handler_version,
            context_handler=definition.context_handler,
        )

    def revoke(self, name: str) -> bool:
        with self._lock:
            removed = self._definitions.pop(name, None) is not None
            if removed:
                self._fingerprints.pop(name, None)
                self._registration_revisions[name] += 1
            return removed

    def get(self, name: str) -> ToolDefinition | None:
        definition, _revision, _fingerprint = self.get_with_revision(name)
        return definition

    def get_with_revision(self, name: str) -> tuple[ToolDefinition | None, int | None, str | None]:
        with self._lock:
            definition = self._definitions.get(name)
            revision = self._registration_revisions.get(name)
            fingerprint = self._fingerprints.get(name)
            return (self._snapshot(definition) if definition is not None else None, revision, fingerprint)

    def begin_execution(self, name: str, fingerprint: str, revision: int) -> ToolDefinition | None:
        """Atomically accept an effect before dispatch; later revocation affects new calls."""
        with self._lock:
            definition = self._definitions.get(name)
            if definition is None or self._fingerprints.get(name) != fingerprint or self._registration_revisions.get(name) != revision:
                return None
            return self._snapshot(definition)

    def was_registered(self, name: str) -> bool:
        with self._lock:
            return name in self._registered_names

    def registration_revision(self, name: str) -> int | None:
        with self._lock:
            return self._registration_revisions.get(name)

    def model_schemas(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._definitions[name].model_schema()
                for name in sorted(self._definitions)
            ]

    def public_catalog(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                name: self._definitions[name].public_metadata()
                for name in sorted(self._definitions)
            }


DEFAULT_TOOL_REGISTRY = ToolRegistry()
