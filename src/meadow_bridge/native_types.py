"""Immutable native observations; protocol termination is not effect settlement."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


@dataclass(frozen=True)
class NativeModel:
    """An advertised native catalog entry; not provider execution attestation."""
    id: str
    name: str


@dataclass(frozen=True)
class NativeServerInfo:
    """Identity reported by the initialized language server."""
    name: str
    version: str


@dataclass(frozen=True)
class AllocatedBinding:
    """Local admission without a native conversation or model call."""
    logical_session_id: str
    model_id: str

    @property
    def conversation_id(self) -> None:
        """Allocation has no backend identity yet."""
        return None

    @property
    def turn_id(self) -> None:
        """Allocation has no backend turn yet."""
        return None


@dataclass(frozen=True)
class ConversationBinding:
    """Native identity observed at begin, including subsequently failed turns."""
    logical_session_id: str
    model_id: str
    conversation_id: str
    turn_id: str


NativeBinding: TypeAlias = AllocatedBinding | ConversationBinding


@dataclass(frozen=True)
class NativeEvent:
    """Ordered, immutable raw observation with its truthful native meaning."""
    kind: str
    payload_json: str


@dataclass(frozen=True)
class NativeToolObservation:
    """One observed native tool identity, including server-owned tools."""
    tool_call_id: str
    name: str
    status: str
    scope: Literal["server", "bridge"]


@dataclass(frozen=True)
class NativePermissionObservation:
    """A Bridge decision for one correlated confirmation callback."""
    tool_call_id: str
    allowed: bool
    policy_digest: str


@dataclass(frozen=True)
class NativeEffectObservation:
    """Executor receipts for one Bridge invocation, not global effect coverage."""
    tool_call_id: str
    result_json: str


@dataclass(frozen=True)
class NativeObservation:
    """Bounded evidence retained even when generation continuity is lost."""
    binding: NativeBinding
    response_text: str
    events: tuple[NativeEvent, ...]
    tools: tuple[NativeToolObservation, ...]
    permissions: tuple[NativePermissionObservation, ...]
    effects: tuple[NativeEffectObservation, ...]
    complete: bool


@dataclass(frozen=True)
class NativeCompleted:
    """Successful native end, matching RPC, and settled callbacks/effects."""
    observation: NativeObservation
    stop_reason: Literal["completed"] = "completed"


@dataclass(frozen=True)
class NativeCancelled:
    """Observed native cancellation after all owned effects settle."""
    observation: NativeObservation
    reason: str
    stop_reason: Literal["cancelled"] = "cancelled"


@dataclass(frozen=True)
class NativeFailed:
    """Explicit native failure with known protocol/effect settlement."""
    observation: NativeObservation
    reason: str
    stop_reason: Literal["failed"] = "failed"


NativeTerminal: TypeAlias = NativeCompleted | NativeCancelled | NativeFailed


class NativeUnsettledError(RuntimeError):
    """The requested turn has no trustworthy native terminal outcome."""

    def __init__(self, reason: str, observation: NativeObservation) -> None:
        super().__init__(reason)
        self.reason = reason
        self.observation = observation
