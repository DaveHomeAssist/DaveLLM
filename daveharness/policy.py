"""Host-injected, fail-closed decisions for registered tool calls."""

from __future__ import annotations

from dataclasses import dataclass, field

from .registry import ToolRegistry

KNOWN_PERMISSIONS = frozenset({
    "read", "read_system", "read_files", "write", "write_files",
    "public_network", "execute_process",
})


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: str
    reason_code: str
    fingerprint: str | None = None
    permission: str | None = None
    registration_revision: int | None = None

    def __post_init__(self) -> None:
        if self.action not in {"allow", "deny", "pause"}:
            raise ValueError("invalid policy action")


@dataclass(frozen=True, slots=True)
class RunPolicyContext:
    approved_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_permissions: frozenset[str] = field(default_factory=lambda: KNOWN_PERMISSIONS)

    def __post_init__(self) -> None:
        object.__setattr__(self, "approved_tools", frozenset(self.approved_tools))
        object.__setattr__(self, "allowed_permissions", frozenset(self.allowed_permissions))


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """The host chooses permitted classes; unknown classes never execute."""

    def evaluate(
        self,
        registry: ToolRegistry,
        name: str,
        context: RunPolicyContext,
        *,
        expected_fingerprint: str | None = None,
        expected_permission: str | None = None,
        expected_registration_revision: int | None = None,
    ) -> PolicyDecision:
        definition, revision, fingerprint = registry.get_with_revision(name)
        if definition is None:
            reason = "tool_revoked" if registry.was_registered(name) else "tool_unknown"
            return PolicyDecision("deny", reason)
        assert fingerprint is not None
        permission = definition.permission
        if expected_registration_revision is not None and revision != expected_registration_revision:
            return PolicyDecision("deny", "registration_changed", fingerprint, permission, revision)
        if expected_fingerprint is not None and fingerprint != expected_fingerprint:
            return PolicyDecision("deny", "definition_changed", fingerprint, permission, revision)
        if expected_permission is not None and permission != expected_permission:
            return PolicyDecision("deny", "permission_changed", fingerprint, permission, revision)
        if permission not in KNOWN_PERMISSIONS:
            return PolicyDecision("deny", "permission_unknown", fingerprint, permission, revision)
        if permission not in context.allowed_permissions:
            return PolicyDecision("deny", "permission_denied", fingerprint, permission, revision)
        if definition.approval_required and name not in context.approved_tools:
            return PolicyDecision("pause", "approval_required", fingerprint, permission, revision)
        reason = "approval_granted" if definition.approval_required else "policy_allowed"
        return PolicyDecision("allow", reason, fingerprint, permission, revision)
