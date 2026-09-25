"""Production-boundary tests for the authenticated Meadow direct router."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path

import pytest
from starlette.types import Message, Scope, Receive, Send
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from meadow_bridge.native_types import (
    AllocatedBinding,
    ConversationBinding,
    NativeBinding,
    NativeCancelled,
    NativeCompleted,
    NativeEvent,
    NativeEffectObservation,
    NativePermissionObservation,
    NativeFailed,
    NativeModel,
    NativeObservation,
    NativeServerInfo,
    NativeTerminal,
    NativeToolObservation,
    NativeUnsettledError,
)
from meadow_bridge.permission_policy import PermissionPolicy
from meadow_bridge.json_types import JsonObject, json_text, json_object

from meadow_bridge.direct_protocol import (
    CancelRequest,
    CreateSessionRequest,
    DirectLimits,
    OperationView,
    PromptRequest,
    PromptResult,
    RetireSessionRequest,
)
from meadow_bridge.direct_server import RequestBodyLimitMiddleware, _bounded_response, create_direct_app
from meadow_bridge.direct_service import DirectGenerationMismatch, DirectService
from meadow_bridge.direct_state import DirectConflict, DirectLimitExceeded

TOKEN = "t" * 48


class FakeNativeClient:
    """Typed native boundary double; lifecycle and reconciliation remain production code."""

    def __init__(self) -> None:
        self.models: tuple[NativeModel, ...] = (NativeModel("gpt-5.3-codex", "GPT-5.3 Codex"),)
        self.server_info = NativeServerInfo("fake-copilot", "1.0")
        self.created: list[tuple[str, str]] = []
        self.bindings: dict[str, NativeBinding] = {}
        self.prompts: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.retired: list[str] = []
        self.release_prompt = asyncio.Event()
        self.block_prompts = False
        self.create_error: Exception | None = None
        self.turn_error: Exception | None = None
        self.updates: list[JsonObject] = [
            {"kind": "native.progress.report", "reply": '{"messages": []}'}
        ]
        self.stop_reason = "completed"
        self.cancel_stop_reason = "cancelled"
        self.active_prompts = 0
        self.max_active_prompts = 0
        self.is_alive = True
        self.lose_transport_after_updates = False
        self.ignore_cancel = False
        self.cancel_error: Exception | None = None
        self.cancel_hangs = False
        self.abort_count = 0
        self.evidence_overflow = False
        self.release_stop = asyncio.Event()
        self.block_stop = False
        self.stop_started = asyncio.Event()

    def allocate_session(
        self, logical_id: str, cwd: str, model_id: str, policy: PermissionPolicy
    ) -> None:
        if self.create_error is not None:
            raise self.create_error
        self.created.append((cwd, model_id))
        binding = AllocatedBinding(logical_id, model_id)
        self.bindings[logical_id] = binding

    def binding(self, logical_id: str) -> NativeBinding:
        return self.bindings[logical_id]

    async def run_turn(
        self,
        logical_id: str,
        text: str,
        *,
        timeout_s: float,
        event_byte_limit: int,
        response_byte_limit: int,
    ) -> NativeTerminal:
        binding = self.bindings[logical_id]
        if isinstance(binding, AllocatedBinding):
            bound = sum(
                isinstance(item, ConversationBinding) for item in self.bindings.values()
            )
            binding = ConversationBinding(
                logical_id, binding.model_id, f"backend-{bound + 1}", "turn-1"
            )
            self.bindings[logical_id] = binding
        self.prompts.append((logical_id, text))
        self.active_prompts += 1
        self.max_active_prompts = max(self.max_active_prompts, self.active_prompts)
        try:
            if self.block_prompts:
                await self.release_prompt.wait()
            events = tuple(
                NativeEvent(str(update["kind"]), json_text(update))
                for update in self.updates
            )
            tools = tuple(
                NativeToolObservation(
                    str(update["toolCallId"]), "test", "complete", "server"
                )
                for update in self.updates
                if "toolCallId" in update
            )
            observation = NativeObservation(
                binding,
                "".join(str(update.get("reply", "")) for update in self.updates),
                events,
                tools,
                (),
                (),
                not self.evidence_overflow,
            )
            if self.lose_transport_after_updates:
                self.is_alive = False
                raise NativeUnsettledError(
                    "private transport detail after accepted prompt", observation
                )
            if self.turn_error is not None:
                raise self.turn_error
            reason = (
                self.cancel_stop_reason
                if logical_id in self.cancelled
                else self.stop_reason
            )
            if self.evidence_overflow:
                await self.cancel_session(logical_id)
                return NativeCancelled(observation, "evidence_limit")
            if reason == "completed":
                return NativeCompleted(observation)
            if reason == "cancelled":
                return NativeCancelled(observation, "cancelled")
            return NativeFailed(observation, "private native failure detail")
        finally:
            self.active_prompts -= 1

    async def cancel_session(self, logical_id: str) -> None:
        if self.cancel_hangs:
            await asyncio.Event().wait()
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(logical_id)
        if not self.ignore_cancel:
            self.release_prompt.set()

    async def retire_session(self, logical_id: str) -> None:
        self.retired.append(logical_id)

    async def stop(self) -> None:
        self.stop_started.set()
        if self.block_stop:
            await self.release_stop.wait()
        self.abort_count += 1
        self.is_alive = False
        self.release_prompt.set()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _create_body(
    service: DirectService, *, operation: str, session: str
) -> dict[str, object]:
    stable = "stable Meadow instructions"
    return {
        "protocol_major": 2,
        "continuity_generation_id": service.continuity_generation_id,
        "operation_id": operation,
        "logical_session_id": session,
        "expected_canonical_workspace": service.canonical_workspace,
        "actor_ref": "developer",
        "title": "Developer",
        "model_id": "gpt-5.3-codex",
        "stable_instruction_digest": hashlib.sha256(stable.encode()).hexdigest(),
        "permission_policy": {"version": 1, "mode": "allow_all"},
    }


def _prompt_body(
    service: DirectService,
    *,
    operation: str,
    invocation: str,
    phase: str = "initial",
) -> dict[str, object]:
    stable = "stable Meadow instructions"
    body: dict[str, object] = {
        "protocol_major": 2,
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
def direct_boundary(tmp_path: Path) -> tuple[DirectService, FakeNativeClient, FastAPI]:
    fake = FakeNativeClient()
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        limits=DirectLimits(max_sessions=8, max_operations=32),
        continuity_generation_id="generation-test",
    )
    return service, fake, create_direct_app(service)


@pytest.mark.asyncio
async def test_session_mapping_capacity_retains_retired_tombstones(
    tmp_path: Path,
) -> None:
    """ADI-03/15: retirement never permits generation-local identity reuse."""

    fake = FakeNativeClient()
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
            protocol_major=2,
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
    direct_boundary: tuple[DirectService, FakeNativeClient, FastAPI],
) -> None:
    """ADI-02/08/15: capability evidence is exact and never public."""
    service, fake, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        denied = await client.get("/meadow/v2/capabilities")
        accepted = await client.get("/meadow/v2/capabilities", headers=_auth())
        repeated = await client.get("/meadow/v2/capabilities", headers=_auth())

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert repeated.json() == accepted.json()
    payload = accepted.json()
    assert payload["continuity_generation_id"] == service.continuity_generation_id
    assert payload["protocol"] == "meadow-bridge-direct"
    assert "consumer_mode" not in payload
    assert payload["model_ids"] == ["gpt-5.3-codex"]
    assert payload["features"]["native_output_schema"] is False
    assert payload["features"]["request_scoped_tool_activity"] is True
    assert payload["features"]["effect_observation"] is True
    assert payload["features"]["usage_reporting"] is False
    assert payload["execution_authority"]["terminal_callbacks"] is True
    assert payload["execution_authority"]["permission_callbacks"] is True
    assert fake.created == []
    assert fake.prompts == []


@pytest.mark.asyncio
async def test_session_identity_is_explicit_idempotent_and_isolated(
    direct_boundary: tuple[DirectService, FakeNativeClient, FastAPI],
) -> None:
    """ADI-03/04: explicit IDs map once and identical prompts cannot collide."""
    service, fake, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        first_body = _create_body(service, operation="create-one", session="one")
        first = await client.post(
            "/meadow/v2/sessions", json=first_body, headers=_auth()
        )
        duplicate = await client.post(
            "/meadow/v2/sessions", json=first_body, headers=_auth()
        )
        second = await client.post(
            "/meadow/v2/sessions",
            json=_create_body(service, operation="create-two", session="two"),
            headers=_auth(),
        )

    assert first.status_code == duplicate.status_code == second.status_code == 200
    assert first.json()["result"]["backend_session_id"] is None
    assert first.json()["result"]["binding_state"] == "allocated"
    assert duplicate.json() == first.json()
    assert second.json()["result"]["backend_session_id"] is None
    assert fake.prompts == []
    assert len(fake.created) == 2


@pytest.mark.asyncio
async def test_instruction_contract_layers_are_not_replayed_on_correction(
    direct_boundary: tuple[DirectService, FakeNativeClient, FastAPI],
) -> None:
    """ADI-06/07/08: stable/current/schema layers occur once; repair is a delta."""
    service, fake, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        await client.post(
            "/meadow/v2/sessions",
            json=_create_body(service, operation="create", session="session"),
            headers=_auth(),
        )
        initial = await client.post(
            "/meadow/v2/sessions/session/requests",
            json=_prompt_body(service, operation="initial", invocation="invocation"),
            headers=_auth(),
        )
        correction = await client.post(
            "/meadow/v2/sessions/session/requests",
            json=_prompt_body(
                service,
                operation="correction",
                invocation="invocation",
                phase="correction",
            ),
            headers=_auth(),
        )

    assert initial.status_code == correction.status_code == 200
    first_text = fake.prompts[0][1]
    repair_text = fake.prompts[1][1]
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

    fake = FakeNativeClient()
    fake.updates = [
        {"kind": "native.progress.report", "inputTokens": True},
        {"kind": "native.progress.report", "outputTokens": -1},
        {"kind": "native.progress.report", "totalTokens": "malformed"},
        {"kind": "native.progress.report", "sessionInfo": [False, -2]},
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
    assert [
        event.raw for event in PromptResult.model_validate(view.result).events
    ] == fake.updates


@pytest.mark.asyncio
async def test_busy_session_rejects_second_prompt_before_dispatch(
    direct_boundary: tuple[DirectService, FakeNativeClient, FastAPI],
) -> None:
    """ADI-05: one session has one in-flight native prompt and one event owner."""
    service, fake, app = direct_boundary
    fake.block_prompts = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        await client.post(
            "/meadow/v2/sessions",
            json=_create_body(service, operation="create", session="session"),
            headers=_auth(),
        )
        first_task = asyncio.create_task(
            client.post(
                "/meadow/v2/sessions/session/requests",
                json=_prompt_body(service, operation="first", invocation="invocation"),
                headers=_auth(),
            )
        )
        for _ in range(50):
            if fake.prompts:
                break
            await asyncio.sleep(0.01)
        second = await client.post(
            "/meadow/v2/sessions/session/requests",
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
    direct_boundary: tuple[DirectService, FakeNativeClient, FastAPI],
) -> None:
    """ADI-08/09: sanitized permission denial remains in the prompt envelope."""
    service, fake, app = direct_boundary
    fake.updates = [
        {
            "kind": "native.client_tool.confirmation",
            "outcome": "allowed",
            "offeredKinds": ["allow_once"],
        },
        {
            "kind": "native.progress.report",
            "reply": "done",
        },
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        await client.post(
            "/meadow/v2/sessions",
            json=_create_body(service, operation="create", session="session"),
            headers=_auth(),
        )
        response = await client.post(
            "/meadow/v2/sessions/session/requests",
            json=_prompt_body(service, operation="prompt", invocation="invocation"),
            headers=_auth(),
        )

    result = response.json()["result"]
    assert [event["sequence"] for event in result["events"]] == [0, 1]
    assert result["permission_evidence"] == {
        "availability": "observed",
        "events": [result["events"][0]],
        "decisions": [],
    }


@pytest.mark.asyncio
async def test_long_admission_diagnostic_uses_negotiated_response_budget(tmp_path: Path) -> None:
    """The reserved overflow-error size is not a second diagnostic cutoff."""
    fake = FakeNativeClient()
    fake.models = tuple(NativeModel(f"model-{index:04d}", "Model") for index in range(500))
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host",
        limits=DirectLimits(max_http_response_bytes=16384),
    )
    async with AsyncClient(
        transport=ASGITransport(app=create_direct_app(service)), base_url="http://direct.test"
    ) as client:
        response = await client.post(
            "/meadow/v2/sessions", headers=_auth(),
            json=_create_body(service, operation="create", session="session"),
        )
    assert 4096 < len(response.content) <= 16384
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "model-0000" in response.json()["error"]["message"]
    assert "model-0499" in response.json()["error"]["message"]
    assert fake.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("http_limit", [8192, 65536])
async def test_http_limit_counts_complete_encoded_receipts_without_changing_settlement(
    tmp_path: Path, http_limit: int,
) -> None:
    """Delivery bounds include every receipt copy and leave execution replay-safe."""
    receipt: JsonObject = {"stdout": "Ω\"\\" * 1000}

    class ReceiptClient(FakeNativeClient):
        async def run_turn(
            self, logical_id: str, text: str, *, timeout_s: float,
            event_byte_limit: int, response_byte_limit: int,
        ) -> NativeTerminal:
            terminal = await super().run_turn(
                logical_id, text, timeout_s=timeout_s,
                event_byte_limit=event_byte_limit, response_byte_limit=response_byte_limit,
            )
            return NativeCompleted(replace(
                terminal.observation,
                events=(NativeEvent("native.client_tool.result", json_text({
                    "toolCallId": "tool", "result": receipt,
                })),),
                tools=(NativeToolObservation("tool", "workspace_run_command", "completed", "bridge"),),
                effects=(NativeEffectObservation("tool", json_text(receipt)),),
            ))

    fake = ReceiptClient()
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host",
        limits=DirectLimits(max_http_response_bytes=http_limit),
    )
    await _settle_creation(service, "create", "session")
    request = _prompt_body(service, operation="prompt", invocation="invocation")
    async with AsyncClient(
        transport=ASGITransport(app=create_direct_app(service)), base_url="http://direct.test"
    ) as client:
        response = await client.post(
            "/meadow/v2/sessions/session/requests", json=request, headers=_auth(),
        )
        record = service.operation(
            "prompt", protocol_major=2, generation_id=service.continuity_generation_id,
        )
        view = service.operation_view(record)
        encoded = JSONResponse(view.model_dump(mode="json")).body
        replay = await client.post(
            "/meadow/v2/sessions/session/requests", json=request, headers=_auth(),
        )
        status = await client.get(
            "/meadow/v2/operations/prompt", headers=_auth(),
            params={"protocol_major": 2, "continuity_generation_id": service.continuity_generation_id},
        )
    assert view.state == "completed"
    retained = PromptResult.model_validate(view.result)
    assert retained.effects[0].receipt == receipt
    assert retained.events[0].raw == {"toolCallId": "tool", "result": receipt}
    assert retained.tool_evidence.events == retained.effect_events == retained.events
    assert len(fake.prompts) == 1
    assert response.content == replay.content == status.content
    assert len(response.content) <= http_limit
    if len(encoded) <= http_limit:
        assert response.status_code == 200
        assert response.content == encoded
    else:
        assert response.status_code == 500
        assert response.json()["error"] == {
            "code": "response_too_large",
            "message": "encoded response exceeds negotiated byte limit",
            "operation_id": "prompt",
            "operation_state": "completed",
            "actual_bytes": len(encoded),
            "max_bytes": http_limit,
        }


@pytest.mark.parametrize("headroom", [-1, 0])
def test_http_boundary_uses_exact_bytes_for_failed_operation_envelope(headroom: int) -> None:
    """An existing non-success status is retained only when its entire body fits."""
    view = OperationView(
        operation_id="retire", kind="retire_session", state="failed",
        error={"code": "retirement_failed", "detail": "Ω\"\\" * 1000},
    )
    content = view.model_dump(mode="json")
    encoded = JSONResponse(content, status_code=409).body
    limit = len(encoded) + headroom
    response = _bounded_response(
        content, max_bytes=limit, status_code=409, operation=view,
    )
    assert len(response.body) <= limit
    assert JSONResponse(view.model_dump(mode="json"), status_code=409).body == encoded
    if headroom == 0:
        assert response.status_code == 409
        assert response.body == encoded
    else:
        assert response.status_code == 500


@pytest.mark.asyncio
async def test_removed_adapter_has_no_route(
    direct_boundary: tuple[DirectService, FakeNativeClient, FastAPI],
) -> None:
    """Removed adapter requests cannot dispatch native work."""
    _, _, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions", json={"messages": []}, headers=_auth()
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_output_contract_digest_and_active_invocation_are_fail_closed(
    direct_boundary: tuple[DirectService, FakeNativeClient, FastAPI],
) -> None:
    """ADI-07: contract bytes bind work and only the active invocation accepts deltas."""
    service, fake, app = direct_boundary
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://direct.test"
    ) as client:
        await client.post(
            "/meadow/v2/sessions",
            json=_create_body(service, operation="create", session="session"),
            headers=_auth(),
        )
        wrong = _prompt_body(service, operation="wrong", invocation="first")
        wrong["output_contract_digest"] = "0" * 64
        rejected = await client.post(
            "/meadow/v2/sessions/session/requests", json=wrong, headers=_auth()
        )
        first = await client.post(
            "/meadow/v2/sessions/session/requests",
            json=_prompt_body(service, operation="first", invocation="first"),
            headers=_auth(),
        )
        second = await client.post(
            "/meadow/v2/sessions/session/requests",
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
            "/meadow/v2/sessions/session/requests",
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
    fake = FakeNativeClient()
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
        created = await client.post("/meadow/v2/sessions", json=create, headers=_auth())
        completed = await client.post(
            "/meadow/v2/sessions/session/requests", json=prompt, headers=_auth()
        )

    assert created.status_code == completed.status_code == 200
    assert completed.json()["result"]["instruction_submission"] == "submitted_once"
    assert fake.prompts[0][1].startswith("\n\ncurrent prompt")


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
        FakeNativeClient(),
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
    fake = FakeNativeClient()
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
async def test_queued_cancellation_settles_without_native_dispatch_or_cancel(
    tmp_path: Path,
) -> None:
    """ADI-05/10: queued cancellation is terminal before any native side effect."""
    fake = FakeNativeClient()
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
            protocol_major=2,
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
    fake = FakeNativeClient()
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
            "old-prompt-0", protocol_major=2, generation_id="old-generation"
        )
    with pytest.raises(DirectGenerationMismatch, match="managed restart is required"):
        service.operation(
            "old-prompt-0", protocol_major=2, generation_id=new_generation
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

    fake = FakeNativeClient()
    service = DirectService(
        fake,
        cwd=str(tmp_path),
        launch_secret=TOKEN,
        execution_authority="trusted-host",
        continuity_generation_id="old-generation",
    )
    collector_started = asyncio.Event()
    collector_cancelled = asyncio.Event()

    async def collector() -> NativeTerminal:
        collector_started.set()
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable collector release")
        except asyncio.CancelledError:
            collector_cancelled.set()
            await asyncio.sleep(0)
            raise

    task = asyncio.create_task(collector())
    service._generation.collector_tasks["synthetic-operation"] = task
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
    fake = FakeNativeClient()
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
    assert (
        service.operation(
            "prompt",
            protocol_major=2,
            generation_id=service.continuity_generation_id,
        )
        is first
    )
    assert len(fake.prompts) <= 1
    fake.release_prompt.set()
    assert (await service.wait_for_operation(first)).state == "completed"
    assert len(fake.prompts) == 1


@pytest.mark.asyncio
async def test_deadline_requires_cancelled_stop_reason_for_timed_out_state(
    tmp_path: Path,
) -> None:
    """ADI-10: deadline settlement is timed_out only after native reports cancelled."""

    async def run_case(stop_after_cancel: str) -> str:
        fake = FakeNativeClient()
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
    assert await run_case("completed") == "in_doubt"


@pytest.mark.asyncio
async def test_unsettled_deadline_quarantines_and_aborts_before_lock_release(
    tmp_path: Path,
) -> None:
    """ADI-05/10: grace expiry kills residual native work before any later dispatch."""
    fake = FakeNativeClient()
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
        "message": "native prompt did not settle after cancellation grace",
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
    fake = FakeNativeClient()
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
    """Incomplete native evidence cannot become a completed result or reusable session."""
    fake = FakeNativeClient()
    fake.evidence_overflow = True
    fake.updates = [
        {
            "kind": "native.progress.report",
            "toolCallId": "tool-before-overflow",
            "title": "observed tool activity",
        },
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
        "message": "native evidence exceeded a negotiated limit",
    }
    assert fake.cancelled == ["session"]
    assert view.result is not None
    retained = json_object(view.result["retained_evidence"])
    assert retained["observed_tool_call_ids"] == ["tool-before-overflow"]
    assert retained["tool_activity_complete"] is False
    assert retained["effect_evidence"] == "unavailable"
    events = retained["ordered_events"]
    assert isinstance(events, list)
    assert [json_object(event)["sequence"] for event in events] == [0]
    assert json_object(events[0])["raw"] == fake.updates[0]
    assert retained["events_complete"] is False
    with pytest.raises(DirectConflict, match="not reusable"):
        await service.admit_prompt(
            "session",
            PromptRequest.model_validate(
                _prompt_body(service, operation="later", invocation="later")
            ),
        )


@pytest.mark.asyncio
async def test_local_allocation_rejection_is_failed_without_quarantining_generation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deferred native creation makes allocation errors pre-inference and deterministic."""
    fake = FakeNativeClient()
    private_error = "PRIVATE-API-KEY-AND-TRANSPORT-DETAILS"
    fake.create_error = ValueError(private_error)
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host"
    )
    request = CreateSessionRequest.model_validate(
        _create_body(service, operation="create", session="session")
    )
    record, _ = await service.admit_create(request)
    failed = await service.wait_for_operation(record)
    assert failed.state == "failed"
    assert failed.error == {
        "code": "session_allocation_failed",
        "message": "native session allocation failed",
    }
    assert private_error not in caplog.text
    assert fake.prompts == []
    assert fake.abort_count == 0
    assert (
        service.capabilities.continuity_generation_id
        == service.continuity_generation_id
    )


