"""Generated reference traces against the production direct service."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from meadow_bridge.native_types import NativeTerminal
from tests.test_direct_server import FakeNativeClient
from meadow_bridge.direct_protocol import (
    CancelRequest,
    CreateSessionRequest,
    DirectLimits,
    PromptRequest,
    PromptPhase,
    PermissionPolicy,
    RetireSessionRequest,
)
from meadow_bridge.direct_service import (
    DirectBusy,
    DirectGenerationMismatch,
    DirectService,
)
from meadow_bridge.direct_state import (
    DirectConflict,
    DirectLimitExceeded,
    DirectStateError,
)


class _TraceNative(FakeNativeClient):
    """Observable native boundary used by generated production lifecycle traces."""

    def __init__(self) -> None:
        super().__init__()
        self.prompted: list[str] = []
        self.prompt_started = asyncio.Event()
        self.release_prompts = self.release_prompt

    async def run_turn(
        self,
        logical_id: str,
        text: str,
        *,
        timeout_s: float,
        event_byte_limit: int,
        event_count_limit: int,
        response_byte_limit: int,
    ) -> NativeTerminal:
        self.prompted.append(logical_id)
        self.prompt_started.set()
        return await super().run_turn(
            logical_id,
            text,
            timeout_s=timeout_s,
            event_byte_limit=event_byte_limit,
            event_count_limit=event_count_limit,
            response_byte_limit=response_byte_limit,
        )


@dataclass
class _ReferenceSession:
    ready: bool = True
    initialized: bool = False
    invocation: str | None = None


Action = tuple[str, int]
ACTIONS = st.lists(
    st.tuples(
        st.sampled_from(
            [
                "create",
                "initial",
                "invocation",
                "correction",
                "continuation",
                "cancel",
                "retire",
                "rotate",
            ]
        ),
        st.integers(min_value=0, max_value=2),
    ),
    min_size=1,
    max_size=30,
)


@settings(max_examples=60, deadline=None)
@given(actions=ACTIONS)
def test_generated_direct_lifecycle_trace(actions: list[Action]) -> None:
    """ADI-03/04/05/06/07/10/11/15: generated mutations match the model."""
    asyncio.run(_exercise_trace(actions))


async def _exercise_trace(actions: list[Action]) -> None:
    fake = _TraceNative()
    limits = DirectLimits(max_sessions=2, max_operations=10)
    service = DirectService(
        fake,
        cwd="/tmp/meadow-bridge-direct-state-trace",
        launch_secret="t" * 48,
        execution_authority="trusted-host",
        limits=limits,
        continuity_generation_id="trace-generation",
    )
    stable = ""
    stable_digest = hashlib.sha256(stable.encode()).hexdigest()
    contract = "complete prose contract"
    contract_digest = hashlib.sha256(contract.encode()).hexdigest()
    model: dict[int, _ReferenceSession] = {}
    last_prompt: dict[int, str] = {}
    admitted_operations = 0
    prompt_effects = 0
    available = True

    for step, (kind, slot) in enumerate(actions):
        operation_id = f"operation-{step}"
        logical_id = f"session-{slot}"
        state = model.get(slot)

        if kind == "rotate":
            if available:
                await service.mark_generation_lost("generated trace rotation")
                available = False
            else:
                await service.mark_generation_lost("duplicate generated rotation")
            with pytest.raises(DirectGenerationMismatch):
                _ = service.capabilities
            continue

        if not available:
            with pytest.raises(DirectGenerationMismatch):
                _ = service.capabilities
            continue

        if kind == "create":
            request = CreateSessionRequest(
                protocol_major=2,
                continuity_generation_id=service.continuity_generation_id,
                operation_id=operation_id,
                logical_session_id=logical_id,
                expected_canonical_workspace=service.canonical_workspace,
                actor_ref="actor",
                title="Actor",
                model_id="gpt-5.3-codex",
                stable_instruction_digest=stable_digest,
                permission_policy=PermissionPolicy(version=1, mode="allow_all"),
            )
            if state is not None:
                with pytest.raises(DirectConflict):
                    await service.admit_create(request)
            elif (
                len(model) >= limits.max_sessions
                or admitted_operations >= limits.max_operations
            ):
                with pytest.raises(DirectLimitExceeded):
                    await service.admit_create(request)
            else:
                record, created = await service.admit_create(request)
                assert created
                assert (await service.wait_for_operation(record)).state == "completed"
                admitted_operations += 1
                model[slot] = _ReferenceSession()
        elif kind in {"initial", "invocation", "correction", "continuation"}:
            phase = kind
            invocation = (
                f"invocation-{step}"
                if phase in {"initial", "invocation"}
                else (state.invocation if state else None) or "missing-invocation"
            )
            prompt_kwargs: dict[str, object] = {
                "protocol_major": 2,
                "continuity_generation_id": service.continuity_generation_id,
                "operation_id": operation_id,
                "invocation_id": invocation,
                "phase": phase,
                "stable_instruction_digest": stable_digest,
                "output_contract_digest": contract_digest,
                "execution_timeout_s": 1.0,
            }
            if phase == "initial":
                prompt_kwargs.update(
                    stable_instructions=stable,
                    prompt="current prompt with routes",
                    output_contract=contract,
                )
            elif phase == "invocation":
                prompt_kwargs.update(
                    prompt="later prompt with routes", output_contract=contract
                )
            else:
                prompt_kwargs["delta"] = f"{phase} delta"
            prompt_request = PromptRequest.model_validate(prompt_kwargs)
            phase_is_valid = bool(
                state
                and state.ready
                and (
                    (phase == "initial" and not state.initialized)
                    or (phase == "invocation" and state.initialized)
                    or (
                        phase in {"correction", "continuation"}
                        and state.initialized
                        and state.invocation is not None
                    )
                )
            )
            if not phase_is_valid:
                with pytest.raises(DirectStateError):
                    await service.admit_prompt(logical_id, prompt_request)
            elif admitted_operations >= limits.max_operations:
                with pytest.raises(DirectLimitExceeded):
                    await service.admit_prompt(logical_id, prompt_request)
            else:
                record, created = await service.admit_prompt(logical_id, prompt_request)
                assert created
                assert (await service.wait_for_operation(record)).state == "completed"
                admitted_operations += 1
                prompt_effects += 1
                assert state is not None
                state.initialized = True
                state.invocation = invocation
                last_prompt[slot] = operation_id
        elif kind == "cancel":
            target = last_prompt.get(slot)
            if target is None:
                continue
            cancel_request = CancelRequest(
                protocol_major=2,
                continuity_generation_id=service.continuity_generation_id,
                operation_id=operation_id,
                target_operation_id=target,
            )
            if admitted_operations >= limits.max_operations:
                with pytest.raises(DirectLimitExceeded):
                    await service.admit_cancel(cancel_request)
            else:
                record, created = await service.admit_cancel(cancel_request)
                assert created
                assert (await service.wait_for_operation(record)).state == "completed"
                admitted_operations += 1
        else:
            retire_request = RetireSessionRequest(
                protocol_major=2,
                continuity_generation_id=service.continuity_generation_id,
                operation_id=operation_id,
                logical_session_id=logical_id,
            )
            if not state or not state.ready:
                with pytest.raises(DirectStateError):
                    await service.admit_retire(retire_request)
            elif admitted_operations >= limits.max_operations:
                with pytest.raises(DirectLimitExceeded):
                    await service.admit_retire(retire_request)
            else:
                record, created = await service.admit_retire(retire_request)
                assert created
                assert (await service.wait_for_operation(record)).state == "completed"
                admitted_operations += 1
                state.ready = False

        assert len(fake.created) == len(model)
        assert set(fake.bindings) == {f"session-{slot}" for slot in model}
        assert len(fake.prompted) == prompt_effects
        assert fake.cancelled == []


SAFE_ID = st.builds(
    lambda first, rest: first + rest,
    st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
    st.text(
        alphabet=("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-"),
        min_size=0,
        max_size=24,
    ),
)


@settings(max_examples=30, deadline=None)
@given(
    operation_id=SAFE_ID.filter(
        lambda value: value not in {"create-0", "create-1", "create-2"}
    ),
    cancel_active=st.booleans(),
    terminal_wins=st.booleans(),
)
def test_generated_concurrent_idempotency_cancel_and_isolation_trace(
    operation_id: str, cancel_active: bool, terminal_wins: bool
) -> None:
    """ADI-04/05/10/15: generated interleavings match observable effects."""

    asyncio.run(_exercise_concurrent_trace(operation_id, cancel_active, terminal_wins))


async def _exercise_concurrent_trace(
    operation_id: str, cancel_active: bool, terminal_wins: bool
) -> None:
    fake = _TraceNative()
    fake.block_prompts = True
    service = DirectService(
        fake,
        cwd="/tmp/meadow-bridge-direct-concurrent-trace",
        launch_secret="t" * 48,
        execution_authority="trusted-host",
        limits=DirectLimits(
            max_sessions=3,
            max_operations=32,
            max_queued_prompts=2,
        ),
        continuity_generation_id="concurrent-generation",
    )
    stable_digest = hashlib.sha256(b"").hexdigest()
    contract = "contract"
    contract_digest = hashlib.sha256(contract.encode()).hexdigest()

    for index in range(3):
        create = CreateSessionRequest(
            protocol_major=2,
            continuity_generation_id=service.continuity_generation_id,
            operation_id=f"create-{index}",
            logical_session_id=f"session-{index}",
            expected_canonical_workspace=service.canonical_workspace,
            actor_ref="actor",
            title="Actor",
            model_id="gpt-5.3-codex",
            stable_instruction_digest=stable_digest,
            permission_policy=PermissionPolicy(version=1, mode="allow_all"),
        )
        record, _ = await service.admit_create(create)
        assert (await service.wait_for_operation(record)).state == "completed"

    def prompt_request(op_id: str, invocation: str) -> PromptRequest:
        return PromptRequest(
            protocol_major=2,
            continuity_generation_id=service.continuity_generation_id,
            operation_id=op_id,
            invocation_id=invocation,
            phase=PromptPhase.INITIAL,
            stable_instruction_digest=stable_digest,
            output_contract_digest=contract_digest,
            execution_timeout_s=2,
            stable_instructions="",
            prompt="prompt with routes",
            output_contract=contract,
        )

    first_request = prompt_request(operation_id, "invocation-zero")
    first, created = await service.admit_prompt("session-0", first_request)
    assert created
    await asyncio.wait_for(fake.prompt_started.wait(), timeout=1)

    queued_request = prompt_request(f"{operation_id}-queued", "invocation-one")
    queued, created = await service.admit_prompt("session-1", queued_request)
    assert created
    duplicate, duplicate_created = await service.admit_prompt(
        "session-1", queued_request
    )
    assert duplicate is queued
    assert duplicate_created is False

    conflicting = queued_request.model_copy(
        update={"invocation_id": "different-invocation"}
    )
    with pytest.raises(DirectConflict, match="reused with different content"):
        await service.admit_prompt("session-1", conflicting)
    with pytest.raises(DirectBusy):
        await service.admit_prompt(
            "session-1",
            prompt_request(f"{operation_id}-busy", "another-invocation"),
        )

    target = first if cancel_active else queued
    terminal_won = cancel_active and terminal_wins
    if terminal_won:
        fake.release_prompts.set()
        assert (await service.wait_for_operation(first)).state == "completed"
    cancel = CancelRequest(
        protocol_major=2,
        continuity_generation_id=service.continuity_generation_id,
        operation_id=f"{operation_id}-cancel",
        target_operation_id=target.operation_id,
    )
    cancel_record, created = await service.admit_cancel(cancel)
    assert created
    assert (await service.wait_for_operation(cancel_record)).state == "completed"
    if not cancel_active:
        assert fake.cancelled == []
        fake.release_prompts.set()

    first_view, queued_view = await asyncio.gather(
        service.wait_for_operation(first),
        service.wait_for_operation(queued),
    )
    assert (first_view.state == "cancelled") is (cancel_active and not terminal_won)
    assert (queued_view.state == "cancelled") is (not cancel_active)
    assert fake.cancelled == (
        ["session-0"] if cancel_active and not terminal_won else []
    )

    isolated_request = prompt_request(f"{operation_id}-isolated", "invocation-two")
    isolated, created = await service.admit_prompt("session-2", isolated_request)
    assert created
    assert (await service.wait_for_operation(isolated)).state == "completed"
    assert fake.prompted[-1] == "session-2"
