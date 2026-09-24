"""Strict direct orchestration over one owned native IDE client."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Literal, Protocol, assert_never

from .json_types import JsonObject, checked_json, json_object, parse_json
from .native_types import (
    NativeBinding,
    NativeCancelled,
    NativeCompleted,
    NativeFailed,
    NativeModel,
    NativeObservation,
    NativeServerInfo,
    NativeTerminal,
    NativeUnsettledError,
    ConversationBinding,
)
from .permission_policy import PermissionPolicy

from .direct_protocol import (
    DIRECT_PROTOCOL_MAJOR,
    BRIDGE_VERSION,
    CancelRequest,
    CapabilitiesResponse,
    NativeServerIdentity,
    ToolObservation,
    CreateSessionRequest,
    DirectFeatures,
    DirectLimits,
    EvidenceAvailability,
    EffectObservation,
    PermissionObservation,
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
    InstructionsPending,
    InstructionsSubmitted,
    OperationKind,
    OperationRecord,
    OperationState,
    SessionState,
)

logger = logging.getLogger(__name__)


class NativeSessionClient(Protocol):
    """Owned native operations; successful terminals imply effect settlement."""

    @property
    def models(self) -> tuple[NativeModel, ...]: ...
    @property
    def server_info(self) -> NativeServerInfo: ...
    @property
    def is_alive(self) -> bool: ...
    def allocate_session(
        self, logical_id: str, cwd: str, model_id: str, policy: PermissionPolicy
    ) -> None: ...
    def binding(self, logical_id: str) -> NativeBinding: ...
    async def run_turn(
        self,
        logical_id: str,
        text: str,
        *,
        timeout_s: float,
        event_byte_limit: int,
        event_count_limit: int,
        response_byte_limit: int,
    ) -> NativeTerminal: ...
    async def cancel_session(self, logical_id: str) -> None: ...
    async def retire_session(self, logical_id: str) -> None: ...
    async def stop(self) -> None: ...


@dataclass
class _GenerationState:
    """Mutable resources owned together for one continuity generation."""

    generation_id: str
    ledger: DirectLedger
    sessions: dict[str, DirectSession] = field(default_factory=dict)
    prompt_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    prompt_reservations: int = 0
    released_reservations: set[str] = field(default_factory=set)
    collector_tasks: dict[str, asyncio.Task[NativeTerminal]] = field(
        default_factory=dict
    )
    execution_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    quarantined: bool = False


@dataclass(frozen=True)
class _DeferredSettlement:
    """A terminal result published only after continuity is quarantined."""

    record: OperationRecord
    state: OperationState
    result: JsonObject | None = None
    error: JsonObject | None = None


class DirectGenerationMismatch(DirectConflict):
    """The caller pinned a continuity generation that is no longer current."""


class DirectWorkspaceMismatch(DirectConflict):
    """The caller and proxy disagree about the canonical execution root."""


class DirectBusy(DirectConflict):
    """The logical session already owns an unsettled prompt."""


class DirectService:
    """Own direct protocol identity, sessions, operation ledger, and settlement."""

    def __init__(
        self,
        native_client: NativeSessionClient,
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
        self.native_client = native_client
        self.canonical_workspace = os.path.realpath(cwd)
        self.launch_secret = launch_secret
        self.limits = limits or DirectLimits()
        if execution_authority == "trusted-host":
            self.execution_authority_name: Literal[
                "trusted-host", "confined-container"
            ] = "trusted-host"
        else:
            self.execution_authority_name = "confined-container"
        self._state_lock = asyncio.Lock()
        self._generation = self._new_generation(continuity_generation_id)
        self._available = True
        self._shutdown_done = asyncio.Event()
        self._shutdown_owner: asyncio.Task[object] | None = None
        self._shutdown_error: RuntimeError | None = None

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
            native_internal_tools=(
                "container-boundary"
                if self.execution_authority_name == "confined-container"
                else "process-user"
            ),
        )
        return CapabilitiesResponse(
            bridge_version=BRIDGE_VERSION,
            continuity_generation_id=self.continuity_generation_id,
            canonical_workspace=self.canonical_workspace,
            execution_authority=authority,
            limits=self.limits,
            features=DirectFeatures(),
            model_ids=[model.id for model in self.native_client.models],
            native_server_info=NativeServerIdentity(
                name=self.native_client.server_info.name,
                version=self.native_client.server_info.version,
            ),
        )

    def _check_pin(self, protocol_major: int, generation_id: str) -> _GenerationState:
        if protocol_major != DIRECT_PROTOCOL_MAJOR:
            raise DirectGenerationMismatch(
                f"unsupported direct protocol major: {protocol_major}"
            )
        if generation_id != self.continuity_generation_id:
            raise DirectGenerationMismatch(
                "continuity generation changed; the prior operation outcome is not "
                "recoverable from this Bridge generation"
            )
        self._ensure_available()
        return self._generation

    def _ensure_available(self) -> None:
        if not self._available or not self._transport_alive():
            raise DirectGenerationMismatch(
                "native child continuity is unavailable; managed restart is required"
            )

    def _transport_alive(self) -> bool:
        return self.native_client.is_alive

    def _schedule(
        self,
        coroutine: Coroutine[object, object, None],
        generation: _GenerationState,
    ) -> asyncio.Task[None]:
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
        if (
            os.path.realpath(request.expected_canonical_workspace)
            != self.canonical_workspace
        ):
            raise DirectWorkspaceMismatch(
                "requested workspace does not match the Bridge-visible canonical workspace"
            )
        available = {model.id for model in self.native_client.models}
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
                model_id=request.model_id,
                stable_instruction_digest=request.stable_instruction_digest,
                permission_policy_digest=canonical_request_digest(
                    request.permission_policy
                ),
            )
            generation.sessions[request.logical_session_id] = session
            try:
                self.native_client.allocate_session(
                    session.logical_session_id,
                    self.canonical_workspace,
                    session.model_id,
                    PermissionPolicy.from_json(
                        request.permission_policy.model_dump(mode="json")
                    ),
                )
            except Exception as exc:
                # Allocation is explicitly local; there is no inferred backend effect.
                logger.error(
                    "Native allocation rejected: error_type=%s", type(exc).__name__
                )
                session.state = SessionState.NON_REUSABLE
                record.set_terminal(
                    OperationState.FAILED,
                    error={
                        "code": "session_allocation_failed",
                        "message": "native session allocation failed",
                    },
                )
            else:
                session.state = SessionState.READY
                record.set_terminal(
                    OperationState.COMPLETED,
                    result={
                        "logical_session_id": session.logical_session_id,
                        "backend_session_id": None,
                        "binding_state": "allocated",
                        "model_id": session.model_id,
                        "stable_instruction_digest": session.stable_instruction_digest,
                        "permission_policy_digest": session.permission_policy_digest,
                        "continuity_generation_id": generation.generation_id,
                    },
                )
            return record, True

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
                raise DirectNotFound(f"unknown session: {logical_session_id}") from exc
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
            raise DirectLimitExceeded(
                "requested execution timeout exceeds negotiated limit"
            )
        text_bytes = len(self._render_prompt(request).encode("utf-8"))
        if text_bytes > self.limits.max_prompt_bytes:
            raise DirectLimitExceeded(
                "model-facing prompt layers exceed negotiated limit"
            )

    def _validate_prompt_lifetime(
        self, session: DirectSession, request: PromptRequest
    ) -> None:
        if request.stable_instruction_digest != session.stable_instruction_digest:
            raise DirectConflict("stable instruction digest changed within the session")
        submitted = session.instructions
        if request.phase is PromptPhase.INITIAL:
            if isinstance(submitted, InstructionsSubmitted):
                raise DirectConflict("stable instructions were already submitted")
            assert request.stable_instructions is not None
            if (
                sha256_text(request.stable_instructions)
                != session.stable_instruction_digest
            ):
                raise DirectConflict(
                    "stable instruction bytes do not match their digest"
                )
        elif isinstance(submitted, InstructionsPending):
            raise DirectConflict(
                "first settled invocation must submit stable instructions"
            )
        elif request.phase is PromptPhase.INVOCATION:
            if submitted.contract_for(request.invocation_id) is not None:
                raise DirectConflict("an existing invocation requires a delta phase")
        else:
            if submitted.contract_for(request.invocation_id) is None:
                raise DirectConflict("delta phase names an unknown invocation")
            if request.invocation_id != submitted.active_invocation_id:
                raise DirectConflict(
                    "delta phase may target only the active invocation"
                )
            if (
                submitted.contract_for(request.invocation_id)
                != request.output_contract_digest
            ):
                raise DirectConflict("delta phase output contract digest changed")
        if request.output_contract is not None:
            if sha256_text(request.output_contract) != request.output_contract_digest:
                raise DirectConflict("output contract bytes do not match their digest")

    @staticmethod
    def _record_instruction_submission(
        session: DirectSession, request: PromptRequest
    ) -> None:
        submitted = session.instructions
        if request.phase is PromptPhase.INITIAL:
            session.instructions = InstructionsSubmitted(
                request.invocation_id,
                ((request.invocation_id, request.output_contract_digest),),
            )
        elif request.phase is PromptPhase.INVOCATION:
            assert isinstance(submitted, InstructionsSubmitted)
            session.instructions = InstructionsSubmitted(
                request.invocation_id,
                (
                    *submitted.contract_digests,
                    (request.invocation_id, request.output_contract_digest),
                ),
            )

    async def _execute_prompt(
        self,
        record: OperationRecord,
        session: DirectSession,
        request: PromptRequest,
        generation: _GenerationState,
    ) -> None:
        dispatched = False
        try:
            async with generation.prompt_lock:
                if record.state.terminal:
                    return
                self._require_current_generation(generation)
                record.state = OperationState.RUNNING
                dispatched = True
                collector = asyncio.create_task(
                    self.native_client.run_turn(
                        session.logical_session_id,
                        self._render_prompt(request),
                        timeout_s=request.execution_timeout_s
                        + self.limits.cancellation_grace_s
                        + 1,
                        event_byte_limit=self.limits.max_event_bytes,
                        event_count_limit=self.limits.max_event_count,
                        response_byte_limit=self.limits.max_response_bytes,
                    )
                )
                generation.collector_tasks[record.operation_id] = collector
                try:
                    terminal = await asyncio.wait_for(
                        asyncio.shield(collector), request.execution_timeout_s
                    )
                except TimeoutError:
                    await self._settle_deadline(
                        record, session, request, collector, generation
                    )
                    return
                finally:
                    if collector.done():
                        generation.collector_tasks.pop(record.operation_id, None)
                self._require_current_generation(generation)
                await self._settle_terminal(
                    record, session, request, terminal, generation
                )
        except NativeUnsettledError as exc:
            logger.error(
                "Native prompt did not settle: error_type=%s", type(exc).__name__
            )
            await self._quarantine_uncertain(
                "native prompt outcome is uncertain",
                _DeferredSettlement(
                    record,
                    OperationState.IN_DOUBT,
                    result=self._retained_evidence(
                        session, request.invocation_id, generation, exc.observation
                    ),
                    error={
                        "code": "prompt_in_doubt",
                        "message": "native prompt outcome is uncertain",
                    },
                ),
            )
        except Exception as exc:
            logger.error(
                "Native prompt outcome became uncertain: error_type=%s",
                type(exc).__name__,
            )
            await self._quarantine_uncertain(
                "native prompt outcome is uncertain",
                _DeferredSettlement(
                    record,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "prompt_in_doubt",
                        "message": "native prompt outcome is uncertain",
                    },
                ),
            )
        finally:
            async with self._state_lock:
                if session.active_operation_id == record.operation_id:
                    session.active_operation_id = None
                if generation.quarantined:
                    session.state = SessionState.LOST
                elif dispatched and record.state in {
                    OperationState.CANCELLED,
                    OperationState.TIMED_OUT,
                    OperationState.IN_DOUBT,
                }:
                    session.state = SessionState.NON_REUSABLE
                self._release_prompt_reservation(generation, record.operation_id)

    async def _settle_terminal(
        self,
        record: OperationRecord,
        session: DirectSession,
        request: PromptRequest,
        terminal: NativeTerminal,
        generation: _GenerationState,
    ) -> None:
        observation = terminal.observation
        session.active_operation_id = None
        if not observation.complete:
            session.state = SessionState.NON_REUSABLE
            record.set_terminal(
                OperationState.FAILED,
                result=self._retained_evidence(
                    session, request.invocation_id, generation, observation
                ),
                error={
                    "code": "evidence_limit",
                    "message": "native evidence exceeded a negotiated limit",
                },
            )
            return
        if isinstance(terminal, NativeFailed):
            session.state = SessionState.NON_REUSABLE
            record.set_terminal(
                OperationState.FAILED,
                result=self._retained_evidence(
                    session, request.invocation_id, generation, observation
                ),
                error={
                    "code": "native_turn_failed",
                    "message": "native turn failed after settlement",
                },
            )
        elif isinstance(terminal, NativeCancelled):
            session.state = SessionState.NON_REUSABLE
            record.set_terminal(
                OperationState.CANCELLED,
                result=self._retained_evidence(
                    session, request.invocation_id, generation, observation
                ),
            )
        elif isinstance(terminal, NativeCompleted):
            if record.state is OperationState.CANCELLING:
                await self._quarantine_uncertain(
                    "native cancellation returned a mismatched terminal",
                    _DeferredSettlement(
                        record,
                        OperationState.IN_DOUBT,
                        result=self._retained_evidence(
                            session, request.invocation_id, generation, observation
                        ),
                        error={
                            "code": "cancellation_stop_mismatch",
                            "message": "native cancellation did not settle as cancelled",
                        },
                    ),
                )
                return
            result = self._normalize_result(
                record, session, request, terminal, generation
            )
            self._record_instruction_submission(session, request)
            session.state = SessionState.READY
            record.set_terminal(
                OperationState.COMPLETED,
                result=json_object(checked_json(result.model_dump(mode="json"))),
            )
        else:
            assert_never(terminal)

    def _release_prompt_reservation(
        self, generation: _GenerationState, operation_id: str
    ) -> None:
        if operation_id in generation.released_reservations:
            return
        generation.released_reservations.add(operation_id)
        generation.prompt_reservations -= 1
        if generation.prompt_reservations < 0:
            raise RuntimeError("prompt reservation accounting underflow")

    def _render_prompt(self, request: PromptRequest) -> str:
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
        return text

    @staticmethod
    def _ordered_events(observation: NativeObservation) -> list[OrderedEvent]:
        return [
            OrderedEvent(
                sequence=index,
                update_type=event.kind,
                raw=json_object(parse_json(event.payload_json)),
            )
            for index, event in enumerate(observation.events)
        ]

    def _retained_evidence(
        self,
        session: DirectSession,
        invocation_id: str,
        generation: _GenerationState,
        observation: NativeObservation,
    ) -> JsonObject:
        return json_object(
            checked_json(
                {
                    "logical_session_id": session.logical_session_id,
                    "backend_session_id": observation.binding.conversation_id,
                    "invocation_id": invocation_id,
                    "continuity_generation_id": generation.generation_id,
                    "retained_evidence": {
                        "ordered_events": [
                            event.model_dump(mode="json")
                            for event in self._ordered_events(observation)
                        ],
                        "events_complete": observation.complete,
                        "observed_tool_call_ids": list(
                            dict.fromkeys(
                                tool.tool_call_id for tool in observation.tools
                            )
                        ),
                        "calls": [
                            {
                                "tool_call_id": tool.tool_call_id,
                                "name": tool.name,
                                "status": tool.status,
                                "scope": tool.scope,
                            }
                            for tool in observation.tools
                        ],
                        "tool_activity_complete": observation.complete,
                        "effect_evidence": "observed"
                        if observation.effects
                        else "unavailable",
                        "effects": [
                            {
                                "tool_call_id": effect.tool_call_id,
                                "receipt": parse_json(effect.result_json),
                            }
                            for effect in observation.effects
                        ],
                        "decisions": [
                            {
                                "tool_call_id": decision.tool_call_id,
                                "allowed": decision.allowed,
                                "policy_digest": decision.policy_digest,
                            }
                            for decision in observation.permissions
                        ],
                        "usage_evidence": "unavailable",
                    },
                }
            )
        )

    async def _settle_deadline(
        self,
        record: OperationRecord,
        session: DirectSession,
        request: PromptRequest,
        collector: asyncio.Task[NativeTerminal],
        generation: _GenerationState,
    ) -> None:
        record.state = OperationState.CANCELLING
        await self._send_cancel_bounded(session.logical_session_id)
        try:
            terminal = await asyncio.wait_for(
                asyncio.shield(collector), self.limits.cancellation_grace_s
            )
        except TimeoutError:
            await self._quarantine_uncertain(
                "native deadline cancellation did not settle",
                _DeferredSettlement(
                    record,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "deadline_settlement_unknown",
                        "message": "native prompt did not settle after cancellation grace",
                    },
                ),
            )
        else:
            evidence = self._retained_evidence(
                session, request.invocation_id, generation, terminal.observation
            )
            if isinstance(terminal, NativeCancelled):
                record.set_terminal(
                    OperationState.TIMED_OUT,
                    result=evidence,
                    error={"code": "execution_deadline", "message": "prompt timed out"},
                )
            else:
                await self._quarantine_uncertain(
                    "native deadline cancellation returned a mismatched terminal",
                    _DeferredSettlement(
                        record,
                        OperationState.IN_DOUBT,
                        result=evidence,
                        error={
                            "code": "deadline_stop_mismatch",
                            "message": "deadline cancellation did not settle as cancelled",
                        },
                    ),
                )
        finally:
            generation.collector_tasks.pop(record.operation_id, None)
            session.state = SessionState.NON_REUSABLE

    @staticmethod
    def _has_tool_evidence(event: OrderedEvent) -> bool:
        """Select raw diagnostics without reconstructing normalized tool state."""

        if event.update_type in {
            "native.client_tool.invocation",
            "native.client_tool.result",
        }:
            return True
        if event.update_type != "native.progress.report":
            return False
        if event.raw.get("toolCalls"):
            return True
        rounds = event.raw.get("editAgentRounds")
        return isinstance(rounds, list) and any(
            isinstance(round_value, dict) and bool(round_value.get("toolCalls"))
            for round_value in rounds
        )

    def _normalize_result(
        self,
        record: OperationRecord,
        session: DirectSession,
        request: PromptRequest,
        terminal: NativeCompleted,
        generation: _GenerationState,
    ) -> PromptResult:
        observation = terminal.observation
        binding = observation.binding
        if not isinstance(binding, ConversationBinding):
            raise RuntimeError("successful native turn has no conversation binding")
        if (
            binding.logical_session_id != session.logical_session_id
            or binding.model_id != session.model_id
        ):
            raise RuntimeError(
                "native terminal binding does not match the admitted session"
            )
        ordered = self._ordered_events(observation)
        tool_events = [event for event in ordered if self._has_tool_evidence(event)]
        permission_events = [
            event
            for event in ordered
            if event.update_type in {"native.client_tool.confirmation"}
        ]
        return PromptResult(
            logical_session_id=session.logical_session_id,
            backend_session_id=binding.conversation_id,
            invocation_id=request.invocation_id,
            operation_id=record.operation_id,
            continuity_generation_id=generation.generation_id,
            model_id=session.model_id,
            response_text=observation.response_text,
            stop_reason=terminal.stop_reason,
            events=ordered,
            tool_evidence=ToolEvidence(
                availability=EvidenceAvailability.OBSERVED,
                tool_call_ids=list(
                    dict.fromkeys(tool.tool_call_id for tool in observation.tools)
                ),
                calls=[
                    ToolObservation(
                        tool_call_id=tool.tool_call_id,
                        name=tool.name,
                        status=tool.status,
                        scope=tool.scope,
                    )
                    for tool in observation.tools
                ],
                events=tool_events,
            ),
            permission_evidence=PermissionEvidence(
                availability=EvidenceAvailability.OBSERVED,
                events=permission_events,
                decisions=[
                    PermissionObservation(
                        tool_call_id=decision.tool_call_id,
                        allowed=decision.allowed,
                        policy_digest=decision.policy_digest,
                    )
                    for decision in observation.permissions
                ],
            ),
            effect_evidence=EvidenceAvailability.OBSERVED
            if observation.effects
            else EvidenceAvailability.UNAVAILABLE,
            effect_events=[
                event
                for event in ordered
                if event.update_type == "native.client_tool.result"
            ],
            effects=[
                EffectObservation(
                    tool_call_id=effect.tool_call_id,
                    receipt=json_object(parse_json(effect.result_json)),
                )
                for effect in observation.effects
            ],
            usage=UsageEvidence(
                availability=EvidenceAvailability.UNAVAILABLE, values=None
            ),
            instruction_submission="submitted_once"
            if request.phase is PromptPhase.INITIAL
            else "not_resubmitted_same_session",
            stable_instruction_digest=session.stable_instruction_digest,
            output_contract_digest=request.output_contract_digest,
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
            self._schedule(self._execute_cancel(record, target, generation), generation)
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
            await self._send_cancel_bounded(session.logical_session_id)
            try:
                await asyncio.wait_for(
                    target.done.wait(), self.limits.cancellation_grace_s
                )
            except TimeoutError:
                session.state = SessionState.NON_REUSABLE
                await self._quarantine_uncertain(
                    "native manual cancellation did not settle",
                    _DeferredSettlement(
                        target,
                        OperationState.IN_DOUBT,
                        error={
                            "code": "cancellation_settlement_unknown",
                            "message": "native prompt did not settle after cancellation grace",
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
                "Direct native cancellation failed: error_type=%s",
                type(exc).__name__,
            )
            session.state = SessionState.NON_REUSABLE
            await self._quarantine_uncertain(
                "native cancellation transport failed",
                _DeferredSettlement(
                    target,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "cancel_transport_failed",
                        "message": "native cancellation transport failed",
                    },
                ),
                _DeferredSettlement(
                    record,
                    OperationState.FAILED,
                    error={
                        "code": "cancel_failed",
                        "message": "native cancellation failed",
                    },
                ),
            )

    async def _send_cancel_bounded(self, logical_session_id: str) -> None:
        """Bound notification drain so a non-reading child cannot hang control."""

        await asyncio.wait_for(
            self.native_client.cancel_session(logical_session_id),
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
                raise DirectConflict("cannot retire a session with active work")
            record, created = generation.ledger.admit(
                request.operation_id,
                digest,
                OperationKind.RETIRE,
                request.logical_session_id,
            )
            assert created
            session.state = SessionState.RETIRING
            session.active_operation_id = record.operation_id
            self._schedule(
                self._execute_retire(record, session, generation), generation
            )
            return record, True

    async def _execute_retire(
        self,
        record: OperationRecord,
        session: DirectSession,
        generation: _GenerationState,
    ) -> None:
        record.state = OperationState.RUNNING
        try:
            binding = self.native_client.binding(session.logical_session_id)
            await asyncio.wait_for(
                self.native_client.retire_session(session.logical_session_id),
                self.limits.session_creation_timeout_s,
            )
            self._require_current_generation(generation)
            session.state = SessionState.RETIRED
            record.set_terminal(
                OperationState.COMPLETED,
                result={
                    "logical_session_id": session.logical_session_id,
                    "backend_session_id": binding.conversation_id,
                    "backend_close": "destroyed"
                    if isinstance(binding, ConversationBinding)
                    else "not_created",
                },
            )
        except Exception as exc:
            logger.error(
                "Native retirement uncertain: error_type=%s", type(exc).__name__
            )
            await self._quarantine_uncertain(
                "native retirement did not settle",
                _DeferredSettlement(
                    record,
                    OperationState.IN_DOUBT,
                    error={
                        "code": "retirement_in_doubt",
                        "message": "native retirement did not settle",
                    },
                ),
            )
        finally:
            if session.active_operation_id == record.operation_id:
                session.active_operation_id = None

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
        expected_shutdown: bool = False,
        defer_operation_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Revoke admission immediately; publish outcomes only after owned cleanup."""
        current_task = asyncio.current_task()
        old_generation = self._generation
        collectors: dict[str, asyncio.Task[NativeTerminal]] = {}
        async with self._state_lock:
            if not self._available:
                already_closing = True
                records: tuple[OperationRecord, ...] = ()
                tasks_to_cancel: tuple[asyncio.Task[NativeTerminal | None], ...] = ()
            else:
                already_closing = False
                self._shutdown_owner = current_task
                old_generation = self._generation
                old_generation.quarantined = True
                self._available = False
                for session in old_generation.sessions.values():
                    if session.state is not SessionState.RETIRED:
                        session.state = SessionState.LOST
                records = tuple(
                    record
                    for record in old_generation.ledger.values()
                    if record.operation_id not in defer_operation_ids
                    and not record.state.terminal
                )
                tasks_to_cancel = tuple(
                    task
                    for task in (
                        *old_generation.execution_tasks,
                        *old_generation.collector_tasks.values(),
                    )
                    if task is not current_task
                )
                for task in tasks_to_cancel:
                    task.cancel()
                collectors = dict(old_generation.collector_tasks)
                old_generation.collector_tasks.clear()
                self._generation = self._new_generation()
        if already_closing:
            if current_task is not self._shutdown_owner:
                await self._shutdown_done.wait()
            if self._shutdown_error is not None:
                raise self._shutdown_error
            return
        try:
            if tasks_to_cancel:
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
            await self.native_client.stop()
        except Exception as exc:
            logger.error(
                "Native child cleanup failed: error_type=%s", type(exc).__name__
            )
            self._shutdown_error = RuntimeError("native owned cleanup did not settle")
            raise self._shutdown_error from exc
        finally:
            for record in records:
                record.set_terminal(
                    OperationState.IN_DOUBT,
                    result=self._lost_prompt_evidence(
                        record, old_generation, collectors
                    ),
                    error={
                        "code": "continuity_lost",
                        "message": "native continuity generation was lost",
                    },
                )
            self._shutdown_done.set()
        if expected_shutdown:
            logger.info("Closed native continuity generation for shutdown: %s", reason)
        else:
            logger.error("Quarantined native continuity generation: %s", reason)

    def _lost_prompt_evidence(
        self,
        record: OperationRecord,
        generation: _GenerationState,
        collectors: dict[str, asyncio.Task[NativeTerminal]],
    ) -> JsonObject | None:
        """Preserve observations even when transport closure wins coroutine scheduling."""
        if record.kind is not OperationKind.PROMPT:
            return None
        assert record.logical_session_id is not None
        assert record.invocation_id is not None
        session = generation.sessions[record.logical_session_id]
        observation: NativeObservation | None = None
        collector = collectors.get(record.operation_id)
        if collector is not None and collector.done() and not collector.cancelled():
            error = collector.exception()
            if isinstance(error, NativeUnsettledError):
                observation = error.observation
            elif error is None:
                observation = collector.result().observation
        if observation is None:
            observation = NativeObservation(
                self.native_client.binding(session.logical_session_id),
                "",
                (),
                (),
                (),
                (),
                False,
            )
        return self._retained_evidence(
            session, record.invocation_id, generation, observation
        )

    async def _quarantine_uncertain(
        self, reason: str, *settlements: _DeferredSettlement
    ) -> None:
        generation = self._generation
        # Shutdown clears ownership; keep these handles until their final evidence settles.
        collectors = dict(generation.collector_tasks)
        try:
            await self.mark_generation_lost(
                reason,
                defer_operation_ids=frozenset(
                    item.record.operation_id for item in settlements
                ),
            )
        except Exception as exc:
            # Failed cleanup preserves uncertainty and closed admission; it never becomes success.
            logger.error(
                "Native quarantine cleanup failed: error_type=%s", type(exc).__name__
            )
        finally:
            for settlement in settlements:
                if settlement.record.state.terminal:
                    continue
                result = settlement.result
                if result is None:
                    result = self._lost_prompt_evidence(
                        settlement.record, generation, collectors
                    )
                if self._shutdown_error is not None:
                    settlement.record.set_terminal(
                        OperationState.IN_DOUBT,
                        result=result,
                        error={
                            "code": "cleanup_settlement_unknown",
                            "message": "native owned cleanup did not settle",
                        },
                    )
                else:
                    settlement.record.set_terminal(
                        settlement.state,
                        result=result,
                        error=settlement.error,
                    )