@pytest.mark.asyncio
async def test_failed_first_turn_preserves_binding_without_claiming_instruction_submission(
    tmp_path: Path,
) -> None:
    """Native creation is observed independently of successful initial instructions."""
    fake = FakeNativeClient()
    fake.stop_reason = "failed"
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host"
    )
    await _settle_creation(service, "create", "session")
    record, _ = await service.admit_prompt(
        "session",
        PromptRequest.model_validate(
            _prompt_body(service, operation="first", invocation="invocation")
        ),
    )
    view = await service.wait_for_operation(record)
    assert view.state == "failed"
    assert view.result is not None
    assert view.result["backend_session_id"] == "backend-1"
    assert "instruction_submission" not in view.result
    assert fake.abort_count == 0
    with pytest.raises(DirectConflict, match="not reusable"):
        await service.admit_prompt(
            "session",
            PromptRequest.model_validate(
                _prompt_body(service, operation="second", invocation="other")
            ),
        )
    retire, _ = await service.admit_retire(
        RetireSessionRequest(
            protocol_major=2,
            continuity_generation_id=service.continuity_generation_id,
            operation_id="retire",
            logical_session_id="session",
        )
    )
    retired = await service.wait_for_operation(retire)
    assert retired.result == {
        "logical_session_id": "session",
        "backend_session_id": "backend-1",
        "backend_close": "destroyed",
    }
    assert fake.retired == ["session"]


