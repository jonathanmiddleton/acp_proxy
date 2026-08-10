"""
Integration tests: run the full proxy against the real copilot-language-server.

These tests assert that the environment has a compatible binary available.
If the binary is not found, the tests FAIL — not skip. A missing binary
means the environment is misconfigured, and skipping would mask that.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import httpx
import pytest

from acp_proxy.__main__ import _direct_child_env
from acp_proxy.client import AcpClient, CallbackPolicy
from acp_proxy.copilot_auth import inject_prior_copilot_oauth
from acp_proxy.direct_protocol import CreateSessionRequest, PromptRequest
from acp_proxy.direct_service import DirectService
from acp_proxy.discovery import BinaryCompatibilityError, find_binary
from acp_proxy.transport import AcpError

REQUIRED_LIVE_MODEL = "gpt-5.3-codex"


def _live_child_env() -> dict[str, str]:
    """Build the same authenticated child environment as direct CLI startup."""

    return _direct_child_env(inject_prior_copilot_oauth(dict(os.environ)))


def _without_token_credentials(source: dict[str, str]) -> dict[str, str]:
    """Remove ambient token credentials so persisted auth cannot mask the test."""

    return {
        key: value
        for key, value in source.items()
        if "TOKEN" not in key.upper()
    }


def _isolated_auth_env(source: dict[str, str], root: Path) -> dict[str, str]:
    """Point every supported credential/config root at a fresh empty tree."""

    locations = {
        "LOCALAPPDATA": root / "local-app-data",
        "APPDATA": root / "app-data",
        "USERPROFILE": root / "user-profile",
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "xdg-config",
        "XDG_DATA_HOME": root / "xdg-data",
        "XDG_STATE_HOME": root / "xdg-state",
        "XDG_CACHE_HOME": root / "xdg-cache",
    }
    env = _direct_child_env(source)
    for name, path in locations.items():
        path.mkdir(parents=True)
        env[name] = str(path)
    return env


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
async def test_acp_client_initialize_and_discover_models(
    binary: str,
    tmp_path: Path,
) -> None:
    """Prove isolated startup fails without injection and succeeds with it."""

    ambient_without_tokens = _without_token_credentials(dict(os.environ))
    unauthenticated_env = _isolated_auth_env(
        ambient_without_tokens,
        tmp_path / "without-token",
    )
    assert "GH_COPILOT_TOKEN" not in unauthenticated_env
    assert "GITHUB_COPILOT_TOKEN" not in unauthenticated_env

    unauthenticated_client = AcpClient(
        binary,
        callback_policy=CallbackPolicy.DIRECT_DENY,
    )
    try:
        await unauthenticated_client.start(env=unauthenticated_env)
        with pytest.raises(AcpError) as exc_info:
            await asyncio.wait_for(
                unauthenticated_client.create_session(os.getcwd()),
                timeout=30.0,
            )
        assert exc_info.value.error_obj.get("code") == -32000
    finally:
        await unauthenticated_client.stop()

    authenticated_source = inject_prior_copilot_oauth(ambient_without_tokens)
    authenticated_env = _isolated_auth_env(
        authenticated_source,
        tmp_path / "with-token",
    )
    assert "GH_COPILOT_TOKEN" not in authenticated_env
    assert "GITHUB_COPILOT_TOKEN" in authenticated_env

    authenticated_client = AcpClient(
        binary,
        callback_policy=CallbackPolicy.DIRECT_DENY,
    )
    try:
        await authenticated_client.start(env=authenticated_env)
        session_id = await authenticated_client.create_session(os.getcwd())

        assert session_id is not None
        assert len(authenticated_client.models) > 0
        assert authenticated_client.default_model is not None
        assert authenticated_client.agent_info["name"] is not None

        model_ids = [model.model_id for model in authenticated_client.models]
        assert len(model_ids) >= 1
    finally:
        await authenticated_client.stop()


@pytest.mark.asyncio
async def test_acp_client_prompt_and_stream(binary: str):
    """Send a prompt and verify streaming response."""
    client = AcpClient(binary)
    try:
        await client.start(env=_live_child_env())
        session_id = await client.create_session(
            os.getcwd(),
            model_id=REQUIRED_LIVE_MODEL,
        )

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
async def test_required_model_prompt(binary: str):
    """Bind gpt-5.3-codex and complete one prompt on the real server."""
    client = AcpClient(binary)
    try:
        await client.start(env=_live_child_env())
        await client.create_session(os.getcwd())
        catalog_default = client.default_model
        assert isinstance(catalog_default, str) and catalog_default
        assert catalog_default != REQUIRED_LIVE_MODEL
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
async def test_meadow_direct_model_binding_and_continuity_probe(binary: str) -> None:
    """Live direct mode binds gpt-5.3-codex and settles two turns."""

    requested_model = REQUIRED_LIVE_MODEL
    client = AcpClient(binary, callback_policy=CallbackPolicy.DIRECT_DENY)
    try:
        await client.start(env=_live_child_env())
        await client.create_session(os.getcwd())  # one non-prompted catalog probe
        catalog_default = client.default_model
        assert isinstance(catalog_default, str) and catalog_default
        assert catalog_default != requested_model
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
