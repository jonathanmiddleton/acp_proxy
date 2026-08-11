"""Production-boundary tests for the authenticated Meadow direct router."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from acp_proxy.client import CallbackPolicy, ModelAcknowledgementError, ModelInfo
from acp_proxy.direct_protocol import (
    CancelRequest,
    CreateSessionRequest,
    DirectLimits,
    PromptRequest,
    RetireSessionRequest,
)
from acp_proxy.direct_server import RequestBodyLimitMiddleware, create_direct_app
from acp_proxy.direct_service import DirectGenerationMismatch, DirectService
from acp_proxy.direct_state import DirectConflict, DirectLimitExceeded

TOKEN = "t" * 48


@dataclass
class FakeSessionDescriptor:
    session_id: str
    model_id: str


class FakeDirectAcpClient:
    """Observable ACP boundary double; protocol state remains real in the service."""

    def __init__(self) -> None:
        self.callback_policy = CallbackPolicy.DIRECT_DENY
        self.models = [ModelInfo("gpt-5.3-codex", "GPT-5.3 Codex")]
        self.protocol_version = 1
        self.agent_info = {"name": "fake-copilot", "version": "1.0"}
        self.agent_capabilities = {
            "loadSession": True,
            "sessionCapabilities": {"list": {}},
        }
        self.created: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, list[dict[str, Any]]]] = []
        self.cancelled: list[str] = []
        self.release_prompt = asyncio.Event()
        self.block_prompts = False
        self.block_create = False
        self.release_create = asyncio.Event()
        self.create_error: Exception | None = None
        self.updates: list[dict[str, Any]] = [
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": '{"messages": []}'},
            }
        ]
        self.stop_reason = "end_turn"
        self.cancel_stop_reason = "cancelled"
        self.active_prompts = 0
        self.max_active_prompts = 0
        self.is_alive = True
        self.lose_transport_after_updates = False
        self.ignore_cancel = False
        self.cancel_error: Exception | None = None
        self.cancel_hangs = False
        self.abort_count = 0

    async def create_session_exact(self, cwd: str, model_id: str) -> Any:
        if self.block_create:
            await self.release_create.wait()
        if self.create_error is not None:
            raise self.create_error
        session_id = f"backend-{len(self.created) + 1}"
        self.created.append((cwd, model_id))
        return FakeSessionDescriptor(session_id, model_id)

    async def prompt_blocks(
        self,
        session_id: str,
        blocks: list[dict[str, Any]],
        *,
        timeout_s: float,
        event_byte_limit: int,
        event_count_limit: int,
    ) -> AsyncIterator[dict[str, Any]]:
        self.prompts.append((session_id, blocks))
        self.active_prompts += 1
        self.max_active_prompts = max(self.max_active_prompts, self.active_prompts)
        try:
            if self.block_prompts:
                await self.release_prompt.wait()
            for update in self.updates:
                yield update
            if self.lose_transport_after_updates:
                self.is_alive = False
                raise ConnectionError("private transport detail after accepted prompt")
            stop_reason = (
                self.cancel_stop_reason
                if session_id in self.cancelled
                else self.stop_reason
            )
            yield {"done": True, "stopReason": stop_reason}
        finally:
            self.active_prompts -= 1

    async def cancel_session(self, session_id: str) -> None:
        if self.cancel_hangs:
            await asyncio.Event().wait()
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(session_id)
        if not self.ignore_cancel:
            self.release_prompt.set()

    async def abort(self) -> None:
        self.abort_count += 1
        self.is_alive = False
        self.release_create.set()
        self.release_prompt.set()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _create_body(service: DirectService, *, operation: str, session: str) -> dict:
    stable = "stable Meadow instructions"
    return {
        "protocol_major": 1,
        "continuity_generation_id": service.continuity_generation_id,
        "operation_id": operation,
        "logical_session_id": session,
        "expected_canonical_workspace": service.canonical_workspace,
        "actor_ref": "developer",
        "title": "Developer",
        "model_id": "gpt-5.3-codex",
        "stable_instruction_digest": hashlib.sha256(stable.encode()).hexdigest(),
    }


def _prompt_body(
    service: DirectService,
    *,
    operation: str,
    invocation: str,
    phase: str = "initial",
) -> dict:
    stable = "stable Meadow instructions"
    body: dict[str, Any] = {
        "protocol_major": 1,
        "continuity_generation_id": service.continuity_generation_id,
        "operation_id": operation,
        "invocation_id": invocation,
        "phase": phase,
        "stable_instruction_digest": hashlib.sha256(stable.encode()).hexdigest(),
        "output_contract_digest": hashlib.sha256(
            (
                "next complete prose contract"
                if phase == "invocation"
                else "complete prose contract"
            ).encode()
        ).hexdigest(),
        "execution_timeout_s": 2.0,
    }
    if phase == "initial":
        body.update(
            stable_instructions=stable,
            prompt="current prompt\n\n## Legal Typed Routes\nroute body",
            output_contract="complete prose contract",
        )
    elif phase == "invocation":
        body.update(
            prompt="next prompt\n\n## Legal Typed Routes\nnext route body",
            output_contract="next complete prose contract",
        )
    else:
        body["delta"] = "validation diagnostic only"
    return body


async def _settle_creation(
    service: DirectService, operation: str, session: str
) -> None:
    request = CreateSessionRequest.model_validate(
        _create_body(service, operation=operation, session=session)
    )
    record, _ = await service.admit_create(request)
    view = await service.wait_for_operation(record)
    assert view.state == "completed"


@pytest.fixture
def direct_boundary(tmp_path: Path) -> tuple[DirectService, FakeDirectAcpClient, Any]:
    fake = FakeDirectAcpClient()
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(max_sessions=8, max_operations=32),
        continuity_generation_id="generation-test",
    )
    return service, fake, create_direct_app(service)


def test_direct_service_rejects_permissive_callback_client(tmp_path: Path) -> None:
    """ADI-09: deny-only capability claims require client-side attestation."""

    fake = FakeDirectAcpClient()
    fake.callback_policy = CallbackPolicy.LEGACY_PERMISSIVE

    with pytest.raises(ValueError, match="direct-deny callback policy"):
        DirectService(
            fake,
            cwd=str(tmp_path),
            launch_secret=TOKEN,
            execution_authority="trusted-host",
        )


@pytest.mark.asyncio
async def test_session_mapping_capacity_retains_retired_tombstones(
    tmp_path: Path,
) -> None:
    """ADI-03/15: retirement never permits generation-local identity reuse."""

    fake = FakeDirectAcpClient()
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(max_sessions=1, max_operations=8),
    )
    await _settle_creation(service, "create-one", "session-one")
    retire, _ = await service.admit_retire(
        RetireSessionRequest(
            protocol_major=1,
            continuity_generation_id=service.continuity_generation_id,
            operation_id="retire-one",
            logical_session_id="session-one",
        )
    )
    assert (await service.wait_for_operation(retire)).state == "completed"

    request = CreateSessionRequest.model_validate(
        _create_body(service, operation="create-two", session="session-two")
    )
    with pytest.raises(
        DirectLimitExceeded,
        match="generation-long session mapping capacity",
    ):
        await service.admit_create(request)
    assert len(fake.created) == 1


@pytest.mark.asyncio
async def test_capability_handshake_is_authenticated_and_truthful(
    direct_boundary: tuple[DirectService, FakeDirectAcpClient, Any],
) -> None:
    """ADI-02/08/15: capability evidence is exact and never public."""
    service, fake, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        denied = await client.get("/meadow/v1/capabilities")
        accepted = await client.get("/meadow/v1/capabilities", headers=_auth())
        repeated = await client.get("/meadow/v1/capabilities", headers=_auth())

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert repeated.json() == accepted.json()
    payload = accepted.json()
    assert payload["continuity_generation_id"] == service.continuity_generation_id
    assert payload["consumer_mode"] == "meadow-direct"
    assert payload["model_ids"] == ["gpt-5.3-codex"]
    assert payload["features"]["native_output_schema"] is False
    assert payload["features"]["request_scoped_tool_activity"] is True
    assert payload["features"]["effect_observation"] is False
    assert payload["features"]["usage_reporting"] is False
    assert payload["execution_authority"]["terminal_callbacks"] is False
    assert payload["execution_authority"]["permission_callbacks"] is False
    assert fake.created == []
    assert fake.prompts == []


@pytest.mark.asyncio
async def test_session_identity_is_explicit_idempotent_and_isolated(
    direct_boundary: tuple[DirectService, FakeDirectAcpClient, Any],
) -> None:
    """ADI-03/04: explicit IDs map once and identical prompts cannot collide."""
    service, fake, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        first_body = _create_body(service, operation="create-one", session="one")
        first = await client.post("/meadow/v1/sessions", json=first_body, headers=_auth())
        duplicate = await client.post(
            "/meadow/v1/sessions", json=first_body, headers=_auth()
        )
        second = await client.post(
            "/meadow/v1/sessions",
            json=_create_body(service, operation="create-two", session="two"),
            headers=_auth(),
        )

    assert first.status_code == duplicate.status_code == second.status_code == 200
    assert first.json()["result"]["backend_session_id"] == "backend-1"
    assert duplicate.json()["result"]["backend_session_id"] == "backend-1"
    assert second.json()["result"]["backend_session_id"] == "backend-2"
    assert len(fake.created) == 2


@pytest.mark.asyncio
async def test_instruction_contract_layers_are_not_replayed_on_correction(
    direct_boundary: tuple[DirectService, FakeDirectAcpClient, Any],
) -> None:
    """ADI-06/07/08: stable/current/schema layers occur once; repair is a delta."""
    service, fake, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        await client.post(
            "/meadow/v1/sessions",
            json=_create_body(service, operation="create", session="session"),
            headers=_auth(),
        )
        initial = await client.post(
            "/meadow/v1/sessions/session/requests",
            json=_prompt_body(
                service, operation="initial", invocation="invocation"
            ),
            headers=_auth(),
        )
        correction = await client.post(
            "/meadow/v1/sessions/session/requests",
            json=_prompt_body(
                service,
                operation="correction",
                invocation="invocation",
                phase="correction",
            ),
            headers=_auth(),
        )

    assert initial.status_code == correction.status_code == 200
    first_text = fake.prompts[0][1][0]["text"]
    repair_text = fake.prompts[1][1][0]["text"]
    assert first_text.count("stable Meadow instructions") == 1
    assert first_text.count("complete prose contract") == 1
    assert repair_text == "validation diagnostic only"
    assert "stable Meadow instructions" not in repair_text
    assert "complete prose contract" not in repair_text
    result = initial.json()["result"]
    assert result["usage"]["availability"] == "unavailable"
    assert result["effect_evidence"] == "unavailable"
    assert "tool_call_count" not in result


@pytest.mark.asyncio
async def test_unproven_usage_and_session_info_remain_raw_diagnostics(
    tmp_path: Path,
) -> None:
    """ADI-08: malformed or ambiguous counters never become usage evidence."""

    fake = FakeDirectAcpClient()
    fake.updates = [
        {"sessionUpdate": "usage_update", "inputTokens": True},
        {"sessionUpdate": "usage_update", "outputTokens": -1},
        {"sessionUpdate": "usage_update", "totalTokens": "malformed"},
        {"sessionUpdate": "session_info_update", "sessionInfo": [False, -2]},
    ]
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
    )
    await _settle_creation(service, "create", "session")

    record, _ = await service.admit_prompt(
        "session",
        PromptRequest.model_validate(
            _prompt_body(service, operation="prompt", invocation="invocation")
        ),
    )
    view = await service.wait_for_operation(record)

    assert view.state == "completed"
    assert view.result is not None
    assert view.result["usage"] == {"availability": "unavailable", "values": None}
    assert [event["raw"] for event in view.result["events"]] == fake.updates


@pytest.mark.asyncio
async def test_busy_session_rejects_second_prompt_before_dispatch(
    direct_boundary: tuple[DirectService, FakeDirectAcpClient, Any],
) -> None:
    """ADI-05: one session has one in-flight ACP prompt and one event owner."""
    service, fake, app = direct_boundary
    fake.block_prompts = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        await client.post(
            "/meadow/v1/sessions",
            json=_create_body(service, operation="create", session="session"),
            headers=_auth(),
        )
        first_task = asyncio.create_task(
            client.post(
                "/meadow/v1/sessions/session/requests",
                json=_prompt_body(
                    service, operation="first", invocation="invocation"
                ),
                headers=_auth(),
            )
        )
        for _ in range(50):
            if fake.prompts:
                break
            await asyncio.sleep(0.01)
        second = await client.post(
            "/meadow/v1/sessions/session/requests",
            json=_prompt_body(
                service,
                operation="second",
                invocation="other-invocation",
                phase="invocation",
            ),
            headers=_auth(),
        )
        fake.release_prompt.set()
        first = await first_task

    assert second.status_code == 409
    assert first.status_code == 200
    assert len(fake.prompts) == 1


@pytest.mark.asyncio
async def test_permission_outcome_is_request_scoped_ordered_evidence(
    direct_boundary: tuple[DirectService, FakeDirectAcpClient, Any],
) -> None:
    """ADI-08/09: sanitized permission denial remains in the prompt envelope."""
    service, fake, app = direct_boundary
    fake.updates = [
        {
            "sessionUpdate": "client_permission_request",
            "outcome": "cancelled",
            "offeredKinds": ["allow_once"],
        },
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "done"},
        },
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        await client.post(
            "/meadow/v1/sessions",
            json=_create_body(service, operation="create", session="session"),
            headers=_auth(),
        )
        response = await client.post(
            "/meadow/v1/sessions/session/requests",
            json=_prompt_body(service, operation="prompt", invocation="invocation"),
            headers=_auth(),
        )

    result = response.json()["result"]
    assert [event["sequence"] for event in result["events"]] == [0, 1]
    assert result["permission_evidence"] == {
        "availability": "observed",
        "events": [result["events"][0]],
    }


@pytest.mark.asyncio
async def test_direct_mode_rejects_legacy_endpoint(
    direct_boundary: tuple[DirectService, FakeDirectAcpClient, Any],
) -> None:
    """ADI-12: direct and deprecated heuristic traffic never share routing."""
    _, _, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions", json={"messages": []}, headers=_auth()
        )
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "legacy_mode_required"


@pytest.mark.asyncio
async def test_direct_mode_rejects_legacy_endpoint_without_auth(
    direct_boundary: tuple[DirectService, FakeDirectAcpClient, Any],
) -> None:
    """ADI-12: stock OpenCode sees migration guidance rather than generic 401."""

    _, _, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions", json={"messages": []}
        )

    assert response.status_code == 410
    assert response.json() == {
        "error": {
            "code": "legacy_mode_required",
            "message": (
                "/v1/chat/completions is available only in explicit "
                "opencode-legacy mode"
            ),
        }
    }


@pytest.mark.asyncio
async def test_output_contract_digest_and_active_invocation_are_fail_closed(
    direct_boundary: tuple[DirectService, FakeDirectAcpClient, Any],
) -> None:
    """ADI-07: contract bytes bind work and only the active invocation accepts deltas."""
    service, fake, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        await client.post(
            "/meadow/v1/sessions",
            json=_create_body(service, operation="create", session="session"),
            headers=_auth(),
        )
        wrong = _prompt_body(service, operation="wrong", invocation="first")
        wrong["output_contract_digest"] = "0" * 64
        rejected = await client.post(
            "/meadow/v1/sessions/session/requests", json=wrong, headers=_auth()
        )
        first = await client.post(
            "/meadow/v1/sessions/session/requests",
            json=_prompt_body(service, operation="first", invocation="first"),
            headers=_auth(),
        )
        second = await client.post(
            "/meadow/v1/sessions/session/requests",
            json=_prompt_body(
                service,
                operation="second",
                invocation="second",
                phase="invocation",
            ),
            headers=_auth(),
        )
        stale_delta = _prompt_body(
            service,
            operation="stale-delta",
            invocation="first",
            phase="correction",
        )
        stale_delta["output_contract_digest"] = hashlib.sha256(
            b"complete prose contract"
        ).hexdigest()
        stale = await client.post(
            "/meadow/v1/sessions/session/requests",
            json=stale_delta,
            headers=_auth(),
        )

    assert rejected.status_code == 409
    assert first.status_code == second.status_code == 200
    assert stale.status_code == 409
    assert len(fake.prompts) == 2


@pytest.mark.asyncio
async def test_empty_stable_instruction_bytes_are_valid_and_submitted_once(
    tmp_path: Path,
) -> None:
    """ADI-06: submitted empty bytes are distinct from a missing stable layer."""
    fake = FakeDirectAcpClient()
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        continuity_generation_id="empty-stable-generation",
    )
    app = create_direct_app(service)
    create = _create_body(service, operation="create", session="session")
    create["stable_instruction_digest"] = hashlib.sha256(b"").hexdigest()
    prompt = _prompt_body(service, operation="prompt", invocation="invocation")
    prompt["stable_instruction_digest"] = hashlib.sha256(b"").hexdigest()
    prompt["stable_instructions"] = ""

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        created = await client.post(
            "/meadow/v1/sessions", json=create, headers=_auth()
        )
        completed = await client.post(
            "/meadow/v1/sessions/session/requests", json=prompt, headers=_auth()
        )

    assert created.status_code == completed.status_code == 200
    assert completed.json()["result"]["instruction_submission"] == "submitted_once"
    assert fake.prompts[0][1][0]["text"].startswith("\n\ncurrent prompt")


@pytest.mark.parametrize(
    ("phase", "at_text", "above_text"),
    [
        ("initial", "P" * 7, "P" * 8),
        ("invocation", "P" * 9, "P" * 10),
        ("correction", "D" * 12, "D" * 13),
    ],
)
def test_prompt_byte_limit_counts_exact_rendered_separators(
    tmp_path: Path, phase: str, at_text: str, above_text: str
) -> None:
    """ADI-07/15: exact rendered initial/invocation/delta bytes admit at/+1."""
    service = DirectService(
        FakeDirectAcpClient(),
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(max_prompt_bytes=12),
    )

    def request(text_value: str) -> PromptRequest:
        body = _prompt_body(
            service,
            operation=f"operation-{len(text_value)}",
            invocation="invocation",
            phase=phase,
        )
        if phase == "initial":
            body["stable_instructions"] = ""
            body["stable_instruction_digest"] = hashlib.sha256(b"").hexdigest()
            body["prompt"] = text_value
            body["output_contract"] = "C"
            body["output_contract_digest"] = hashlib.sha256(b"C").hexdigest()
        elif phase == "invocation":
            body["prompt"] = text_value
            body["output_contract"] = "C"
            body["output_contract_digest"] = hashlib.sha256(b"C").hexdigest()
        else:
            body["delta"] = text_value
        return PromptRequest.model_validate(body)

    service._check_prompt_limits(request(at_text))
    with pytest.raises(DirectLimitExceeded, match="prompt layers"):
        service._check_prompt_limits(request(above_text))


@settings(
    max_examples=4,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(max_queued=st.integers(min_value=0, max_value=3))
@pytest.mark.asyncio
async def test_global_prompt_queue_has_exact_atomic_admission_edges(
    tmp_path: Path, max_queued: int
) -> None:
    """ADI-05/15: 0/at/max+1 queue edges serialize distinct sessions pre-dispatch."""
    fake = FakeDirectAcpClient()
    fake.block_prompts = True
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(
            max_sessions=8,
            max_operations=64,
            max_queued_prompts=max_queued,
        ),
    )
    session_count = max_queued + 2
    for index in range(session_count):
        await _settle_creation(service, f"create-{index}", f"session-{index}")

    accepted = []
    for index in range(max_queued + 1):
        request = PromptRequest.model_validate(
            _prompt_body(
                service,
                operation=f"prompt-{index}",
                invocation=f"invocation-{index}",
            )
        )
        record, created = await service.admit_prompt(f"session-{index}", request)
        assert created
        accepted.append(record)

    overflow = PromptRequest.model_validate(
        _prompt_body(
            service,
            operation="overflow",
            invocation="overflow-invocation",
        )
    )
    with pytest.raises(DirectLimitExceeded, match="queue capacity"):
        await service.admit_prompt(f"session-{session_count - 1}", overflow)
    assert len(fake.prompts) <= 1

    fake.release_prompt.set()
    for record in accepted:
        assert (await service.wait_for_operation(record)).state == "completed"
    assert fake.max_active_prompts == 1
    assert len(fake.prompts) == max_queued + 1


@pytest.mark.asyncio
async def test_queued_cancellation_settles_without_acp_dispatch_or_cancel(
    tmp_path: Path,
) -> None:
    """ADI-05/10: queued cancellation is terminal before any ACP side effect."""
    fake = FakeDirectAcpClient()
    fake.block_prompts = True
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(max_queued_prompts=1),
    )
    await _settle_creation(service, "create-one", "one")
    await _settle_creation(service, "create-two", "two")
    first, _ = await service.admit_prompt(
        "one",
        PromptRequest.model_validate(
            _prompt_body(service, operation="first", invocation="first")
        ),
    )
    for _ in range(50):
        if fake.prompts:
            break
        await asyncio.sleep(0.01)
    queued, _ = await service.admit_prompt(
        "two",
        PromptRequest.model_validate(
            _prompt_body(service, operation="queued", invocation="queued")
        ),
    )
    cancel, _ = await service.admit_cancel(
        CancelRequest(
            protocol_major=1,
            continuity_generation_id=service.continuity_generation_id,
            operation_id="cancel-queued",
            target_operation_id="queued",
        )
    )

    assert (await service.wait_for_operation(queued)).state == "cancelled"
    cancel_view = await service.wait_for_operation(cancel)
    assert cancel_view.result == {"target_state": "cancelled", "cancel_sent": False}
    assert fake.cancelled == []
    assert len(fake.prompts) == 1
    fake.release_prompt.set()
    assert (await service.wait_for_operation(first)).state == "completed"
    await asyncio.sleep(0)
    assert len(fake.prompts) == 1


@pytest.mark.asyncio
async def test_generation_rotation_quarantines_in_flight_ownership(
    tmp_path: Path,
) -> None:
    """ADI-10/11: in-flight tasks cannot mutate the next generation's ledger or slots."""
    fake = FakeDirectAcpClient()
    fake.block_prompts = True
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(max_queued_prompts=1),
        continuity_generation_id="old-generation",
    )
    await _settle_creation(service, "old-create-one", "old-one")
    await _settle_creation(service, "old-create-two", "old-two")
    old_records = []
    for index, session in enumerate(("old-one", "old-two")):
        record, _ = await service.admit_prompt(
            session,
            PromptRequest.model_validate(
                _prompt_body(
                    service,
                    operation=f"old-prompt-{index}",
                    invocation=f"old-invocation-{index}",
                )
            ),
        )
        old_records.append(record)

    await service.mark_generation_lost("test transport EOF with private detail")
    new_generation = service.continuity_generation_id
    assert new_generation != "old-generation"
    for record in old_records:
        assert (await service.wait_for_operation(record)).state == "in_doubt"
    with pytest.raises(DirectGenerationMismatch, match="continuity generation changed"):
        service.operation(
            "old-prompt-0", protocol_major=1, generation_id="old-generation"
        )
    with pytest.raises(DirectGenerationMismatch, match="managed restart is required"):
        service.operation(
            "old-prompt-0", protocol_major=1, generation_id=new_generation
        )

    replacement = CreateSessionRequest.model_validate(
        _create_body(service, operation="new-create", session="new-session")
    )
    with pytest.raises(DirectGenerationMismatch, match="managed restart is required"):
        await service.admit_create(replacement)
    with pytest.raises(DirectGenerationMismatch, match="managed restart is required"):
        _ = service.capabilities