@pytest.mark.asyncio
async def test_actual_chunked_request_bytes_are_bounded_without_content_length() -> (
    None
):
    """ADI-15: missing or lying Content-Length cannot bypass actual byte limits."""
    reached_downstream = False

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reached_downstream
        reached_downstream = True

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=5)
    chunks = iter(
        (
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        )
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return next(chunks)

    async def send(message: Message) -> None:
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

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal reached_downstream
        reached_downstream = True

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=8)
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        return {
            "type": "http.request",
            "body": b"x" * 1_000_000,
            "more_body": False,
        }

    sent: list[Message] = []

    async def send(message: Message) -> None:
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
    fake = FakeNativeClient()
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
        "code": "prompt_in_doubt",
        "message": "native prompt outcome is uncertain",
    }
    assert "private" not in str(view.error)
    with pytest.raises(DirectGenerationMismatch, match="managed restart is required"):
        _ = service.capabilities


@pytest.mark.asyncio
async def test_cancel_send_failure_quarantines_before_any_later_dispatch(
    tmp_path: Path,
) -> None:
    """ADI-05/10: nominally-live uncertain cancellation revokes the generation."""

    fake = FakeNativeClient()
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
        "message": "native prompt outcome is uncertain",
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

    fake = FakeNativeClient()
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
            protocol_major=2,
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


