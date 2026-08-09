"""Tests for the OpenAI-compatible HTTP server layer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from acp_proxy.client import ModelInfo
from acp_proxy.server import create_app


class FakeAcpClient:
    """A fake AcpClient for testing the HTTP server without a real subprocess."""

    def __init__(
        self,
        models: list[ModelInfo] | None = None,
        default_model: str = "gpt-4.1",
        prompt_response: str = "Hello from Copilot!",
        stop_reason: str = "end_turn",
    ) -> None:
        self._models = models or [
            ModelInfo(model_id="gpt-4.1", name="GPT-4.1"),
            ModelInfo(model_id="gpt-4o", name="GPT-4o"),
        ]
        self._default_model = default_model
        self._prompt_response = prompt_response
        self._stop_reason = stop_reason
        self._sessions: dict[str, str] = {}
        self._session_counter = 0
        self.last_prompt_messages: list[dict] | None = None

    @property
    def models(self) -> list[ModelInfo]:
        return list(self._models)

    @property
    def default_model(self) -> str | None:
        return self._default_model

    @property
    def agent_info(self) -> dict[str, str | None]:
        return {"name": "FakeAgent", "version": "0.0.1"}

    async def create_session(self, cwd: str, model_id: str | None = None) -> str:
        self._session_counter += 1
        sid = f"fake-session-{self._session_counter}"
        self._sessions[sid] = model_id or self._default_model
        return sid

    async def prompt(
        self, session_id: str, messages: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        self.last_prompt_messages = messages

        # Simulate streaming chunks
        words = self._prompt_response.split(" ")
        for i, word in enumerate(words):
            text = word if i == 0 else " " + word
            yield {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            }
            await asyncio.sleep(0.01)

        yield {"done": True, "stopReason": self._stop_reason}


@pytest.fixture
def fake_client():
    return FakeAcpClient()


@pytest.fixture
def app(fake_client):
    return create_app(fake_client, "/tmp/test")


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_list_models(client, fake_client):
    """GET /v1/models returns the models from the ACP client."""
    resp = await client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    ids = [m["id"] for m in data["data"]]
    assert "gpt-4.1" in ids
    assert "gpt-4o" in ids


@pytest.mark.asyncio
async def test_chat_completion_non_streaming(client, fake_client):
    """POST /v1/chat/completions (non-streaming) returns a complete response."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "gpt-4.1"
    assert len(data["choices"]) == 1
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "Hello from Copilot!"
    assert data["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_chat_completion_streaming(client, fake_client):
    """POST /v1/chat/completions (streaming) returns SSE chunks."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    # Parse SSE events
    body = resp.text
    events = []
    for line in body.split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))

    # Should have content chunks + final chunk
    assert len(events) >= 2
    # First chunk should have content
    assert events[0]["object"] == "chat.completion.chunk"
    assert events[0]["choices"][0]["delta"].get("content") is not None
    # Last chunk should have finish_reason
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_chat_completion_unknown_model_returns_error(client, fake_client):
    """An unknown model ID returns a 404 error, not a silent fallback."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "nonexistent-model",
            "messages": [{"role": "user", "content": "test"}],
            "stream": False,
        },
    )
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"]["code"] == "model_not_found"
    assert "nonexistent-model" in data["error"]["message"]


@pytest.mark.asyncio
async def test_chat_completion_passes_messages(client, fake_client):
    """Messages from the request are forwarded to the ACP client."""
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            "stream": False,
        },
    )
    assert fake_client.last_prompt_messages is not None
    assert len(fake_client.last_prompt_messages) == 2
    assert fake_client.last_prompt_messages[0]["role"] == "system"
    assert fake_client.last_prompt_messages[1]["role"] == "user"


