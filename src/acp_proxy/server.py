"""
OpenAI-compatible HTTP server.

Exposes /v1/chat/completions and /v1/models endpoints, translating
requests to ACP calls via the AcpClient.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .client import AcpClient, PromptTimeout
from .config import estimate_tokens

logger = logging.getLogger(__name__)


# --- Request/Response models (OpenAI-compatible subset) ---


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    # Accept and ignore these — OpenCode sends them
    stream_options: dict[str, Any] | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: list[str] | str | None = None
    n: int | None = None

    model_config = {"extra": "allow"}


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# --- Server ---


def create_app(
    acp_client: AcpClient,
    cwd: str,
    system_prompt: str | None = None,
) -> FastAPI:
    """Create the FastAPI app wired to the given ACP client.

    Args:
        acp_client: The ACP client instance.
        cwd: Working directory for ACP sessions.
        system_prompt: Optional system prompt injected as the first turn
            in each new ACP session. If None, no system prompt is injected.
    """

    app = FastAPI(title="ACP-to-OpenAI Proxy")

    # Session management: keyed by (model_id, conversation_hash).
    # conversation_hash is derived from the first user message in the
    # OpenAI messages array — this is stable across turns because
    # OpenCode replays the full history each time.
    #
    # Single-message requests (len(messages) == 1) with a key that
    # already has an active session are treated as new conversations
    # and get a fresh session.  This prevents identical first prompts
    # (scripts, repeated agent invocations) from colliding into one
    # session.  Multi-message requests (len > 1) are continuations —
    # they reuse the existing session.
    _sessions: dict[tuple[str, str], str] = {}
    # Track which sessions have had their system prompt injected.
    _initialized_sessions: set[str] = set()
    # Track sessions that have received at least one user prompt
    # (beyond the system prompt injection).  Used to decide whether
    # a single-message request hitting an existing key is a collision.
    _active_sessions: set[str] = set()
    # Estimated token accumulation per session.  These are estimates —
    # actual tokenization depends on the model's tokenizer, and Copilot's
    # backend injects additional context we cannot observe.
    _session_tokens: dict[str, dict[str, int]] = {}

    async def _get_session(model_id: str, messages: list[dict[str, Any]]) -> str:
        """Get or create an ACP session for this conversation.

        Sessions are identified by (model, hash_of_first_user_message).
        A new conversation in OpenCode produces a different first user
        message and therefore a different session.  The title generator
        also produces a different first message ("You are a title
        generator...") and gets its own session — no collision.

        Single-message requests that match an already-active session are
        treated as new conversations: the old key is evicted and a fresh
        session is created.  Multi-message requests always reuse the
        existing session (they are continuations with replayed history).
        """
        import hashlib

        first_msg = AcpClient.extract_first_user_message(messages)
        conv_hash = hashlib.sha256(first_msg.encode()).hexdigest()
        key = (model_id, conv_hash)

        is_single_message = len(messages) == 1
        existing_session = _sessions.get(key)
        needs_new = existing_session is None or (
            is_single_message and existing_session in _active_sessions
        )

        if needs_new:
            session_id = await acp_client.create_session(cwd, model_id=model_id)
            _sessions[key] = session_id
            logger.info(
                "New session %s for model=%s conv=%s (first_msg=%s...)",
                session_id[:8],
                model_id,
                conv_hash[:8],
                first_msg[:60].replace("\n", " "),
            )

            # Initialize token tracking for this session
            _session_tokens[session_id] = {"prompt": 0, "completion": 0}

            # Inject system prompt as the first turn if configured
            if system_prompt and session_id not in _initialized_sessions:
                _initialized_sessions.add(session_id)
                prompt_tokens = estimate_tokens(system_prompt)
                logger.info(
                    "Injecting system prompt into session %s "
                    "(%d chars, ~%d est. tokens)",
                    session_id[:8],
                    len(system_prompt),
                    prompt_tokens,
                )
                _session_tokens[session_id]["prompt"] += prompt_tokens
                # Send and drain — we don't return this response to the caller
                async for _ in acp_client.prompt(
                    session_id,
                    [{"role": "system", "content": system_prompt}],
                ):
                    pass

        session_id = _sessions[key]
        _active_sessions.add(session_id)
        return session_id

    @app.get("/v1/models")
    async def list_models() -> JSONResponse:
        """List available models (OpenAI-compatible)."""
        models = acp_client.models
        data = []
        for m in models:
            data.append(
                {
                    "id": m.model_id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "copilot",
                }
            )
        return JSONResponse({"object": "list", "data": data})

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: ChatCompletionRequest, raw_request: Request
    ) -> Any:
        """Handle chat completion requests."""
        # Log tool definitions sent by OpenCode so we can see exactly
        # what the client thinks is available.
        raw_body = await raw_request.body()
        try:
            raw = json.loads(raw_body)
            tools = raw.get("tools")
            if tools is not None:
                tool_names = [
                    t.get("function", {}).get("name", "<unnamed>") for t in tools
                ]
                logger.info("Tools in request (%d): %s", len(tool_names), tool_names)
                logger.debug("Full tool definitions: %s", json.dumps(tools, indent=2))
            else:
                logger.info("No tools in request")

            # Log ALL extra fields OpenCode sends (session IDs, metadata, etc.)
            known_fields = {
                "model",
                "messages",
                "stream",
                "temperature",
                "max_tokens",
                "stream_options",
                "top_p",
                "frequency_penalty",
                "presence_penalty",
                "stop",
                "n",
                "tools",
                "tool_choice",
            }
            extra = {k: v for k, v in raw.items() if k not in known_fields}
            if extra:
                logger.info(
                    "Extra fields in request: %s", json.dumps(extra, default=str)[:500]
                )
        except Exception:
            logger.debug("Could not parse raw request body for tool inspection")

        model_id = request.model
        messages = [m.model_dump() for m in request.messages]

        available_ids = {m.model_id for m in acp_client.models}
        if model_id not in available_ids:
            logger.debug(
                "Model %s not in available set %s",
                model_id,
                available_ids,
            )
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": (
                            f"Model '{model_id}' is not available. "
                            f"Available models: {sorted(available_ids)}"
                        ),
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )

        try:
            session_id = await _get_session(model_id, messages)
        except Exception as e:
            logger.error("Session creation failed: %s", e, exc_info=True)
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": f"Failed to create ACP session: {e}",
                        "type": "server_error",
                        "code": "acp_session_error",
                    }
                },
            )

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        if request.stream:
            return StreamingResponse(
                _stream_response(
                    acp_client,
                    session_id,
                    messages,
                    completion_id,
                    created,
                    model_id,
                    _session_tokens,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            try:
                return await _non_streaming_response(
                    acp_client,
                    session_id,
                    messages,
                    completion_id,
                    created,
                    model_id,
                    _session_tokens,
                )
            except PromptTimeout as e:
                logger.error(
                    "Prompt timed out: %s (partial text: %d chars)",
                    e,
                    len(e.partial_text),
                )
                return JSONResponse(
                    status_code=504,
                    content={
                        "error": {
                            "message": (
                                f"ACP prompt timed out after {e.timeout_s}s. "
                                f"The copilot-language-server did not respond "
                                f"within the deadline."
                            ),
                            "type": "server_error",
                            "code": "prompt_timeout",
                            "param": e.session_id,
                        }
                    },
                )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "consumer_mode": "opencode-legacy",
                "deprecated": True,
                "agent": acp_client.agent_info,
            }
        )

    @app.api_route(
        "/meadow/v1/{direct_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def reject_direct(direct_path: str) -> JSONResponse:
        return JSONResponse(
            status_code=410,
            content={
                "error": {
                    "code": "meadow_direct_mode_required",
                    "message": (
                        f"/meadow/v1/{direct_path} requires explicit "
                        "--consumer-mode meadow-direct"
                    ),
                }
            },
        )

    return app


async def _non_streaming_response(
    client: AcpClient,
    session_id: str,
    messages: list[dict[str, Any]],
    completion_id: str,
    created: int,
    model_id: str,
    session_tokens: dict[str, dict[str, int]],
) -> JSONResponse:
    """Collect the full response and return it."""
    full_text = ""
    stop_reason = "stop"

    # Estimate prompt tokens from the user message we're sending
    last_msg = AcpClient.extract_last_user_message(messages)
    est_prompt = estimate_tokens(last_msg)

    async for update in client.prompt(session_id, messages):
        if update.get("done"):
            sr = update.get("stopReason", "end_turn")
            stop_reason = _map_stop_reason(sr)
            break
        kind = update.get("sessionUpdate", "")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") == "text":
                full_text += content.get("text", "")

    est_completion = estimate_tokens(full_text)

    # Accumulate session-level estimates
    if session_id in session_tokens:
        session_tokens[session_id]["prompt"] += est_prompt
        session_tokens[session_id]["completion"] += est_completion
        totals = session_tokens[session_id]
        logger.info(
            "Token estimate (session %s): prompt ~%d, completion ~%d | "
            "session total: prompt ~%d, completion ~%d, combined ~%d "
            "(estimates only — actual usage is higher due to provider injection)",
            session_id[:8],
            est_prompt,
            est_completion,
            totals["prompt"],
            totals["completion"],
            totals["prompt"] + totals["completion"],
        )

    response = ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model_id,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=full_text),
                finish_reason=stop_reason,
            )
        ],
        usage=UsageInfo(
            prompt_tokens=est_prompt,
            completion_tokens=est_completion,
            total_tokens=est_prompt + est_completion,
        ),
    )
    return JSONResponse(response.model_dump())


async def _stream_response(
    client: AcpClient,
    session_id: str,
    messages: list[dict[str, Any]],
    completion_id: str,
    created: int,
    model_id: str,
    session_tokens: dict[str, dict[str, int]],
) -> AsyncIterator[str]:
    """Stream SSE events in OpenAI format.

    If a PromptTimeout occurs mid-stream, we emit an error event and
    close the stream.  The client sees whatever was delivered before the
    timeout plus the error indicator.  This is the best we can do — once
    SSE headers are sent, we cannot change the HTTP status code.
    """

    # Estimate prompt tokens from the user message we're sending
    last_msg = AcpClient.extract_last_user_message(messages)
    est_prompt = estimate_tokens(last_msg)
    response_chars = 0

    try:
        async for update in client.prompt(session_id, messages):
            if update.get("done"):
                sr = update.get("stopReason", "end_turn")
                finish_reason = _map_stop_reason(sr)

                # Log token estimates at end of stream
                est_completion = estimate_tokens("x" * response_chars)
                if session_id in session_tokens:
                    session_tokens[session_id]["prompt"] += est_prompt
                    session_tokens[session_id]["completion"] += est_completion
                    totals = session_tokens[session_id]
                    logger.info(
                        "Token estimate (session %s): prompt ~%d, completion ~%d"
                        " | session total: prompt ~%d, completion ~%d, "
                        "combined ~%d (estimates only — actual usage is higher "
                        "due to provider injection)",
                        session_id[:8],
                        est_prompt,
                        est_completion,
                        totals["prompt"],
                        totals["completion"],
                        totals["prompt"] + totals["completion"],
                    )

                # Send final chunk with finish_reason
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_id,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": finish_reason,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
                break

            kind = update.get("sessionUpdate", "")
            if kind == "agent_message_chunk":
                content = update.get("content", {})
                if content.get("type") == "text":
                    text = content.get("text", "")
                    if text:
                        response_chars += len(text)
                        chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model_id,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "role": "assistant",
                                        "content": text,
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"

    except PromptTimeout as e:
        logger.error(
            "Streaming prompt timed out: %s (partial text: %d chars)",
            e,
            len(e.partial_text),
        )
        # Emit an error event so the client knows the stream was
        # terminated due to a timeout, not a clean completion.
        error_data = {
            "error": {
                "message": (
                    f"ACP prompt timed out after {e.timeout_s}s. "
                    f"Partial response was delivered before timeout."
                ),
                "type": "server_error",
                "code": "prompt_timeout",
            }
        }
        yield f"data: {json.dumps(error_data)}\n\n"
        yield "data: [DONE]\n\n"


def _map_stop_reason(acp_reason: str) -> str:
    """Map ACP stop reasons to OpenAI finish reasons."""
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "cancelled": "stop",
        "refusal": "stop",
        "max_turn_requests": "stop",
    }
    return mapping.get(acp_reason, "stop")