@pytest.mark.asyncio
async def test_allocated_retirement_is_local_and_replayed_once(tmp_path: Path) -> None:
    """Retirement before any prompt must never create a native conversation."""
    fake = FakeNativeClient()
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host"
    )
    await _settle_creation(service, "create", "session")
    request = RetireSessionRequest(
        protocol_major=2,
        continuity_generation_id=service.continuity_generation_id,
        operation_id="retire",
        logical_session_id="session",
    )
    first, _ = await service.admit_retire(request)
    duplicate, created = await service.admit_retire(request)
    assert first is duplicate
    assert not created
    result = await service.wait_for_operation(first)
    assert result.result == {
        "logical_session_id": "session",
        "backend_session_id": None,
        "backend_close": "not_created",
    }
    assert fake.prompts == []
    assert fake.retired == ["session"]
    with pytest.raises(DirectConflict, match="not reusable"):
        await service.admit_prompt(
            "session",
            PromptRequest.model_validate(
                _prompt_body(service, operation="prompt", invocation="invocation")
            ),
        )


@pytest.mark.asyncio
async def test_queued_cancel_remains_reusable_after_cancelled_worker_drains(
    tmp_path: Path,
) -> None:
    """Cancelling work before dispatch cannot poison an uninitialized session."""
    fake = FakeNativeClient()
    fake.block_prompts = True
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host"
    )
    await _settle_creation(service, "create-a", "a")
    await _settle_creation(service, "create-b", "b")
    first, _ = await service.admit_prompt(
        "a",
        PromptRequest.model_validate(
            _prompt_body(service, operation="first", invocation="a")
        ),
    )
    while not fake.prompts:
        await asyncio.sleep(0)
    queued, _ = await service.admit_prompt(
        "b",
        PromptRequest.model_validate(
            _prompt_body(service, operation="queued", invocation="b")
        ),
    )
    cancelled, _ = await service.admit_cancel(
        CancelRequest(
            protocol_major=2,
            continuity_generation_id=service.continuity_generation_id,
            operation_id="cancel",
            target_operation_id="queued",
        )
    )
    assert (await service.wait_for_operation(cancelled)).state == "completed"
    assert (await service.wait_for_operation(queued)).state == "cancelled"
    fake.release_prompt.set()
    await service.wait_for_operation(first)
    await asyncio.gather(*tuple(service._generation.execution_tasks))
    retry, _ = await service.admit_prompt(
        "b",
        PromptRequest.model_validate(
            _prompt_body(service, operation="fresh", invocation="fresh")
        ),
    )
    assert (await service.wait_for_operation(retry)).state == "completed"
    assert [logical_id for logical_id, _ in fake.prompts] == ["a", "b"]
    assert fake.cancelled == []