@pytest.mark.asyncio
async def test_generation_rotation_settles_owned_collector_tasks(
    tmp_path: Path,
) -> None:
    """ADI-10/13: rotation returns only after collectors acknowledge cancel."""

    fake = FakeDirectAcpClient()
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        continuity_generation_id="old-generation",
    )
    collector_started = asyncio.Event()
    collector_cancelled = asyncio.Event()

    async def collector() -> None:
        collector_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            collector_cancelled.set()
            await asyncio.sleep(0)
            raise

    task = asyncio.create_task(collector())
    service._generation.collector_tasks["synthetic-operation"] = task
    service._generation.execution_tasks.add(task)
    await asyncio.wait_for(collector_started.wait(), timeout=1.0)

    await service.mark_generation_lost("synthetic transport loss")

    assert collector_cancelled.is_set()
    assert task.done()
    assert service._generation.collector_tasks == {}


@pytest.mark.asyncio
async def test_status_is_generation_pinned_and_duplicate_work_is_not_redispatched(
    tmp_path: Path,
) -> None:
    """ADI-04/11: response reconciliation joins the recorded operation only."""
    fake = FakeDirectAcpClient()
    fake.block_prompts = True
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
    )
    await _settle_creation(service, "create", "session")
    request = PromptRequest.model_validate(
        _prompt_body(service, operation="prompt", invocation="invocation")
    )
    first, first_created = await service.admit_prompt("session", request)
    duplicate, duplicate_created = await service.admit_prompt("session", request)
    assert first is duplicate
    assert first_created is True
    assert duplicate_created is False
    assert service.operation(
        "prompt",
        protocol_major=1,
        generation_id=service.continuity_generation_id,
    ) is first
    assert len(fake.prompts) <= 1
    fake.release_prompt.set()
    assert (await service.wait_for_operation(first)).state == "completed"
    assert len(fake.prompts) == 1


