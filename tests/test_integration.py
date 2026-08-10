"""
Integration tests: run the full proxy against the real copilot-language-server.

These tests assert that the environment has a compatible binary available.
If the binary is not found, the tests FAIL — not skip. A missing binary
means the environment is misconfigured, and skipping would mask that.
"""

from __future__ import annotations

import hashlib
import os

import httpx
import pytest

from acp_proxy.__main__ import _direct_child_env
from acp_proxy.client import AcpClient, CallbackPolicy
from acp_proxy.copilot_auth import inject_prior_copilot_oauth
from acp_proxy.direct_protocol import CreateSessionRequest, PromptRequest
from acp_proxy.direct_service import DirectService
from acp_proxy.discovery import BinaryCompatibilityError, find_binary

REQUIRED_LIVE_MODEL = "gpt-5.3-codex"


def _live_child_env() -> dict[str, str]:
    """Build the same authenticated child environment as direct CLI startup."""

    return _direct_child_env(inject_prior_copilot_oauth(dict(os.environ)))


@pytest.fixture(scope="module")
def binary() -> str:
    """Resolve the compatible copilot-language-server binary.

    Fails the test session if no compatible binary is found. This is
    intentional — the environment must have a supported JetBrains Copilot
    plugin installed with its bundled language server.
    """
    try:
        result = find_binary()
    except BinaryCompatibilityError as exc:
        pytest.fail(f"Incompatible copilot-language-server: {exc}")
    assert result is not None, (
        "No compatible copilot-language-server binary found. "
        "The environment must have a supported JetBrains Copilot plugin "
        "installed. Only the binary bundled with that plugin is supported."
    )
    assert os.path.isfile(result), f"Discovered binary path does not exist: {result}"
    assert os.access(result, os.X_OK), f"Discovered binary is not executable: {result}"
    return result