@pytest.mark.asyncio
async def test_generation_loss_does_not_publish_terminal_before_owned_cleanup(
    tmp_path: Path,
) -> None:
    """A lost child does not settle the operation while effect cleanup is pending."""
    fake = FakeNativeClient()
    fake.block_prompts = True
    fake.block_stop = True
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host"
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
    shutdown = asyncio.create_task(service.mark_generation_lost("test loss"))
    await asyncio.wait_for(fake.stop_started.wait(), timeout=1)
    assert not prompt.done.is_set()
    assert fake.active_prompts == 0
    with pytest.raises(DirectGenerationMismatch):
        _ = service.capabilities
    fake.release_stop.set()
    await shutdown
    assert (await service.wait_for_operation(prompt)).state == "in_doubt"
    assert fake.abort_count == 1


@pytest.mark.asyncio
async def test_two_invocations_bind_once_and_submit_stable_instructions_once(
    tmp_path: Path,
) -> None:
    """Stable logical identity survives deferred creation and later invocation layers."""
    fake = FakeNativeClient()
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host"
    )
    create = CreateSessionRequest.model_validate(
        _create_body(service, operation="create", session="session")
    )
    allocation, _ = await service.admit_create(create)
    assert allocation.result is not None
    assert (
        allocation.result["permission_policy_digest"]
        == PermissionPolicy(version=1, mode="allow_all").digest
    )
    assert allocation.result["backend_session_id"] is None
    results: list[PromptResult] = []
    for index, phase in enumerate(("initial", "invocation")):
        request = PromptRequest.model_validate(
            _prompt_body(
                service,
                operation=f"prompt-{index}",
                invocation=f"invocation-{index}",
                phase=phase,
            )
        )
        record, _ = await service.admit_prompt("session", request)
        view = await service.wait_for_operation(record)
        assert view.state == "completed"
        results.append(PromptResult.model_validate(view.result))
    assert [result.backend_session_id for result in results] == [
        "backend-1",
        "backend-1",
    ]
    assert [result.instruction_submission for result in results] == [
        "submitted_once",
        "not_resubmitted_same_session",
    ]
    assert fake.prompts[0][1].count("stable Meadow instructions") == 1
    assert "stable Meadow instructions" not in fake.prompts[1][1]
    assert (
        fake.prompts[1][1]
        == "next prompt\n\n## Legal Typed Routes\nnext route body\n\nnext complete prose contract"
    )