@pytest.mark.asyncio
async def test_deadline_requires_cancelled_stop_reason_for_timed_out_state(
    tmp_path: Path,
) -> None:
    """ADI-10: deadline settlement is timed_out only after ACP reports cancelled."""
    async def run_case(stop_after_cancel: str) -> str:
        fake = FakeDirectAcpClient()
        fake.block_prompts = True
        fake.cancel_stop_reason = stop_after_cancel
        service = DirectService(
            fake,
            cwd=str(tmp_path),
            launch_secret=TOKEN,
            execution_authority="trusted-host",
            limits=DirectLimits(cancellation_grace_s=0.2),
        )
        await _settle_creation(service, f"create-{stop_after_cancel}", "session")
        body = _prompt_body(
            service,
            operation=f"prompt-{stop_after_cancel}",
            invocation="invocation",
        )
        body["execution_timeout_s"] = 0.01
        record, _ = await service.admit_prompt(
            "session", PromptRequest.model_validate(body)
        )
        return (await service.wait_for_operation(record)).state

    assert await run_case("cancelled") == "timed_out"
    assert await run_case("end_turn") == "in_doubt"


@pytest.mark.asyncio
async def test_unsettled_deadline_quarantines_and_aborts_before_lock_release(
    tmp_path: Path,
) -> None:
    """ADI-05/10: grace expiry kills residual ACP work before any later dispatch."""
    fake = FakeDirectAcpClient()
    fake.block_prompts = True
    fake.ignore_cancel = True
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(cancellation_grace_s=0.01),
    )
    await _settle_creation(service, "create", "session")
    body = _prompt_body(service, operation="prompt", invocation="invocation")
    body["execution_timeout_s"] = 0.01
    record, _ = await service.admit_prompt(
        "session", PromptRequest.model_validate(body)
    )
    view = await service.wait_for_operation(record)

    assert view.state == "in_doubt"
    assert view.error == {
        "code": "deadline_settlement_unknown",
        "message": "ACP prompt did not settle after cancellation grace",
    }
    assert fake.abort_count == 1
    assert len(fake.prompts) == 1
    with pytest.raises(DirectGenerationMismatch, match="managed restart is required"):
        _ = service.capabilities


