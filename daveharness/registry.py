"""Tool definitions and the runtime registry."""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Collection

DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
VALID_CANCELLATION_MODES = frozenset({"bounded", "abandon"})

ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


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
        self._async_handler_allowlist = frozenset(async_handler_allowlist)

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not definition.name.strip():
            raise ValueError("Tool name is required")
        if definition.timeout_seconds <= 0:
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
        if definition.name in self._definitions:
            raise ValueError(f"Tool '{definition.name}' is already registered")
        self._definitions[definition.name] = self._snapshot(definition)

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
        )

    def revoke(self, name: str) -> bool:
        return self._definitions.pop(name, None) is not None

    def get(self, name: str) -> ToolDefinition | None:
        definition = self._definitions.get(name)
        return self._snapshot(definition) if definition is not None else None

    def model_schemas(self) -> list[dict[str, Any]]:
        return [
            self._definitions[name].model_schema()
            for name in sorted(self._definitions)
        ]

    def public_catalog(self) -> dict[str, dict[str, Any]]:
        return {
            name: self._definitions[name].public_metadata()
            for name in sorted(self._definitions)
        }


DEFAULT_TOOL_REGISTRY = ToolRegistry()
