"""Versioned HTTP vocabulary for Meadow's strict direct ACP integration.

This module contains wire shapes only.  ACP method names and transport details
remain below the HTTP service boundary.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__

DIRECT_PROTOCOL_ID = "meadow-acp-direct"
DIRECT_PROTOCOL_MAJOR = 1
PROXY_VERSION = __version__

PATH_SAFE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._~-]*$"
Identifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        pattern=PATH_SAFE_IDENTIFIER_PATTERN,
    ),
]
Sha256Digest = str


class StrictModel(BaseModel):
    """Base model for direct traffic: extension fields are never ignored."""

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def reject_ambiguous_protocol_major(cls, value: Any) -> Any:
        """Reject Python bool/float aliases for the JSON integer major."""

        if (
            isinstance(value, dict)
            and "protocol_major" in value
            and type(value["protocol_major"]) is not int
        ):
            raise ValueError("protocol_major must be an exact integer")
        return value


class DirectLimits(StrictModel):
    """Negotiated admission and evidence limits for one proxy generation."""

    max_request_bytes: int = Field(default=1_000_000, ge=1)
    max_prompt_bytes: int = Field(default=500_000, ge=1)
    max_response_bytes: int = Field(default=2_000_000, ge=1)
    max_event_bytes: int = Field(default=4_000_000, ge=1)
    max_event_count: int = Field(default=4096, ge=1)
    max_sessions: int = Field(
        default=64,
        ge=1,
        description=(
            "Maximum generation-long logical session mappings, including "
            "retired/lost tombstones"
        ),
    )
    max_operations: int = Field(default=4096, ge=1)
    max_queued_prompts: int = Field(default=64, ge=0)
    max_execution_timeout_s: float = Field(default=600.0, gt=0)
    session_creation_timeout_s: float = Field(default=30.0, gt=0)
    cancellation_grace_s: float = Field(default=10.0, gt=0)


class ExecutionAuthority(StrictModel):
    """Truthful process/callback authority advertised to Meadow."""

    profile: Literal["trusted-host", "confined-container"]
    acp_agent_internal_tools: Literal["process-user", "container-boundary"]
    filesystem_callbacks: bool = False
    terminal_callbacks: bool = False
    permission_callbacks: bool = False
    permission_default: Literal["deny"] = "deny"


class DirectFeatures(StrictModel):
    """Normalized direct features, independent of raw ACP capability names."""

    request_status: bool = True
    request_cancellation: bool = True
    session_retirement: bool = True
    at_most_once_generation_ledger: bool = True
    per_session_prompt_exclusion: bool = True
    cross_session_parallel_prompts: bool = False
    transparent_session_recovery: bool = False
    native_output_schema: bool = False
    provider_system_role: bool = False
    ordered_request_events: bool = True
    request_scoped_tool_activity: bool = True
    permission_activity_observation: bool = True
    effect_observation: bool = False
    usage_reporting: bool = False


class CapabilitiesResponse(StrictModel):
    protocol: Literal["meadow-acp-direct"] = DIRECT_PROTOCOL_ID
    protocol_major: Literal[1] = DIRECT_PROTOCOL_MAJOR
    proxy_version: str = PROXY_VERSION
    continuity_generation_id: Identifier
    consumer_mode: Literal["meadow-direct"] = "meadow-direct"
    canonical_workspace: str
    execution_authority: ExecutionAuthority
    limits: DirectLimits
    features: DirectFeatures = Field(default_factory=DirectFeatures)
    model_ids: list[str]
    acp_protocol_version: int
    acp_agent_info: dict[str, Any]
    acp_agent_capabilities: dict[str, Any]


class PinnedRequest(StrictModel):
    protocol_major: Literal[1]
    continuity_generation_id: Identifier = Field(min_length=1, max_length=256)
    operation_id: Identifier = Field(min_length=1, max_length=256)


class CreateSessionRequest(PinnedRequest):
    logical_session_id: Identifier = Field(min_length=1, max_length=256)
    expected_canonical_workspace: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=1024)
    model_id: str = Field(min_length=1, max_length=256)
    stable_instruction_digest: Sha256Digest = Field(pattern=r"^[0-9a-f]{64}$")


class PromptPhase(StrEnum):
    INITIAL = "initial"
    INVOCATION = "invocation"
    CORRECTION = "correction"
    CONTINUATION = "continuation"


class PromptRequest(PinnedRequest):
    invocation_id: Identifier = Field(min_length=1, max_length=256)
    phase: PromptPhase = Field(strict=False)
    stable_instruction_digest: Sha256Digest = Field(pattern=r"^[0-9a-f]{64}$")
    output_contract_digest: Sha256Digest = Field(pattern=r"^[0-9a-f]{64}$")
    execution_timeout_s: float = Field(gt=0)
    stable_instructions: str | None = None
    prompt: str | None = None
    output_contract: str | None = None
    delta: str | None = None

    @model_validator(mode="after")
    def validate_layers(self) -> PromptRequest:
        if self.phase is PromptPhase.INITIAL:
            if self.stable_instructions is None:
                raise ValueError(
                    "initial phase requires stable_instructions (empty bytes are valid)"
                )
            if not self.prompt or not self.output_contract:
                raise ValueError("initial phase requires prompt and output_contract")
            if self.delta is not None:
                raise ValueError("initial phase forbids delta")
        elif self.phase is PromptPhase.INVOCATION:
            required = (self.prompt, self.output_contract)
            if any(value is None or value == "" for value in required):
                raise ValueError("invocation phase requires prompt and output_contract")
            if self.stable_instructions is not None or self.delta is not None:
                raise ValueError(
                    "invocation phase forbids stable_instructions and delta"
                )
        else:
            if self.delta is None or self.delta == "":
                raise ValueError("correction/continuation phase requires delta")
            replayed = (
                self.stable_instructions,
                self.prompt,
                self.output_contract,
            )
            if any(value is not None for value in replayed):
                raise ValueError(
                    "correction/continuation phase forbids retransmitted layers"
                )
        return self


class CancelRequest(PinnedRequest):
    target_operation_id: Identifier = Field(min_length=1, max_length=256)


class RetireSessionRequest(PinnedRequest):
    logical_session_id: Identifier = Field(min_length=1, max_length=256)


class EvidenceAvailability(StrEnum):
    REPORTED = "reported"
    OBSERVED = "observed"
    UNAVAILABLE = "unavailable"


class OrderedEvent(StrictModel):
    sequence: int = Field(ge=0)
    update_type: str
    raw: dict[str, Any]


class ToolEvidence(StrictModel):
    availability: EvidenceAvailability = Field(strict=False)
    tool_call_ids: list[str]
    events: list[OrderedEvent]


class UsageEvidence(StrictModel):
    availability: EvidenceAvailability = Field(strict=False)
    values: dict[str, int] | None = None


class PermissionEvidence(StrictModel):
    availability: EvidenceAvailability = Field(strict=False)
    events: list[OrderedEvent]


class PromptResult(StrictModel):
    logical_session_id: Identifier
    backend_session_id: Identifier
    invocation_id: Identifier
    operation_id: Identifier
    continuity_generation_id: Identifier
    model_id: str
    response_text: str
    acp_stop_reason: str
    events: list[OrderedEvent]
    tool_evidence: ToolEvidence
    permission_evidence: PermissionEvidence
    effect_evidence: EvidenceAvailability = Field(strict=False)
    usage: UsageEvidence
    instruction_submission: Literal[
        "submitted_once",
        "not_resubmitted_same_session",
    ]
    stable_instruction_digest: Sha256Digest
    output_contract_digest: Sha256Digest | None = None


class OperationView(StrictModel):
    operation_id: Identifier
    kind: str
    state: str
    logical_session_id: Identifier | None = None
    invocation_id: Identifier | None = None
    target_operation_id: Identifier | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def sha256_text(value: str) -> Sha256Digest:
    """Return the lowercase SHA-256 digest used only for evidence matching."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_request_digest(request: BaseModel) -> Sha256Digest:
    """Hash a strict canonical representation for at-most-once ID reuse."""

    payload = request.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
