"""Strict direct-mode orchestration over one stateful ACP client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from .client import DIRECT_STOP_REASONS, CallbackPolicy, ModelAcknowledgementError
from .direct_protocol import (
    DIRECT_PROTOCOL_MAJOR,
    PROXY_VERSION,
    CancelRequest,
    CapabilitiesResponse,
    CreateSessionRequest,
    DirectFeatures,
    DirectLimits,
    EvidenceAvailability,
    ExecutionAuthority,
    OperationView,
    OrderedEvent,
    PermissionEvidence,
    PromptPhase,
    PromptRequest,
    PromptResult,
    RetireSessionRequest,
    ToolEvidence,
    UsageEvidence,
    canonical_request_digest,
    sha256_text,
)
from .direct_state import (
    DirectConflict,
    DirectLedger,
    DirectLimitExceeded,
    DirectNotFound,
    DirectSession,
    OperationKind,
    OperationRecord,
    OperationState,
    SessionState,
)

logger = logging.getLogger(__name__)


@dataclass
class _GenerationState:
    """All mutable ownership that must rotate as one continuity generation."""

    generation_id: str
    ledger: DirectLedger
    sessions: dict[str, DirectSession] = field(default_factory=dict)
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    prompt_reservations: int = 0
    released_reservations: set[str] = field(default_factory=set)
    collector_tasks: dict[
        str, asyncio.Task[tuple[list[dict[str, Any]], str]]
    ] = field(default_factory=dict)
    execution_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    quarantined: bool = False


@dataclass(frozen=True)
class _DeferredSettlement:
    """A terminal result published only after continuity is quarantined."""

    record: OperationRecord
    state: OperationState
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class DirectGenerationMismatch(DirectConflict):
    """The caller pinned a continuity generation that is no longer current."""


class DirectWorkspaceMismatch(DirectConflict):
    """The caller and proxy disagree about the canonical execution root."""


class DirectBusy(DirectConflict):
    """The logical session already owns an unsettled prompt."""


class EvidenceLimitExceeded(DirectLimitExceeded):
    """A response exceeded a negotiated evidence bound after dispatch."""

    def __init__(
        self,
        *,
        settled_cancelled: bool,
        retained_updates: list[dict[str, Any]],
        event_bytes: int,
        response_bytes: int,
        event_limit_exceeded: bool,
        response_limit_exceeded: bool,
    ) -> None:
        super().__init__("ACP evidence exceeded a negotiated limit")
        self.settled_cancelled = settled_cancelled
        self.retained_updates = retained_updates
        self.event_bytes = event_bytes
        self.response_bytes = response_bytes
        self.event_limit_exceeded = event_limit_exceeded
        self.response_limit_exceeded = response_limit_exceeded


class DirectService:
    """Own direct protocol identity, sessions, operation ledger, and settlement."""

    def __init__(
        self,
        acp_client: Any,
        *,
        cwd: str,
        launch_secret: str,
        execution_authority: str,
        limits: DirectLimits | None = None,
        continuity_generation_id: str | None = None,
    ) -> None:
        if len(launch_secret.encode("utf-8")) < 32:
            raise ValueError("direct launch secret must contain at least 32 bytes")
        if execution_authority not in {"trusted-host", "confined-container"}:
            raise ValueError(f"unsupported execution authority: {execution_authority}")
        if getattr(acp_client, "callback_policy", None) is not CallbackPolicy.DIRECT_DENY:
            raise ValueError(
                "Meadow direct mode requires an ACP client attested to "
                "the direct-deny callback policy"
            )
        self.acp_client = acp_client
        self.canonical_workspace = os.path.realpath(cwd)
        self.launch_secret = launch_secret
        self.limits = limits or DirectLimits()
        self.execution_authority_name = execution_authority
        self._state_lock = asyncio.Lock()
        self._generation = self._new_generation(continuity_generation_id)
        self._available = True

    def _new_generation(self, generation_id: str | None = None) -> _GenerationState:
        return _GenerationState(
            generation_id=generation_id or str(uuid.uuid4()),
            ledger=DirectLedger(self.limits.max_operations),
        )

    @property
    def continuity_generation_id(self) -> str:
        return self._generation.generation_id

    @property
    def capabilities(self) -> CapabilitiesResponse:
        self._ensure_available()
        authority = ExecutionAuthority(
            profile=self.execution_authority_name,
            acp_agent_internal_tools=(
                "container-boundary"
                if self.execution_authority_name == "confined-container"
                else "process-user"
            ),
        )
        return CapabilitiesResponse(
            proxy_version=PROXY_VERSION,
            continuity_generation_id=self.continuity_generation_id,
            canonical_workspace=self.canonical_workspace,
            execution_authority=authority,
            limits=self.limits,
            features=DirectFeatures(),
            model_ids=[model.model_id for model in self.acp_client.models],
            acp_protocol_version=self.acp_client.protocol_version,
            acp_agent_info=self.acp_client.agent_info,
            acp_agent_capabilities=self.acp_client.agent_capabilities,
        )

    def _check_pin(
        self, protocol_major: int, generation_id: str
    ) -> _GenerationState:
        if protocol_major != DIRECT_PROTOCOL_MAJOR:
            raise DirectGenerationMismatch(
                f"unsupported direct protocol major: {protocol_major}"
            )
        if generation_id != self.continuity_generation_id:
            raise DirectGenerationMismatch(
                "continuity generation changed; the prior operation outcome is not "
                "recoverable from this proxy generation"
            )
        self._ensure_available()
        return self._generation

    def _ensure_available(self) -> None:
        if not self._available or not self._transport_alive():
            raise DirectGenerationMismatch(
                "ACP child continuity is unavailable; managed restart is required"
            )

    def _transport_alive(self) -> bool:
        return bool(getattr(self.acp_client, "is_alive", True))

    def _schedule(
        self,
        coroutine: Coroutine[Any, Any, Any],
        generation: _GenerationState,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        generation.execution_tasks.add(task)
        task.add_done_callback(generation.execution_tasks.discard)
        return task

    def _require_current_generation(self, generation: _GenerationState) -> None:
        if generation is not self._generation or generation.quarantined:
            raise DirectGenerationMismatch(
                "continuity generation changed before operation admission"
            )

    async def admit_create(
        self, request: CreateSessionRequest
    ) -> tuple[OperationRecord, bool]:
        generation = self._check_pin(
            request.protocol_major, request.continuity_generation_id
        )
        if os.path.realpath(request.expected_canonical_workspace) != self.canonical_workspace:
            raise DirectWorkspaceMismatch(
                "requested workspace does not match the proxy-visible canonical workspace"
            )
        available = {model.model_id for model in self.acp_client.models}
        if request.model_id not in available:
            raise DirectConflict(
                f"requested model {request.model_id!r} is not advertised: {sorted(available)}"
            )
        digest = canonical_request_digest(request)
        async with self._state_lock:
            self._require_current_generation(generation)
            existing_operation = None
            try:
                existing_operation = generation.ledger.get(request.operation_id)
            except DirectNotFound:
                pass
            if existing_operation is None:
                if request.logical_session_id in generation.sessions:
                    raise DirectConflict(
                        f"logical session already exists: {request.logical_session_id}"
                    )
                if len(generation.sessions) >= self.limits.max_sessions:
                    raise DirectLimitExceeded(
                        "generation-long session mapping capacity is exhausted"
                    )
            record, created = generation.ledger.admit(
                request.operation_id,
                digest,
                OperationKind.CREATE,
                request.logical_session_id,
            )
            if not created:
                return record, False
            session = DirectSession(
                logical_session_id=request.logical_session_id,
                actor_ref=request.actor_ref,
                title=request.title,
                backend_session_id=None,
                model_id=request.model_id,
                stable_instruction_digest=request.stable_instruction_digest,
            )
            generation.sessions[request.logical_session_id] = session
            self._schedule(
                self._execute_create(record, session, generation), generation
            )
            return record, True

    async def _execute_create(
        self,
        record: OperationRecord,
        session: DirectSession,
        generation: _GenerationState,
    ) -> None:
        record.state = OperationState.RUNNING
        try:
            descriptor = await asyncio.wait_for(
                self.acp_client.create_session_exact(
                    self.canonical_workspace, session.model_id
                ),
                timeout=self.limits.session_creation_timeout_s,
            )
            backend_session_id = descriptor.session_id
            bound_model = descriptor.model_id
            if bound_model != session.model_id:
                raise DirectConflict(
                    f"ACP bound model {bound_model!r}, expected {session.model_id!r}"
                )
            async with self._state_lock:
                session.backend_session_id = backend_session_id
                session.state = SessionState.READY
                record.set_terminal(
                    OperationState.COMPLETED,
                    result={
                        "logical_session_id": session.logical_session_id,
                        "backend_session_id": backend_session_id,
                        "model_id": bound_model,
                        "stable_instruction_digest": session.stable_instruction_digest,
                        "continuity_generation_id": generation.generation_id,
                    },
                )
        except TimeoutError:
            logger.error("Direct ACP session creation timed out")
            async with self._state_lock:
                session.state = SessionState.NON_REUSABLE
            await self._quarantine_uncertain(
                "ACP session creation did not settle before its deadline",
                _DeferredSettlement(
                    record,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "session_creation_in_doubt",
                        "message": "ACP session creation did not settle before its deadline",
                    },
                ),
            )
        except (DirectConflict, ModelAcknowledgementError):
            logger.error("Direct ACP session configuration was rejected")
            async with self._state_lock:
                session.state = SessionState.NON_REUSABLE
                record.set_terminal(
                    OperationState.FAILED,
                    error={
                        "code": "session_configuration_failed",
                        "message": "ACP did not settle the requested session configuration",
                    },
                )
        except Exception as exc:  # noqa: BLE001 - ambiguous create must quarantine
            logger.error(
                "Direct ACP session creation became uncertain: error_type=%s",
                type(exc).__name__,
            )
            if not self._transport_alive():
                await self.mark_generation_lost("ACP transport failed during session creation")
                return
            async with self._state_lock:
                session.state = SessionState.NON_REUSABLE
            await self._quarantine_uncertain(
                "ACP session creation outcome is uncertain",
                _DeferredSettlement(
                    record,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "session_creation_in_doubt",
                        "message": "ACP session creation outcome is uncertain",
                    },
                ),
            )

    async def admit_prompt(
        self, logical_session_id: str, request: PromptRequest
    ) -> tuple[OperationRecord, bool]:
        generation = self._check_pin(
            request.protocol_major, request.continuity_generation_id
        )
        self._check_prompt_limits(request)
        digest = canonical_request_digest(request)
        async with self._state_lock:
            self._require_current_generation(generation)
            try:
                existing = generation.ledger.get(request.operation_id)
            except DirectNotFound:
                existing = None
            if existing is not None:
                record, _ = generation.ledger.admit(
                    request.operation_id,
                    digest,
                    OperationKind.PROMPT,
                    logical_session_id,
                    invocation_id=request.invocation_id,
                )
                return record, False

            try:
                candidate_session = generation.sessions[logical_session_id]
            except KeyError as exc:
                raise DirectNotFound(
                    f"unknown session: {logical_session_id}"
                ) from exc
            if (
                candidate_session.active_operation_id is not None
                or candidate_session.state is SessionState.BUSY
            ):
                raise DirectBusy(
                    f"session {logical_session_id!r} already has an active prompt"
                )
            session = self._session_for_new_work(generation, logical_session_id)
            self._validate_prompt_lifetime(session, request)
            will_queue = generation.prompt_reservations > 0
            queued_count = max(0, generation.prompt_reservations - 1)
            if will_queue and queued_count >= self.limits.max_queued_prompts:
                raise DirectLimitExceeded("prompt queue capacity is exhausted")
            record, created = generation.ledger.admit(
                request.operation_id,
                digest,
                OperationKind.PROMPT,
                logical_session_id,
                invocation_id=request.invocation_id,
            )
            if will_queue:
                record.state = OperationState.QUEUED
            generation.prompt_reservations += 1
            session.active_operation_id = request.operation_id
            session.state = SessionState.BUSY
            self._schedule(
                self._execute_prompt(record, session, request, generation), generation
            )
            return record, created

    def _session_for_new_work(
        self, generation: _GenerationState, logical_session_id: str
    ) -> DirectSession:
        try:
            session = generation.sessions[logical_session_id]
        except KeyError as exc:
            raise DirectNotFound(f"unknown session: {logical_session_id}") from exc
        if session.state is not SessionState.READY:
            raise DirectConflict(
                f"session {logical_session_id!r} is not reusable ({session.state})"
            )
        return session

    def _check_prompt_limits(self, request: PromptRequest) -> None:
        if request.execution_timeout_s > self.limits.max_execution_timeout_s:
            raise DirectLimitExceeded("requested execution timeout exceeds negotiated limit")
        text_bytes = len(self._render_blocks(request)[0]["text"].encode("utf-8"))
        if text_bytes > self.limits.max_prompt_bytes:
            raise DirectLimitExceeded("model-facing prompt layers exceed negotiated limit")

    def _validate_prompt_lifetime(
        self, session: DirectSession, request: PromptRequest
    ) -> None:
        if request.stable_instruction_digest != session.stable_instruction_digest:
            raise DirectConflict("stable instruction digest changed within the session")
        if request.phase is PromptPhase.INITIAL:
            if session.stable_submitted:
                raise DirectConflict("stable instructions were already submitted")
            assert request.stable_instructions is not None
            if sha256_text(request.stable_instructions) != session.stable_instruction_digest:
                raise DirectConflict("stable instruction bytes do not match their digest")
            assert request.output_contract is not None
            if sha256_text(request.output_contract) != request.output_contract_digest:
                raise DirectConflict("output contract bytes do not match their digest")
            if session.active_invocation_id is not None:
                raise DirectConflict("initial phase already has an invocation")
        elif request.phase is PromptPhase.INVOCATION:
            if not session.stable_submitted:
                raise DirectConflict("first settled invocation must submit stable instructions")
            if request.invocation_id in session.invocation_contract_digests:
                raise DirectConflict("an existing invocation requires a delta phase")
            assert request.output_contract is not None
            if sha256_text(request.output_contract) != request.output_contract_digest:
                raise DirectConflict("output contract bytes do not match their digest")
        else:
            if not session.stable_submitted:
                raise DirectConflict("cannot continue an uninitialized session")
            if request.invocation_id not in session.invocation_contract_digests:
                raise DirectConflict("delta phase names an unknown invocation")
            if request.invocation_id != session.active_invocation_id:
                raise DirectConflict("delta phase may target only the active invocation")
            if (
                session.invocation_contract_digests[request.invocation_id]
                != request.output_contract_digest
            ):
                raise DirectConflict("delta phase output contract digest changed")

    async def _execute_prompt(
        self,
        record: OperationRecord,
        session: DirectSession,
        request: PromptRequest,
        generation: _GenerationState,
    ) -> None:
        try:
            async with generation.prompt_lock:
                if record.state.terminal:
                    return
                if record.state is not OperationState.CANCELLING:
                    record.state = OperationState.RUNNING
                blocks = self._render_blocks(request)
                collector = asyncio.create_task(
                    self._collect_prompt(
                        session.backend_session_id or "",
                        blocks,
                        request.execution_timeout_s + self.limits.cancellation_grace_s + 1,
                    )
                )
                generation.collector_tasks[record.operation_id] = collector
                try:
                    updates, stop_reason = await asyncio.wait_for(
                        asyncio.shield(collector), request.execution_timeout_s
                    )
                except TimeoutError:
                    await self._settle_deadline(
                        record, session, collector, generation
                    )
                    return
                finally:
                    if collector.done():
                        generation.collector_tasks.pop(record.operation_id, None)

                result = self._normalize_result(
                    record, session, request, updates, stop_reason, generation
                )
                async with self._state_lock:
                    if record.state is OperationState.CANCELLING:
                        if stop_reason == "cancelled":
                            record.set_terminal(
                                OperationState.CANCELLED,
                                result=result.model_dump(mode="json"),
                            )
                        else:
                            record.set_terminal(
                                OperationState.IN_DOUBT,
                                error={
                                    "code": "cancellation_stop_mismatch",
                                    "message": "ACP cancellation did not settle as cancelled",
                                },
                            )
                        session.state = SessionState.NON_REUSABLE
                    elif stop_reason == "cancelled":
                        record.set_terminal(
                            OperationState.CANCELLED,
                            result=result.model_dump(mode="json"),
                        )
                        session.state = SessionState.NON_REUSABLE
                    else:
                        record.set_terminal(
                            OperationState.COMPLETED,
                            result=result.model_dump(mode="json"),
                        )
                        if request.phase is PromptPhase.INITIAL:
                            session.stable_submitted = True
                        if request.output_contract is not None:
                            session.invocation_contract_digests[request.invocation_id] = (
                                sha256_text(request.output_contract)
                            )
                        session.active_invocation_id = request.invocation_id
                        session.state = SessionState.READY
        except EvidenceLimitExceeded as exc:
            logger.error("Direct ACP evidence limit exceeded")
            async with self._state_lock:
                retained_evidence = self._overflow_evidence(
                    session, request, generation, exc
                )
                if exc.settled_cancelled:
                    record.set_terminal(
                        OperationState.FAILED,
                        result=retained_evidence,
                        error={
                            "code": "evidence_limit",
                            "message": "ACP evidence exceeded a negotiated limit",
                        },
                    )
                session.state = SessionState.NON_REUSABLE
            if not exc.settled_cancelled:
                await self._quarantine_uncertain(
                    "ACP evidence-limit cancellation did not settle",
                    _DeferredSettlement(
                        record,
                        OperationState.IN_DOUBT,
                        result=retained_evidence,
                        error={
                            "code": "evidence_limit_in_doubt",
                            "message": "ACP evidence limit cancellation did not settle",
                        },
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - ambiguous prompt must quarantine
            logger.error(
                "Direct ACP prompt outcome became uncertain: error_type=%s",
                type(exc).__name__,
            )
            if not self._transport_alive():
                await self.mark_generation_lost("ACP transport failed during prompt")
                return
            async with self._state_lock:
                session.state = SessionState.NON_REUSABLE
            await self._quarantine_uncertain(
                "ACP prompt outcome is uncertain",
                _DeferredSettlement(
                    record,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "prompt_in_doubt",
                        "message": "ACP prompt outcome is uncertain",
                    },
                ),
            )
        finally:
            async with self._state_lock:
                if session.active_operation_id == record.operation_id:
                    session.active_operation_id = None
                if generation.quarantined:
                    session.state = SessionState.LOST
                elif record.state in {
                    OperationState.CANCELLED,
                    OperationState.TIMED_OUT,
                    OperationState.IN_DOUBT,
                }:
                    session.state = SessionState.NON_REUSABLE
                self._release_prompt_reservation(generation, record.operation_id)

    def _release_prompt_reservation(
        self, generation: _GenerationState, operation_id: str
    ) -> None:
        if operation_id in generation.released_reservations:
            return
        generation.released_reservations.add(operation_id)
        generation.prompt_reservations -= 1
        if generation.prompt_reservations < 0:
            raise RuntimeError("prompt reservation accounting underflow")

    def _render_blocks(self, request: PromptRequest) -> list[dict[str, str]]:
        if request.phase is PromptPhase.INITIAL:
            assert request.stable_instructions is not None
            assert request.prompt is not None
            assert request.output_contract is not None
            text = (
                f"{request.stable_instructions}\n\n{request.prompt}\n\n"
                f"{request.output_contract}"
            )
        elif request.phase is PromptPhase.INVOCATION:
            assert request.prompt is not None
            assert request.output_contract is not None
            text = f"{request.prompt}\n\n{request.output_contract}"
        else:
            assert request.delta is not None
            text = request.delta
        return [{"type": "text", "text": text}]

    async def _collect_prompt(
        self, backend_session_id: str, blocks: list[dict[str, str]], timeout_s: float
    ) -> tuple[list[dict[str, Any]], str]:
        updates: list[dict[str, Any]] = []
        event_bytes = 0
        response_bytes = 0
        stop_reason: str | None = None
        event_limit_exceeded = False
        response_limit_exceeded = False
        limit_exceeded = False
        cancel_sent = False
        async for update in self.acp_client.prompt_blocks(
            backend_session_id,
            blocks,
            timeout_s=timeout_s,
            event_byte_limit=self.limits.max_event_bytes,
            event_count_limit=self.limits.max_event_count,
        ):
            if update.get("done") is True:
                candidate = update.get("stopReason")
                if not isinstance(candidate, str) or candidate not in DIRECT_STOP_REASONS:
                    raise RuntimeError(
                        "direct ACP prompt omitted a known non-empty stopReason"
                    )
                stop_reason = candidate
                break
            encoded = json.dumps(
                update, ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8")
            event_bytes += len(encoded)
            if event_bytes > self.limits.max_event_bytes:
                event_limit_exceeded = True
            retained_update = update
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    text = str(content.get("text", ""))
                    encoded_text = text.encode("utf-8")
                    remaining = max(0, self.limits.max_response_bytes - response_bytes)
                    response_bytes += len(encoded_text)
                    if response_bytes > self.limits.max_response_bytes:
                        response_limit_exceeded = True
                        prefix = self._utf8_prefix(text, remaining)
                        retained_update = {
                            **update,
                            "content": {**content, "text": prefix},
                            "retention": {
                                "boundedPrefix": True,
                                "originalUtf8Bytes": len(encoded_text),
                            },
                        }
            limit_exceeded = event_limit_exceeded or response_limit_exceeded
            if limit_exceeded and not cancel_sent:
                cancel_sent = True
                try:
                    await self._send_cancel_bounded(backend_session_id)
                except Exception:  # noqa: BLE001 - ACP boundary may raise any error
                    raise EvidenceLimitExceeded(
                        settled_cancelled=False,
                        retained_updates=updates,
                        event_bytes=event_bytes,
                        response_bytes=response_bytes,
                        event_limit_exceeded=event_limit_exceeded,
                        response_limit_exceeded=response_limit_exceeded,
                    ) from None
            if not event_limit_exceeded:
                updates.append(retained_update)
        if not stop_reason:
            raise RuntimeError("ACP prompt ended without a terminal stop reason")
        if limit_exceeded:
            raise EvidenceLimitExceeded(
                settled_cancelled=stop_reason == "cancelled",
                retained_updates=updates,
                event_bytes=event_bytes,
                response_bytes=response_bytes,
                event_limit_exceeded=event_limit_exceeded,
                response_limit_exceeded=response_limit_exceeded,
            )
        return updates, stop_reason

    @staticmethod
    def _utf8_prefix(value: str, max_bytes: int) -> str:
        if max_bytes <= 0:
            return ""
        return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")

    def _overflow_evidence(
        self,
        session: DirectSession,
        request: PromptRequest,
        generation: _GenerationState,
        error: EvidenceLimitExceeded,
    ) -> dict[str, Any]:
        """Preserve the exact bounded prefix without claiming completeness."""

        ordered = [
            OrderedEvent(
                sequence=index,
                update_type=str(update.get("sessionUpdate", "unknown")),
                raw=update,
            )
            for index, update in enumerate(error.retained_updates)
        ]
        tool_ids: list[str] = []
        for event in ordered:
            if event.update_type not in {"tool_call", "tool_call_update"}:
                continue
            candidate = event.raw.get("toolCallId") or event.raw.get("id")
            if candidate is None and isinstance(event.raw.get("toolCall"), dict):
                candidate = event.raw["toolCall"].get("toolCallId") or event.raw[
                    "toolCall"
                ].get("id")
            if candidate is not None and str(candidate) not in tool_ids:
                tool_ids.append(str(candidate))
        return {
            "logical_session_id": session.logical_session_id,
            "backend_session_id": session.backend_session_id,
            "invocation_id": request.invocation_id,
            "continuity_generation_id": generation.generation_id,
            "retained_evidence": {
                "ordered_events": [event.model_dump(mode="json") for event in ordered],
                "events_complete": not error.event_limit_exceeded,
                "observed_tool_call_ids": tool_ids,
                "tool_activity_complete": not error.event_limit_exceeded,
                "effect_evidence": EvidenceAvailability.UNAVAILABLE.value,
                "usage_evidence": EvidenceAvailability.UNAVAILABLE.value,
            },
            "overflow": {
                "event_bytes": error.event_bytes,
                "response_bytes": error.response_bytes,
                "event_limit_exceeded": error.event_limit_exceeded,
                "response_limit_exceeded": error.response_limit_exceeded,
            },
        }

    async def _settle_deadline(
        self,
        record: OperationRecord,
        session: DirectSession,
        collector: asyncio.Task[tuple[list[dict[str, Any]], str]],
        generation: _GenerationState,
    ) -> None:
        async with self._state_lock:
            record.state = OperationState.CANCELLING
        await self._send_cancel_bounded(session.backend_session_id or "")
        try:
            _updates, stop_reason = await asyncio.wait_for(
                asyncio.shield(collector), self.limits.cancellation_grace_s
            )
        except TimeoutError:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
            await self._quarantine_uncertain(
                "ACP deadline cancellation did not settle",
                _DeferredSettlement(
                    record,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "deadline_settlement_unknown",
                        "message": "ACP prompt did not settle after cancellation grace",
                    },
                ),
            )
        else:
            if stop_reason == "cancelled":
                record.set_terminal(
                    OperationState.TIMED_OUT,
                    error={"code": "execution_deadline", "message": "prompt timed out"},
                )
            else:
                await self._quarantine_uncertain(
                    "ACP deadline cancellation returned a mismatched stop reason",
                    _DeferredSettlement(
                        record,
                        OperationState.IN_DOUBT,
                        error={
                            "code": "deadline_stop_mismatch",
                            "message": "deadline cancellation did not settle as cancelled",
                        },
                    ),
                )
        finally:
            generation.collector_tasks.pop(record.operation_id, None)
            session.state = SessionState.NON_REUSABLE

    def _normalize_result(
        self,
        record: OperationRecord,
        session: DirectSession,
        request: PromptRequest,
        updates: list[dict[str, Any]],
        stop_reason: str,
        generation: _GenerationState,
    ) -> PromptResult:
        ordered = [
            OrderedEvent(
                sequence=index,
                update_type=str(update.get("sessionUpdate", "unknown")),
                raw=update,
            )
            for index, update in enumerate(updates)
        ]
        response_text = "".join(
            str(update.get("content", {}).get("text", ""))
            for update in updates
            if update.get("sessionUpdate") == "agent_message_chunk"
            and update.get("content", {}).get("type") == "text"
        )
        tool_events = [
            event
            for event in ordered
            if event.update_type in {"tool_call", "tool_call_update"}
        ]
        permission_events = [
            event
            for event in ordered
            if event.update_type
            in {"client_permission_request", "client_callback_denied"}
        ]
        tool_ids: list[str] = []
        for event in tool_events:
            raw = event.raw
            candidate = raw.get("toolCallId") or raw.get("id")
            if candidate is None and isinstance(raw.get("toolCall"), dict):
                candidate = raw["toolCall"].get("toolCallId") or raw["toolCall"].get(
                    "id"
                )
            if candidate is not None and str(candidate) not in tool_ids:
                tool_ids.append(str(candidate))
        return PromptResult(
            logical_session_id=session.logical_session_id,
            backend_session_id=session.backend_session_id or "",
            invocation_id=request.invocation_id,
            operation_id=record.operation_id,
            continuity_generation_id=generation.generation_id,
            model_id=session.model_id,
            response_text=response_text,
            acp_stop_reason=stop_reason,
            events=ordered,
            tool_evidence=ToolEvidence(
                availability=EvidenceAvailability.OBSERVED,
                tool_call_ids=tool_ids,
                events=tool_events,
            ),
            permission_evidence=PermissionEvidence(
                availability=EvidenceAvailability.OBSERVED,
                events=permission_events,
            ),
            effect_evidence=EvidenceAvailability.UNAVAILABLE,
            usage=UsageEvidence(
                # ACP v1 does not give this integration a proven normalized
                # counter shape. Raw updates remain ordered diagnostics only.
                availability=EvidenceAvailability.UNAVAILABLE,
                values=None,
            ),
            instruction_submission=(
                "submitted_once"
                if request.phase is PromptPhase.INITIAL
                else "not_resubmitted_same_session"
            ),
            stable_instruction_digest=session.stable_instruction_digest,
            output_contract_digest=(
                request.output_contract_digest
            ),
        )

    async def admit_cancel(
        self, request: CancelRequest
    ) -> tuple[OperationRecord, bool]:
        generation = self._check_pin(
            request.protocol_major, request.continuity_generation_id
        )
        digest = canonical_request_digest(request)
        async with self._state_lock:
            self._require_current_generation(generation)
            target = generation.ledger.get(request.target_operation_id)
            if target.kind is not OperationKind.PROMPT:
                raise DirectConflict("cancellation target is not a prompt operation")
            record, created = generation.ledger.admit(
                request.operation_id,
                digest,
                OperationKind.CANCEL,
                target.logical_session_id,
                target_operation_id=target.operation_id,
            )
            if not created:
                return record, False
            if target.state in {OperationState.ACCEPTED, OperationState.QUEUED}:
                session = generation.sessions[target.logical_session_id or ""]
                target.set_terminal(
                    OperationState.CANCELLED,
                    result={"cancelled_pre_dispatch": True},
                )
                session.active_operation_id = None
                session.state = SessionState.READY
                self._release_prompt_reservation(generation, target.operation_id)
                record.set_terminal(
                    OperationState.COMPLETED,
                    result={"target_state": "cancelled", "cancel_sent": False},
                )
                return record, True
            if target.state.terminal:
                record.set_terminal(
                    OperationState.COMPLETED,
                    result={"target_state": target.state.value, "cancel_sent": False},
                )
                return record, True
            target.state = OperationState.CANCELLING
            self._schedule(
                self._execute_cancel(record, target, generation), generation
            )
            return record, True

    async def _execute_cancel(
        self,
        record: OperationRecord,
        target: OperationRecord,
        generation: _GenerationState,
    ) -> None:
        record.state = OperationState.RUNNING
        session = generation.sessions[target.logical_session_id or ""]
        try:
            await self._send_cancel_bounded(session.backend_session_id or "")
            try:
                await asyncio.wait_for(
                    target.done.wait(), self.limits.cancellation_grace_s
                )
            except TimeoutError:
                collector = generation.collector_tasks.pop(
                    target.operation_id, None
                )
                if collector is not None:
                    collector.cancel()
                    await asyncio.gather(collector, return_exceptions=True)
                session.state = SessionState.NON_REUSABLE
                await self._quarantine_uncertain(
                    "ACP manual cancellation did not settle",
                    _DeferredSettlement(
                        target,
                        OperationState.IN_DOUBT,
                        error={
                            "code": "cancellation_settlement_unknown",
                            "message": "ACP prompt did not settle after cancellation grace",
                        },
                    ),
                    _DeferredSettlement(
                        record,
                        OperationState.COMPLETED,
                        result={"target_state": "in_doubt", "cancel_sent": True},
                    ),
                )
            record.set_terminal(
                OperationState.COMPLETED,
                result={"target_state": target.state.value, "cancel_sent": True},
            )
        except Exception as exc:  # noqa: BLE001 - cancel failures lose continuity
            logger.error(
                "Direct ACP cancellation failed: error_type=%s",
                type(exc).__name__,
            )
            session.state = SessionState.NON_REUSABLE
            await self._quarantine_uncertain(
                "ACP cancellation transport failed",
                _DeferredSettlement(
                    target,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "cancel_transport_failed",
                        "message": "ACP cancellation transport failed",
                    },
                ),
                _DeferredSettlement(
                    record,
                    OperationState.FAILED,
                    error={
                        "code": "cancel_failed",
                        "message": "ACP cancellation failed",
                    },
                ),
            )

    async def _send_cancel_bounded(self, backend_session_id: str) -> None:
        """Bound notification drain so a non-reading child cannot hang control."""

        await asyncio.wait_for(
            self.acp_client.cancel_session(backend_session_id),
            timeout=self.limits.cancellation_grace_s,
        )

    async def admit_retire(
        self, request: RetireSessionRequest
    ) -> tuple[OperationRecord, bool]:
        generation = self._check_pin(
            request.protocol_major, request.continuity_generation_id
        )
        digest = canonical_request_digest(request)
        async with self._state_lock:
            self._require_current_generation(generation)
            try:
                existing = generation.ledger.get(request.operation_id)
            except DirectNotFound:
                existing = None
            if existing is not None:
                record, _ = generation.ledger.admit(
                    request.operation_id,
                    digest,
                    OperationKind.RETIRE,
                    request.logical_session_id,
                )
                return record, False
            try:
                session = generation.sessions[request.logical_session_id]
            except KeyError as exc:
                raise DirectNotFound(
                    f"unknown session: {request.logical_session_id}"
                ) from exc
            if session.state is SessionState.RETIRED:
                raise DirectConflict("session is already retired")
            if session.active_operation_id is not None:
                raise DirectConflict(
                    "cannot retire a session with active work"
                )
            record, created = generation.ledger.admit(
                request.operation_id,
                digest,
                OperationKind.RETIRE,
                request.logical_session_id,
            )
            assert created
            session.state = SessionState.RETIRED
            record.set_terminal(
                OperationState.COMPLETED,
                result={
                    "logical_session_id": session.logical_session_id,
                    "backend_session_id": session.backend_session_id,
                    "backend_close": session.backend_close,
                },
            )
            return record, True

    def operation(
        self, operation_id: str, *, protocol_major: int, generation_id: str
    ) -> OperationRecord:
        generation = self._check_pin(protocol_major, generation_id)
        return generation.ledger.get(operation_id)

    @staticmethod
    def operation_view(record: OperationRecord) -> OperationView:
        return OperationView(
            operation_id=record.operation_id,
            kind=record.kind.value,
            state=record.state.value,
            logical_session_id=record.logical_session_id,
            invocation_id=record.invocation_id,
            target_operation_id=record.target_operation_id,
            result=record.result,
            error=record.error,
        )

    async def wait_for_operation(self, record: OperationRecord) -> OperationView:
        await record.done.wait()
        return self.operation_view(record)

    async def mark_generation_lost(
        self,
        reason: str,
        *,
        defer_operation_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Quarantine this generation after ACP child or proof-ledger loss."""

        tasks_to_cancel: tuple[asyncio.Task[Any], ...]
        async with self._state_lock:
            if not self._available:
                return
            old_generation = self._generation
            old_generation.quarantined = True
            for session in old_generation.sessions.values():
                if session.state is not SessionState.RETIRED:
                    session.state = SessionState.LOST
            for record in old_generation.ledger.values():
                if (
                    record.operation_id not in defer_operation_ids
                    and not record.state.terminal
                ):
                    record.set_terminal(
                        OperationState.IN_DOUBT,
                        error={
                            "code": "continuity_lost",
                            "message": "ACP continuity generation was lost",
                        },
                    )
            current_task = asyncio.current_task()
            tasks_to_cancel = tuple(
                task
                for task in {
                    *old_generation.execution_tasks,
                    *old_generation.collector_tasks.values(),
                }
                if task is not current_task
            )
            for task in tasks_to_cancel:
                task.cancel()
            old_generation.collector_tasks.clear()
            self._generation = self._new_generation()
            self._available = False

        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        logger.error("Quarantined ACP continuity generation")

    async def _quarantine_uncertain(
        self,
        reason: str,
        *settlements: _DeferredSettlement,
    ) -> None:
        """Quarantine proof state and abort the owned child after uncertainty."""

        await self.mark_generation_lost(
            reason,
            defer_operation_ids=frozenset(
                settlement.record.operation_id for settlement in settlements
            ),
        )
        abort = getattr(self.acp_client, "abort", None)
        try:
            if abort is not None:
                await abort()
        finally:
            for settlement in settlements:
                settlement.record.set_terminal(
                    settlement.state,
                    result=settlement.result,
                    error=settlement.error,
                )
