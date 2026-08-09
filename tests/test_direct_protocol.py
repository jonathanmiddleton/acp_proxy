"""Property tests for the strict Meadow direct wire contract."""

from __future__ import annotations

import hashlib

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from acp_proxy.direct_protocol import (
    DIRECT_PROTOCOL_MAJOR,
    CreateSessionRequest,
    DirectLimits,
    PromptRequest,
    PromptResult,
    canonical_request_digest,
)

_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
IDENTIFIERS = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from(_ALNUM),
    st.text(alphabet=_ALNUM + "._~-", min_size=0, max_size=39),
)
TEXT = st.text(min_size=1, max_size=200)


@given(
    session_id=IDENTIFIERS,
    operation_id=IDENTIFIERS,
    generation_id=IDENTIFIERS,
    model_id=IDENTIFIERS,
    instructions=TEXT,
)
def test_create_session_digest_is_stable_and_identity_sensitive(
    session_id: str,
    operation_id: str,
    generation_id: str,
    model_id: str,
    instructions: str,
) -> None:
    """ADI-03/04: IDs, model, and instruction identity all bind admission."""
    request = CreateSessionRequest(
        protocol_major=DIRECT_PROTOCOL_MAJOR,
        continuity_generation_id=generation_id,
        operation_id=operation_id,
        logical_session_id=session_id,
        expected_canonical_workspace="/workspace",
        actor_ref="actor",
        title="title",
        model_id=model_id,
        stable_instruction_digest=hashlib.sha256(instructions.encode()).hexdigest(),
    )

    assert canonical_request_digest(request) == canonical_request_digest(request)
    changed = request.model_copy(update={"operation_id": operation_id + "x"})
    assert canonical_request_digest(request) != canonical_request_digest(changed)


@given(extra_key=IDENTIFIERS.filter(lambda value: value not in {"model_id"}))
def test_direct_requests_reject_unknown_fields(extra_key: str) -> None:
    """ADI-02/12: direct mode never ignores extension or legacy fields."""
    payload = {
        "protocol_major": DIRECT_PROTOCOL_MAJOR,
        "continuity_generation_id": "generation",
        "operation_id": "operation",
        "logical_session_id": "session",
        "expected_canonical_workspace": "/workspace",
        "actor_ref": "actor",
        "title": "title",
        "model_id": "gpt-5.3-codex",
        "stable_instruction_digest": "0" * 64,
        extra_key: "ignored-by-an-old-proxy",
    }
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate(payload)


@pytest.mark.parametrize("invalid_major", [True, 1.0, "1"])
def test_direct_requests_reject_coerced_protocol_major(
    invalid_major: object,
) -> None:
    """ADI-02: the protocol major has one exact JSON type and value."""

    payload = {
        "protocol_major": invalid_major,
        "continuity_generation_id": "generation",
        "operation_id": "operation",
        "logical_session_id": "session",
        "expected_canonical_workspace": "/workspace",
        "actor_ref": "actor",
        "title": "title",
        "model_id": "gpt-5.3-codex",
        "stable_instruction_digest": "0" * 64,
    }
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate(payload)


@pytest.mark.parametrize("invalid_count", [True, 4096.0, "4096"])
def test_direct_limits_reject_coerced_numeric_types(invalid_count: object) -> None:
    """ADI-15: admission bounds cannot change meaning through coercion."""

    with pytest.raises(ValidationError):
        DirectLimits.model_validate({"max_event_count": invalid_count})


@given(stable=st.text(max_size=200), prompt=TEXT, contract=TEXT)
def test_prompt_phase_shapes_forbid_layer_retransmission(
    stable: str, prompt: str, contract: str
) -> None:
    """ADI-06/07: initial layers occur once and correction carries only a delta."""
    digest = hashlib.sha256(stable.encode()).hexdigest()
    common = {
        "protocol_major": DIRECT_PROTOCOL_MAJOR,
        "continuity_generation_id": "generation",
        "operation_id": "operation",
        "invocation_id": "invocation",
        "stable_instruction_digest": digest,
        "output_contract_digest": hashlib.sha256(contract.encode()).hexdigest(),
        "execution_timeout_s": 30.0,
    }
    initial = PromptRequest(
        **common,
        phase="initial",
        stable_instructions=stable,
        prompt=prompt,
        output_contract=contract,
    )
    assert initial.phase == "initial"

    correction_payload = {
        **common,
        "operation_id": "correction-operation",
        "phase": "correction",
        "delta": "correct the malformed envelope",
        "stable_instructions": stable,
    }
    with pytest.raises(ValidationError):
        PromptRequest.model_validate(correction_payload)


@given(
    unsafe_id=st.text(min_size=1, max_size=40).filter(
        lambda value: any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-"
            for character in value
        )
    )
)
def test_wire_identifiers_reject_non_path_safe_values(unsafe_id: str) -> None:
    """ADI-03/12: opaque IDs have one unambiguous URL-segment grammar."""

    payload = {
        "protocol_major": DIRECT_PROTOCOL_MAJOR,
        "continuity_generation_id": "generation",
        "operation_id": unsafe_id,
        "logical_session_id": "session",
        "expected_canonical_workspace": "/workspace",
        "actor_ref": "actor",
        "title": "title",
        "model_id": "gpt-5.3-codex",
        "stable_instruction_digest": "0" * 64,
    }
    with pytest.raises(ValidationError):
        CreateSessionRequest.model_validate(payload)


def test_instruction_submission_reports_only_observed_submission_states() -> None:
    """ADI-06: the wire does not infer behavioral recall from session reuse."""

    payload = {
        "logical_session_id": "session",
        "backend_session_id": "backend",
        "invocation_id": "invocation",
        "operation_id": "operation",
        "continuity_generation_id": "generation",
        "model_id": "gpt-5.3-codex",
        "response_text": "result",
        "acp_stop_reason": "end_turn",
        "events": [],
        "tool_evidence": {
            "availability": "observed",
            "tool_call_ids": [],
            "events": [],
        },
        "permission_evidence": {"availability": "observed", "events": []},
        "effect_evidence": "unavailable",
        "usage": {"availability": "unavailable", "values": None},
        "instruction_submission": "unavailable",
        "stable_instruction_digest": "0" * 64,
        "output_contract_digest": "1" * 64,
    }

    with pytest.raises(ValidationError):
        PromptResult.model_validate(payload)