@pytest.mark.asyncio
async def test_health_endpoint(client, fake_client):
    """GET /health returns status and agent info."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["consumer_mode"] == "opencode-legacy"
    assert data["deprecated"] is True
    assert data["agent"]["name"] == "FakeAgent"


@pytest.mark.asyncio
async def test_legacy_mode_rejects_meadow_direct_endpoint(client, fake_client):
    """ADI-12: legacy mode cannot accidentally admit strict direct traffic."""
    response = await client.get("/meadow/v1/capabilities")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "meadow_direct_mode_required"


@pytest.mark.asyncio
async def test_extra_fields_accepted(client, fake_client):
    """Extra fields in the request (like stream_options) are accepted."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "test"}],
            "stream": False,
            "stream_options": {"include_usage": True},
            "temperature": 0.7,
            "top_p": 0.9,
        },
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_system_prompt_injected_on_first_session(fake_client):
    """ADR-003: system prompt is sent as the first turn in a new session.

    The system prompt must be injected and drained before any user content
    reaches the session. Verify that the FakeAcpClient receives the system
    prompt as a separate call before the user's actual message.
    """
    prompt_calls: list[tuple[str, list[dict]]] = []
    original_prompt = fake_client.prompt

    async def tracking_prompt(session_id, messages):
        prompt_calls.append((session_id, messages))
        async for update in original_prompt(session_id, messages):
            yield update

    fake_client.prompt = tracking_prompt

    app = create_app(fake_client, "/tmp/test", system_prompt="You are a test agent.")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        await http.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4.1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )

    # Should have two prompt calls: system prompt injection, then user message
    assert len(prompt_calls) == 2
    # First call is the system prompt
    sys_msgs = prompt_calls[0][1]
    assert len(sys_msgs) == 1
    assert sys_msgs[0]["role"] == "system"
    assert sys_msgs[0]["content"] == "You are a test agent."
    # Second call is the user message
    user_msgs = prompt_calls[1][1]
    assert any(m["role"] == "user" for m in user_msgs)


@pytest.mark.asyncio
async def test_system_prompt_not_injected_on_reused_session(fake_client):
    """ADR-003: system prompt is injected only once per session, not per turn."""
    prompt_calls: list[tuple[str, list[dict]]] = []
    original_prompt = fake_client.prompt

    async def tracking_prompt(session_id, messages):
        prompt_calls.append((session_id, messages))
        async for update in original_prompt(session_id, messages):
            yield update

    fake_client.prompt = tracking_prompt

    app = create_app(fake_client, "/tmp/test", system_prompt="System instructions.")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        # Turn 1
        await http.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4.1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
        # Turn 2 — same conversation (same first user message)
        await http.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4.1",
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                    {"role": "user", "content": "follow up"},
                ],
                "stream": False,
            },
        )

    # Turn 1: system prompt + user message = 2 calls
    # Turn 2: user message only = 1 call (no system prompt re-injection)
    assert len(prompt_calls) == 3
    system_calls = [c for c in prompt_calls if c[1][0].get("role") == "system"]
    assert len(system_calls) == 1


@pytest.mark.asyncio
async def test_no_system_prompt_when_not_configured(fake_client):
    """When no system prompt is provided, no injection occurs."""
    prompt_calls: list[tuple[str, list[dict]]] = []
    original_prompt = fake_client.prompt

    async def tracking_prompt(session_id, messages):
        prompt_calls.append((session_id, messages))
        async for update in original_prompt(session_id, messages):
            yield update

    fake_client.prompt = tracking_prompt

    app = create_app(fake_client, "/tmp/test")  # No system_prompt
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        await http.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4.1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )

    # Only the user message, no system prompt
    assert len(prompt_calls) == 1
    assert prompt_calls[0][1][0]["role"] == "user"


@pytest.mark.asyncio
async def test_session_reuse_per_conversation(client, fake_client):
    """Sessions are identified by (model, hash_of_first_user_message).

    Same model + same first user message = same session (multi-turn).
    Same model + different first user message = different session.
    Different model + same first user message = different session.
    """
    # Turn 1 of conversation A
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 1

    # Turn 2 of conversation A — same first user message, reuses session
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "follow up"},
            ],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 1

    # Conversation B — different first user message, new session
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "different topic"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 2

    # Conversation C — same first msg as A but different model, new session
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 3


@pytest.mark.asyncio
async def test_identical_single_message_gets_new_session(client, fake_client):
    """Repeated single-message requests with the same prompt get separate sessions.

    This prevents scripts and agents that send a static prompt from colliding
    into one ACP session.  Each single-message request after the first creates
    a fresh session because the prior session is already active.
    """
    # First invocation — creates session 1
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "run the tests"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 1

    # Second invocation — same prompt, single message, session 1 is active
    # → must create session 2
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "run the tests"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 2

    # Third invocation — same again → session 3
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "run the tests"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 3