@pytest.mark.asyncio
async def test_spontaneous_cancelled_stop_is_cancelled_and_session_is_not_reused(
    tmp_path: Path,
) -> None:
    """ADI-10: spontaneous cancellation is truthful and poisons reuse."""
    fake = FakeDirectAcpClient()
    fake.stop_reason = "cancelled"
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
    )
    await _settle_creation(service, "create", "session")
    record, _ = await service.admit_prompt(
        "session",
        PromptRequest.model_validate(
            _prompt_body(service, operation="prompt", invocation="invocation")
        ),
    )
    assert (await service.wait_for_operation(record)).state == "cancelled"
    later = PromptRequest.model_validate(
        _prompt_body(service, operation="later", invocation="later")
    )
    with pytest.raises(Exception, match="not reusable"):
        await service.admit_prompt("session", later)


@pytest.mark.asyncio
async def test_evidence_limit_cancels_and_settles_before_terminal_result(
    tmp_path: Path,
) -> None:
    """ADI-08/10/15: retained evidence overflow cancels ACP and never truncates."""
    fake = FakeDirectAcpClient()
    fake.updates = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tool-before-overflow",
            "title": "observed tool activity",
        },
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "response exceeds five bytes"},
        }
    ]
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(max_response_bytes=5),
    )
    await _settle_creation(service, "create", "session")
    record, _ = await service.admit_prompt(
        "session",
        PromptRequest.model_validate(
            _prompt_body(service, operation="prompt", invocation="invocation")
        ),
    )
    view = await service.wait_for_operation(record)

    assert view.state == "failed"
    assert view.error == {
        "code": "evidence_limit",
        "message": "ACP evidence exceeded a negotiated limit",
    }
    assert fake.cancelled == ["backend-1"]
    assert view.result is not None
    retained = view.result["retained_evidence"]
    assert retained["observed_tool_call_ids"] == ["tool-before-overflow"]
    assert retained["tool_activity_complete"] is True
    assert retained["effect_evidence"] == "unavailable"
    assert [event["sequence"] for event in retained["ordered_events"]] == [0, 1]
    bounded_chunk = retained["ordered_events"][1]["raw"]
    assert len(bounded_chunk["content"]["text"].encode()) <= 5
    assert bounded_chunk["retention"]["boundedPrefix"] is True
    assert "response exceeds five bytes" not in repr(retained)


