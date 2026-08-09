"""Pure in-memory state and replay-protection ledger for direct mode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class DirectStateError(RuntimeError):
    """Base class for deterministic pre-effect direct protocol failures."""


class DirectConflict(DirectStateError):
    """An identity was reused for a different operation or invalid state."""


class DirectNotFound(DirectStateError):
    """A requested operation or session is unknown in this generation."""


class DirectLimitExceeded(DirectStateError):
    """A generation-long bound is exhausted; nothing may be evicted."""


class OperationKind(StrEnum):
    CREATE = "create_session"
    PROMPT = "prompt"
    CANCEL = "cancel_request"
    RETIRE = "retire_session"


class OperationState(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    IN_DOUBT = "in_doubt"

    @property
    def terminal(self) -> bool:
        return self in {
            OperationState.COMPLETED,
            OperationState.FAILED,
            OperationState.CANCELLED,
            OperationState.TIMED_OUT,
            OperationState.IN_DOUBT,
        }


class SessionState(StrEnum):
    CREATING = "creating"
    READY = "ready"
    BUSY = "busy"
    NON_REUSABLE = "non_reusable"
    LOST = "lost"
    RETIRED = "retired"


@dataclass
class OperationRecord:
    operation_id: str
    digest: str
    kind: OperationKind
    logical_session_id: str | None
    invocation_id: str | None = None
    target_operation_id: str | None = None
    state: OperationState = OperationState.ACCEPTED
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def set_terminal(
        self,
        state: OperationState,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if not state.terminal:
            raise ValueError(f"not a terminal operation state: {state}")
        if self.state.terminal:
            return
        self.state = state
        self.result = result
        self.error = error
        self.done.set()


@dataclass
class DirectSession:
    logical_session_id: str
    actor_ref: str
    title: str
    backend_session_id: str | None
    model_id: str
    stable_instruction_digest: str
    state: SessionState = SessionState.CREATING
    active_operation_id: str | None = None
    stable_submitted: bool = False
    active_invocation_id: str | None = None
    invocation_contract_digests: dict[str, str] = field(default_factory=dict)
    backend_close: str = "unsupported"


class DirectLedger:
    """Generation-long at-most-once operation ledger with no eviction."""

    def __init__(self, max_operations: int) -> None:
        if max_operations < 1:
            raise ValueError("max_operations must be positive")
        self._max_operations = max_operations
        self._records: dict[str, OperationRecord] = {}

    def admit(
        self,
        operation_id: str,
        digest: str,
        kind: OperationKind,
        logical_session_id: str | None,
        *,
        invocation_id: str | None = None,
        target_operation_id: str | None = None,
    ) -> tuple[OperationRecord, bool]:
        existing = self._records.get(operation_id)
        if existing is not None:
            identity = (
                existing.digest,
                existing.kind,
                existing.logical_session_id,
                existing.invocation_id,
                existing.target_operation_id,
            )
            candidate = (
                digest,
                kind,
                logical_session_id,
                invocation_id,
                target_operation_id,
            )
            if identity != candidate:
                raise DirectConflict(
                    f"operation ID {operation_id!r} was reused with different content"
                )
            return existing, False
        if len(self._records) >= self._max_operations:
            raise DirectLimitExceeded("operation ledger capacity is exhausted")
        record = OperationRecord(
            operation_id=operation_id,
            digest=digest,
            kind=kind,
            logical_session_id=logical_session_id,
            invocation_id=invocation_id,
            target_operation_id=target_operation_id,
        )
        self._records[operation_id] = record
        return record, True

    def get(self, operation_id: str) -> OperationRecord:
        try:
            return self._records[operation_id]
        except KeyError as exc:
            raise DirectNotFound(f"unknown operation: {operation_id}") from exc

    def values(self) -> tuple[OperationRecord, ...]:
        return tuple(self._records.values())