@pytest.mark.asyncio
async def test_multi_turn_reuses_session_after_collision(client, fake_client):
    """A multi-turn continuation reuses the session even after a single-message
    request evicted the previous key holder.

    Sequence:
    1. Single-message "hello" → session 1
    2. Single-message "hello" → session 2 (collision avoidance)
    3. Multi-turn with first_msg "hello" → reuses session 2 (continuation)
    """
    # Turn 1: first conversation
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 1

    # Turn 1 of second conversation: same prompt → new session
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 2

    # Turn 2 of second conversation: multi-message continuation → reuses session 2
    await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4.1",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
                {"role": "user", "content": "follow up"},
            ],
            "stream": False,
        },
    )
    assert fake_client._session_counter == 2


# ---------------------------------------------------------------------------
# _map_stop_reason
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Prompt timeout error handling
# ---------------------------------------------------------------------------


class TimeoutFakeAcpClient(FakeAcpClient):
    """A FakeAcpClient whose prompt() raises PromptTimeout."""

    async def prompt(
        self, session_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        from acp_proxy.client import PromptTimeout

        self.last_prompt_messages = messages
        raise PromptTimeout(session_id, timeout_s=120.0, partial_text="partial")
        # Make this a generator
        yield  # pragma: no cover


class SessionFailFakeAcpClient(FakeAcpClient):
    """A FakeAcpClient whose create_session() raises."""

    async def create_session(self, cwd: str, model_id: str | None = None) -> str:
        raise RuntimeError("ACP server not responding")


@pytest.mark.asyncio
async def test_non_streaming_prompt_timeout_returns_504():
    """A prompt timeout in non-streaming mode returns a 504 with structured error."""
    fake = TimeoutFakeAcpClient()
    app = create_app(fake, "/tmp/test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4.1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
    assert resp.status_code == 504
    data = resp.json()
    assert data["error"]["code"] == "prompt_timeout"
    assert "timed out" in data["error"]["message"]


@pytest.mark.asyncio
async def test_streaming_prompt_timeout_emits_error_event():
    """A prompt timeout in streaming mode emits an error SSE event."""

    class StreamingTimeoutClient(FakeAcpClient):
        """Yields a few chunks then raises PromptTimeout."""

        async def prompt(
            self, session_id: str, messages: list[dict[str, Any]], **kwargs: Any
        ) -> AsyncIterator[dict[str, Any]]:
            from acp_proxy.client import PromptTimeout

            self.last_prompt_messages = messages
            yield {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "partial "},
            }
            yield {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "response"},
            }
            raise PromptTimeout(
                session_id, timeout_s=120.0, partial_text="partial response"
            )

    fake = StreamingTimeoutClient()
    app = create_app(fake, "/tmp/test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4.1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
    assert resp.status_code == 200  # SSE headers already sent
    body = resp.text

    # Should contain content chunks before the error
    assert "partial " in body
    assert "response" in body

    # Should contain an error event
    events = []
    for line in body.split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))

    error_events = [e for e in events if "error" in e]
    assert len(error_events) == 1
    assert error_events[0]["error"]["code"] == "prompt_timeout"

    # Should end with [DONE]
    assert "data: [DONE]" in body


@pytest.mark.asyncio
async def test_session_creation_failure_returns_502():
    """A session creation failure returns a 502 with structured error."""
    fake = SessionFailFakeAcpClient()
    app = create_app(fake, "/tmp/test")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4.1",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
    assert resp.status_code == 502
    data = resp.json()
    assert data["error"]["code"] == "acp_session_error"
    assert "ACP server not responding" in data["error"]["message"]


# ---------------------------------------------------------------------------
# _map_stop_reason
# ---------------------------------------------------------------------------


class TestMapStopReason:
    """All ACP stop reasons map to OpenAI finish reasons."""

    @pytest.mark.parametrize(
        "acp_reason,expected",
        [
            ("end_turn", "stop"),
            ("max_tokens", "length"),
            ("cancelled", "stop"),
            ("refusal", "stop"),
            ("max_turn_requests", "stop"),
            ("unknown_future_reason", "stop"),  # unknown defaults to "stop"
        ],
    )
    def test_mapping(self, acp_reason: str, expected: str):
        from acp_proxy.server import _map_stop_reason

        assert _map_stop_reason(acp_reason) == expected