@pytest.mark.asyncio
async def test_create_deadline_and_upstream_errors_are_bounded_and_sanitized(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADI-03/08/10: session creation has a deadline and leaks no ACP error text."""
    blocked = FakeDirectAcpClient()
    blocked.block_create = True
    service = DirectService(
        blocked,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(session_creation_timeout_s=0.01),
    )
    request = CreateSessionRequest.model_validate(
        _create_body(service, operation="blocked-create", session="blocked")
    )
    record, _ = await service.admit_create(request)
    timed = await service.wait_for_operation(record)
    assert timed.state == "in_doubt"
    assert "private" not in str(timed.error)

    private_error = "T122-PRIVATE-API-KEY-AND-TRANSPORT-DETAILS"
    failing = FakeDirectAcpClient()
    failing.create_error = RuntimeError(private_error)
    failed_service = DirectService(
        failing,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
    )
    failed_request = CreateSessionRequest.model_validate(
        _create_body(failed_service, operation="failed-create", session="failed")
    )
    failed_record, _ = await failed_service.admit_create(failed_request)
    failed = await failed_service.wait_for_operation(failed_record)
    assert failed.state == "in_doubt"
    assert failed.error == {
        "code": "session_creation_in_doubt",
        "message": "ACP session creation outcome is uncertain",
    }
    assert failing.abort_count == 1
    assert private_error not in caplog.text
    with pytest.raises(
        DirectGenerationMismatch,
        match="managed restart is required",
    ):
        await failed_service.admit_create(
            CreateSessionRequest.model_validate(
                _create_body(
                    failed_service,
                    operation="later-create",
                    session="later",
                )
            )
        )


@pytest.mark.asyncio
async def test_known_model_binding_rejection_fails_without_quarantining_generation(
    tmp_path: Path,
) -> None:
    """A settled selector rejection is failed, not uncertain or reusable."""

    fake = FakeDirectAcpClient()
    fake.create_error = ModelAcknowledgementError("private selector detail")
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
    )
    request = CreateSessionRequest.model_validate(
        _create_body(service, operation="rejected-create", session="rejected")
    )

    record, _ = await service.admit_create(request)
    failed = await service.wait_for_operation(record)

    assert failed.state == "failed"
    assert failed.error == {
        "code": "session_configuration_failed",
        "message": "ACP did not settle the requested session configuration",
    }
    assert service._generation.sessions["rejected"].state == "non_reusable"
    prompt = PromptRequest.model_validate(
        _prompt_body(
            service,
            operation="rejected-prompt",
            invocation="rejected-invocation",
        )
    )
    with pytest.raises(DirectConflict, match="not reusable"):
        await service.admit_prompt("rejected", prompt)
    assert fake.abort_count == 0
    assert fake.prompts == []
    assert service.capabilities.continuity_generation_id == service.continuity_generation_id


@pytest.mark.asyncio
async def test_actual_chunked_request_bytes_are_bounded_without_content_length() -> None:
    """ADI-15: missing or lying Content-Length cannot bypass actual byte limits."""
    reached_downstream = False

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        nonlocal reached_downstream
        reached_downstream = True

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=5)
    chunks = iter(
        (
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        )
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(chunks)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-length", b"1")],
        },
        receive,
        send,
    )

    assert reached_downstream is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_far_over_limit_first_chunk_is_rejected_before_retention() -> None:
    """ADI-15: projected size is checked before a hostile chunk is retained."""

    reached_downstream = False

    async def downstream(scope: Any, receive: Any, send: Any) -> None:
        nonlocal reached_downstream
        reached_downstream = True

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    calls = 0

    async def receive() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "type": "http.request",
            "body": b"x" * 1_000_000,
            "more_body": False,
        }

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
        receive,
        send,
    )

    assert calls == 1
    assert reached_downstream is False
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_after_effect_transport_loss_is_in_doubt_and_quarantines_generation(
    tmp_path: Path,
) -> None:
    """ADI-10/11: accepted prompt loss is never failed/retried or left reusable."""
    fake = FakeDirectAcpClient()
    fake.lose_transport_after_updates = True
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        continuity_generation_id="transport-generation",
    )
    await _settle_creation(service, "create", "session")
    record, _ = await service.admit_prompt(
        "session",
        PromptRequest.model_validate(
            _prompt_body(service, operation="prompt", invocation="invocation")
        ),
    )
    view = await service.wait_for_operation(record)

    assert view.state == "in_doubt"
    assert view.error == {
        "code": "continuity_lost",
        "message": "ACP continuity generation was lost",
    }
    assert "private" not in str(view.error)
    with pytest.raises(DirectGenerationMismatch, match="managed restart is required"):
        _ = service.capabilities


@pytest.mark.asyncio
async def test_cancel_send_failure_quarantines_before_any_later_dispatch(
    tmp_path: Path,
) -> None:
    """ADI-05/10: nominally-live uncertain cancellation revokes the generation."""

    fake = FakeDirectAcpClient()
    fake.block_prompts = True
    fake.cancel_error = ConnectionError("private cancel-send detail")
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(max_sessions=2, max_operations=16),
        continuity_generation_id="uncertain-generation",
    )
    await _settle_creation(service, "create-one", "session-one")
    await _settle_creation(service, "create-two", "session-two")
    body = _prompt_body(
        service, operation="uncertain-prompt", invocation="invocation-one"
    )
    body["execution_timeout_s"] = 0.01
    record, _ = await service.admit_prompt(
        "session-one", PromptRequest.model_validate(body)
    )

    view = await service.wait_for_operation(record)

    assert view.state == "in_doubt"
    assert view.error == {
        "code": "prompt_in_doubt",
        "message": "ACP prompt outcome is uncertain",
    }
    assert "private" not in repr(view.error)
    assert fake.abort_count == 1
    assert len(fake.prompts) == 1
    later = PromptRequest.model_validate(
        _prompt_body(
            service,
            operation="must-not-dispatch",
            invocation="invocation-two",
        )
    )
    with pytest.raises(DirectGenerationMismatch):
        await service.admit_prompt("session-two", later)
    assert len(fake.prompts) == 1


@pytest.mark.asyncio
async def test_hanging_manual_cancel_send_is_bounded_and_quarantines(
    tmp_path: Path,
) -> None:
    """ADI-10/15: notification drain cannot hang cancellation or prompt lock."""

    fake = FakeDirectAcpClient()
    fake.block_prompts = True
    fake.cancel_hangs = True
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(
            max_sessions=1,
            max_operations=8,
            cancellation_grace_s=0.02,
        ),
        continuity_generation_id="hanging-cancel-generation",
    )
    await _settle_creation(service, "create", "session")
    prompt, _ = await service.admit_prompt(
        "session",
        PromptRequest.model_validate(
            _prompt_body(service, operation="prompt", invocation="invocation")
        ),
    )
    while not fake.prompts:
        await asyncio.sleep(0)
    cancel, _ = await service.admit_cancel(
        CancelRequest(
            protocol_major=1,
            continuity_generation_id=service.continuity_generation_id,
            operation_id="cancel",
            target_operation_id=prompt.operation_id,
        )
    )

    cancel_view = await asyncio.wait_for(
        service.wait_for_operation(cancel), timeout=0.5
    )
    prompt_view = await service.wait_for_operation(prompt)

    assert cancel_view.state == "failed"
    assert prompt_view.state == "in_doubt"
    assert fake.abort_count == 1
    with pytest.raises(DirectGenerationMismatch):
        _ = service.capabilities