@pytest.mark.asyncio
async def test_first_turn_cancellation_before_binding_uses_logical_identity(
    tmp_path: Path,
) -> None:
    """The cancellation route must work before a conversation ID exists."""

    class PreBindingClient(FakeNativeClient):
        async def run_turn(
            self,
            logical_id: str,
            text: str,
            *,
            timeout_s: float,
            event_byte_limit: int,
            response_byte_limit: int,
        ) -> NativeTerminal:
            self.prompts.append((logical_id, text))
            await self.release_prompt.wait()
            return NativeCancelled(
                NativeObservation(self.binding(logical_id), "", (), (), (), (), True),
                "cancelled before begin",
            )

    fake = PreBindingClient()
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host"
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
            protocol_major=2,
            continuity_generation_id=service.continuity_generation_id,
            operation_id="cancel",
            target_operation_id="prompt",
        )
    )
    assert (await service.wait_for_operation(cancel)).state == "completed"
    result = await service.wait_for_operation(prompt)
    assert result.state == "cancelled"
    assert result.result is not None
    assert result.result["backend_session_id"] is None
    assert fake.cancelled == ["session"]
    assert fake.abort_count == 0


@pytest.mark.asyncio
async def test_retirement_remains_exclusive_until_destroy_settles(
    tmp_path: Path,
) -> None:
    """A pending destroy joins only its original operation and rejects new work."""

    class DelayedRetirementClient(FakeNativeClient):
        def __init__(self) -> None:
            super().__init__()
            self.retirement_started = asyncio.Event()
            self.retirement_release = asyncio.Event()

        async def retire_session(self, logical_id: str) -> None:
            self.retirement_started.set()
            await self.retirement_release.wait()
            await super().retire_session(logical_id)

    fake = DelayedRetirementClient()
    service = DirectService(
        fake, cwd=str(tmp_path), launch_secret=TOKEN, execution_authority="trusted-host"
    )
    await _settle_creation(service, "create", "session")
    request = RetireSessionRequest(
        protocol_major=2,
        continuity_generation_id=service.continuity_generation_id,
        operation_id="retire",
        logical_session_id="session",
    )
    record, _ = await service.admit_retire(request)
    await fake.retirement_started.wait()
    assert not record.done.is_set()
    duplicate, created = await service.admit_retire(request)
    assert duplicate is record and not created
    with pytest.raises(DirectConflict, match="active work"):
        await service.admit_retire(
            request.model_copy(update={"operation_id": "other-retire"})
        )
    with pytest.raises(DirectConflict):
        await service.admit_prompt(
            "session",
            PromptRequest.model_validate(
                _prompt_body(service, operation="prompt", invocation="invocation")
            ),
        )
    fake.retirement_release.set()
    assert (await service.wait_for_operation(record)).state == "completed"
    assert fake.retired == ["session"]