@pytest.mark.asyncio
async def test_acp_client_initialize_and_discover_models(binary: str):
    """Start the ACP client and verify initialization + model discovery."""
    client = AcpClient(binary)
    try:
        await client.start(env=_live_child_env())
        session_id = await client.create_session(os.getcwd())

        assert session_id is not None
        assert len(client.models) > 0
        assert client.default_model is not None
        assert client.agent_info["name"] is not None

        model_ids = [m.model_id for m in client.models]
        assert len(model_ids) >= 1
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_acp_client_prompt_and_stream(binary: str):
    """Send a prompt and verify streaming response."""
    client = AcpClient(binary)
    try:
        await client.start(env=_live_child_env())
        await client.create_session(os.getcwd())
        session_id = (
            await client.create_session_exact(os.getcwd(), REQUIRED_LIVE_MODEL)
        ).session_id

        chunks: list[dict] = []
        async for update in client.prompt(
            session_id,
            [{"role": "user", "content": "Reply with exactly: PROXY_TEST_OK"}],
        ):
            chunks.append(update)

        # Should have at least one message chunk and a done sentinel
        assert len(chunks) >= 2
        done = chunks[-1]
        assert done.get("done") is True
        assert done.get("stopReason") == "end_turn"

        # Collect all text from message chunks
        text = ""
        for c in chunks:
            if c.get("sessionUpdate") == "agent_message_chunk":
                content = c.get("content", {})
                if content.get("type") == "text":
                    text += content.get("text", "")

        assert "PROXY_TEST_OK" in text
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_full_proxy_http_roundtrip(binary: str):
    """Start the full proxy and make an HTTP request against it."""
    from acp_proxy.server import create_app

    client = AcpClient(binary)
    try:
        await client.start(env=_live_child_env())
        await client.create_session(os.getcwd())

        app = create_app(client, os.getcwd())

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http:
            # Test /v1/models
            resp = await http.get("/v1/models")
            assert resp.status_code == 200
            models = resp.json()
            assert len(models["data"]) > 0

            # Test non-streaming completion
            resp = await http.post(
                "/v1/chat/completions",
                json={
                    "model": REQUIRED_LIVE_MODEL,
                    "messages": [
                        {"role": "user", "content": "Reply with exactly: HTTP_OK"}
                    ],
                    "stream": False,
                },
                timeout=30.0,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["choices"][0]["finish_reason"] == "stop"
            assert "HTTP_OK" in data["choices"][0]["message"]["content"]
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_exact_required_model_prompt(binary: str):
    """Verify exact gpt-5.3-codex selection without spending another model call."""
    client = AcpClient(binary)
    try:
        await client.start(env=_live_child_env())
        await client.create_session(os.getcwd())
        available = {model.model_id for model in client.models}
        assert REQUIRED_LIVE_MODEL in available
        descriptor = await client.create_session_exact(
            os.getcwd(), REQUIRED_LIVE_MODEL
        )
        assert descriptor.model_id == REQUIRED_LIVE_MODEL

        text = ""
        async for update in client.prompt(
            descriptor.session_id,
            [
                {
                    "role": "user",
                    "content": "Reply with exactly: EXACT_MODEL_OK",
                }
            ],
        ):
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    text += content.get("text", "")

        assert "EXACT_MODEL_OK" in text
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_meadow_direct_exact_model_and_continuity_probe(binary: str) -> None:
    """Live ADI-03/06/08: exact gpt-5.3-codex direct session settles twice."""

    requested_model = REQUIRED_LIVE_MODEL
    client = AcpClient(binary, callback_policy=CallbackPolicy.DIRECT_DENY)
    try:
        await client.start(env=_live_child_env())
        catalog_session_id = await client.create_session(
            os.getcwd()
        )  # one non-prompted catalog probe
        catalog_default = client.default_model
        assert isinstance(catalog_default, str) and catalog_default
        assert (
            await client.acknowledge_session_model(
                catalog_session_id,
                catalog_default,
            )
            == catalog_default
        )
        assert requested_model in {model.model_id for model in client.models}
        service = DirectService(
            client,
            cwd=os.getcwd(),
            launch_secret="integration-test-launch-secret-000000000000",
            execution_authority="trusted-host",
            continuity_generation_id="integration-generation",
        )
        stable = ""
        stable_digest = hashlib.sha256(stable.encode()).hexdigest()
        create, _ = await service.admit_create(
            CreateSessionRequest(
                protocol_major=1,
                continuity_generation_id=service.continuity_generation_id,
                operation_id="live-create",
                logical_session_id="live-session",
                expected_canonical_workspace=service.canonical_workspace,
                actor_ref="live-probe",
                title="Live probe",
                model_id=requested_model,
                stable_instruction_digest=stable_digest,
            )
        )
        created = await service.wait_for_operation(create)
        assert created.state == "completed"

        async def submit(
            *, operation_id: str, invocation_id: str, phase: str
        ) -> dict:
            contract = "Return only the requested probe token as plain text."
            kwargs = {
                "protocol_major": 1,
                "continuity_generation_id": service.continuity_generation_id,
                "operation_id": operation_id,
                "invocation_id": invocation_id,
                "phase": phase,
                "stable_instruction_digest": stable_digest,
                "output_contract_digest": hashlib.sha256(
                    contract.encode()
                ).hexdigest(),
                "execution_timeout_s": 120,
                "prompt": (
                    "Reply with exactly DIRECT_CONTINUITY_OK.\n\n"
                    "## Legal Typed Routes\nNo routes are available."
                ),
                "output_contract": contract,
            }
            if phase == "initial":
                kwargs["stable_instructions"] = stable
            request = PromptRequest.model_validate(kwargs)
            record, _ = await service.admit_prompt("live-session", request)
            return (await service.wait_for_operation(record)).model_dump(
                mode="json"
            )

        initial = await submit(
            operation_id="live-initial",
            invocation_id="live-invocation-one",
            phase="initial",
        )
        later = await submit(
            operation_id="live-later",
            invocation_id="live-invocation-two",
            phase="invocation",
        )

        assert initial["state"] == "completed"
        assert later["state"] == "completed"
        assert initial["result"]["model_id"] == requested_model
        assert later["result"]["model_id"] == requested_model
        assert "DIRECT_CONTINUITY_OK" in initial["result"]["response_text"]
        assert "DIRECT_CONTINUITY_OK" in later["result"]["response_text"]
        assert initial["result"]["instruction_submission"] == "submitted_once"
        assert later["result"]["instruction_submission"] == (
            "not_resubmitted_same_session"
        )
    finally:
        await client.stop()
