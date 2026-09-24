"""Noninteractive confirmation decisions, separate from effect execution."""

from __future__ import annotations

from dataclasses import dataclass

from .json_types import JsonObject
from .permission_policy import PermissionAction, PermissionPolicy


@dataclass(frozen=True)
class PermissionDecision:
    """A policy evaluation, never a reusable execution authorization token."""

    action: PermissionAction
    policy_digest: str
    allowed: bool

    def as_json(self) -> JsonObject:
        """Return truthful policy evidence without native UI state."""
        return {
            "action": self.action.value,
            "policy_digest": self.policy_digest,
            "allowed": self.allowed,
        }


class PermissionHandler:
    """Apply session policy equally to optional confirmation and invocation."""

    def __init__(self, policy: PermissionPolicy) -> None:
        self._policy = policy

    def confirm(self, action: PermissionAction) -> PermissionDecision:
        """Evaluate an optional native confirmation without retaining state."""
        return PermissionDecision(
            action, self._policy.digest, self._policy.permits(action)
        )

    def authorize(self, action: PermissionAction) -> PermissionDecision:
        """Reevaluate actual execution independently of earlier confirmations."""
        decision = self.confirm(action)
        if not decision.allowed:
            raise PermissionError("the session policy denies this effect")
        return decision