@pytest.mark.asyncio
async def test_cleanup_failure_cannot_be_reported_as_successful_cancellation(
    tmp_path: Path,
) -> None:
    """Failure to settle effects stays uncertain and is repeated to shutdown callers."""

    class FailedCleanupClient(FakeNativeClient):
        async def stop(self) -> None:
            self.abort_count += 1
            raise RuntimeError("private cleanup detail")

    fake = FailedCleanupClient()
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
            protocol_major=2,
            continuity_generation_id=service.continuity_generation_id,
            operation_id="cancel",
            target_operation_id="prompt",
        )
    )
    cancel_result = await asyncio.wait_for(
        service.wait_for_operation(cancel), timeout=1
    )
    assert cancel_result.state == "in_doubt"
    assert cancel_result.error == {
        "code": "cleanup_settlement_unknown",
        "message": "native owned cleanup did not settle",
    }
    assert (await service.wait_for_operation(prompt)).state == "in_doubt"
    with pytest.raises(RuntimeError, match="native owned cleanup did not settle"):
        await service.mark_generation_lost("second shutdown")
    assert fake.abort_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_route", "error_code"),
    [
        ("transport_close", "continuity_lost"),
        ("manual_send_timeout", "cancel_transport_failed"),
        ("manual_grace_timeout", "cancellation_settlement_unknown"),
        ("deadline_send_timeout", "prompt_in_doubt"),
        ("deadline_grace_timeout", "deadline_settlement_unknown"),
    ],
)
async def test_uncertain_turn_preserves_native_observations(
    tmp_path: Path,
    failure_route: str,
    error_code: str,
) -> None:
    """Loss and uncertain cancellation retain evidence after the collector settles."""

    class ObservedFailureClient(FakeNativeClient):
        def __init__(self) -> None:
            super().__init__()
            self.observed = asyncio.Event()

        async def run_turn(
            self,
            logical_id: str,
            text: str,
            *,
            timeout_s: float,
            event_byte_limit: int,
            response_byte_limit: int,
        ) -> NativeTerminal:
            prior = self.binding(logical_id)
            binding = ConversationBinding(
                logical_id,
                prior.model_id,
                "conversation-before-loss",
                "turn-before-loss",
            )
            self.bindings[logical_id] = binding
            policy = PermissionPolicy(version=1, mode="allow_all")
            receipt: JsonObject = {"stdout": "observed output", "returncode": 0}
            observation = NativeObservation(
                binding,
                "observed text",
                (
                    NativeEvent(
                        "native.client_tool.result",
                        json_text(
                            {"toolCallId": "call-before-loss", "result": receipt}
                        ),
                    ),
                ),
                (
                    NativeToolObservation(
                        "call-before-loss", "run_in_terminal", "complete", "bridge"
                    ),
                ),
                (NativePermissionObservation("call-before-loss", True, policy.digest),),
                (NativeEffectObservation("call-before-loss", json_text(receipt)),),
                False,
            )
            self.observed.set()
            try:
                await asyncio.Event().wait()
                raise AssertionError("unreachable turn release")
            except asyncio.CancelledError as exc:
                # Cleanup starts before the collector reports its retained evidence.
                await asyncio.sleep(0)
                raise NativeUnsettledError(
                    "private upstream detail", observation
                ) from exc

    fake = ObservedFailureClient()
    fake.cancel_hangs = failure_route.endswith("send_timeout")
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
    if failure_route.startswith("deadline_"):
        body["execution_timeout_s"] = 0.01
    prompt, _ = await service.admit_prompt(
        "session", PromptRequest.model_validate(body)
    )
    await fake.observed.wait()
    if failure_route == "transport_close":
        await service.mark_generation_lost("transport closure callback")
    elif failure_route.startswith("manual_"):
        cancel, _ = await service.admit_cancel(
            CancelRequest(
                protocol_major=2,
                continuity_generation_id=service.continuity_generation_id,
                operation_id="cancel",
                target_operation_id=prompt.operation_id,
            )
        )
        cancellation = await asyncio.wait_for(service.wait_for_operation(cancel), 1)
        assert cancellation.state == (
            "failed" if failure_route.endswith("send_timeout") else "completed"
        )
    result = await asyncio.wait_for(service.wait_for_operation(prompt), 1)
    assert result.state == "in_doubt"
    assert result.error is not None
    assert result.error["code"] == error_code
    assert result.result is not None
    assert result.result["backend_session_id"] == "conversation-before-loss"
    assert "instruction_submission" not in result.result
    retained = json_object(result.result["retained_evidence"])
    assert retained["events_complete"] is False
    assert retained["calls"] == [
        {
            "tool_call_id": "call-before-loss",
            "name": "run_in_terminal",
            "status": "complete",
            "scope": "bridge",
        }
    ]
    assert retained["decisions"] == [
        {
            "tool_call_id": "call-before-loss",
            "allowed": True,
            "policy_digest": PermissionPolicy(version=1, mode="allow_all").digest,
        }
    ]
    assert retained["effects"] == [
        {
            "tool_call_id": "call-before-loss",
            "receipt": {"stdout": "observed output", "returncode": 0},
        }
    ]
    assert fake.abort_count == 1
