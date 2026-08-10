"""
ACP client for copilot-language-server.

Manages initialization, session lifecycle, model selection, and prompt
execution. Translates between ACP's stateful session model and the
request/response pattern needed by the OpenAI-compatible proxy layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from . import __version__
from .transport import AcpError, AcpTransport

logger = logging.getLogger(__name__)

# Default prompt timeout in seconds.  If the ACP server does not complete
# a session/prompt response within this window, the prompt is cancelled and
# a PromptTimeout is raised.  The experiment harness uses 120s; production
# should match.  This is the single most important safety net — without it,
# a hung language server blocks the HTTP connection indefinitely.
DEFAULT_PROMPT_TIMEOUT_S: float = 120.0
DIRECT_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "max_turn_requests",
    "refusal",
    "cancelled",
}
DIRECT_SESSION_UPDATE_TYPES = {
    "agent_message_chunk",
    "agent_thought_chunk",
    "available_commands_update",
    "config_option_update",
    "current_mode_update",
    "plan",
    "session_info_update",
    "tool_call",
    "tool_call_update",
    "usage_update",
    "user_message_chunk",
}
_DIRECT_PROMPT_TERMINAL_MARKER = {"__acp_prompt_terminal__": True}
_MAX_DIRECT_CONTROL_UPDATE_BYTES = 256_000
_MAX_DIRECT_AVAILABLE_COMMANDS = 1024


class PromptTimeout(Exception):
    """Raised when a session/prompt exceeds the configured deadline.

    Attributes:
        session_id: The ACP session that timed out.
        timeout_s: The deadline that was exceeded.
        partial_text: Any response text collected before the timeout.
    """

    def __init__(
        self, session_id: str, timeout_s: float, partial_text: str = ""
    ) -> None:
        self.session_id = session_id
        self.timeout_s = timeout_s
        self.partial_text = partial_text
        super().__init__(
            f"session/prompt timed out after {timeout_s}s (session {session_id[:8]})"
        )


class ModelAcknowledgementError(RuntimeError):
    """ACP failed to settle the requested session model binding."""


def _summarize(obj: Any, max_len: int = 200) -> str:
    """Summarize an object for logging — truncate long values."""
    import json

    try:
        s = json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001 - diagnostic summarization must never escape
        s = repr(obj)
    if len(s) > max_len:
        return s[:max_len] + f"... ({len(s)} chars)"
    return s


@dataclass
class ModelInfo:
    """A model available through the ACP agent."""

    model_id: str
    name: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AcpSessionDescriptor:
    """A real ACP session with its requested model binding settled."""

    session_id: str
    model_id: str


class CallbackPolicy(StrEnum):
    """Client callback authority selected before ACP initialization."""

    LEGACY_PERMISSIVE = "legacy-permissive"
    DIRECT_DENY = "direct-deny"


@dataclass
class SessionState:
    """Tracks the state of an ACP session."""

    session_id: str
    model_id: str | None = None
    available_model_ids: frozenset[str] | None = None
    created_at: float = field(default_factory=time.time)


class AcpClient:
    """High-level ACP client wrapping transport + session management.

    Responsibilities:
    - ACP initialization handshake
    - Session creation with model selection
    - Prompt execution with streaming response collection
    - Handling agent callbacks (permission requests, fs, terminal)
    """

    def __init__(
        self,
        binary_path: str,
        *,
        callback_policy: CallbackPolicy = CallbackPolicy.LEGACY_PERMISSIVE,
    ) -> None:
        self._binary_path = binary_path
        self._callback_policy = callback_policy
        self._transport = AcpTransport()
        self._models: list[ModelInfo] = []
        self._default_model: str | None = None
        self._sessions: dict[str, SessionState] = {}
        self._update_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
        self._direct_prompt_phases: dict[str, str] = {}
        self._direct_update_budgets: dict[str, dict[str, int]] = {}
        self._expected_model_updates: dict[str, str] = {}
        self._available_commands_by_session: dict[str, list[Any]] = {}
        self._provisional_session_ids: set[str] = set()
        self._agent_name: str | None = None
        self._agent_version: str | None = None
        self._protocol_version: int | None = None
        self._agent_capabilities: dict[str, Any] = {}

    @property
    def models(self) -> list[ModelInfo]:
        return list(self._models)

    @property
    def default_model(self) -> str | None:
        return self._default_model

    @property
    def agent_info(self) -> dict[str, str | None]:
        return {"name": self._agent_name, "version": self._agent_version}

    @property
    def protocol_version(self) -> int:
        if self._protocol_version is None:
            raise RuntimeError("ACP client has not completed initialization")
        return self._protocol_version

    @property
    def agent_capabilities(self) -> dict[str, Any]:
        return dict(self._agent_capabilities)

    @property
    def is_alive(self) -> bool:
        """Whether the owned ACP child transport remains usable."""

        return self._transport.is_open

    @property
    def callback_policy(self) -> CallbackPolicy:
        """Return the immutable callback authority selected before startup."""

        return self._callback_policy

    def on_transport_closed(self, handler: Any) -> None:
        """Notify the process owner when the ACP child stream closes unexpectedly."""

        self._transport.on_close(handler)

    async def start(self, env: dict[str, str] | None = None) -> None:
        """Start the language server and complete ACP initialization.

        Args:
            env: Environment variables for the subprocess.  If None, the
                current process environment is inherited.
        """
        self._transport.on_notification(self._handle_notification)
        self._transport.set_strict_response_correlation(
            self._callback_policy is CallbackPolicy.DIRECT_DENY
        )
        self._transport.on_request_observed(self._observe_agent_request)
        self._transport.on_request(self._handle_agent_request)
        self._transport.on_request_sent(self._observe_request_sent)
        self._transport.on_response_observed(self._observe_response)
        await self._transport.start(self._binary_path, env=env)
        await self._initialize()

    async def stop(self) -> None:
        """Shut down the transport and clean up sessions."""
        try:
            self._signal_update_queues()
            self._clear_client_state()
        finally:
            await self._transport.stop()

    async def abort(self) -> None:
        """Abort uncertain ACP work and notify the owning proxy lifecycle."""

        try:
            self._signal_update_queues()
            self._clear_client_state()
        finally:
            await self._transport.abort()

    def _signal_update_queues(self) -> None:
        """Wake collectors without allowing a full evidence queue to block teardown."""

        for queue in self._update_queues.values():
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)

    def _clear_client_state(self) -> None:
        """Drop all session-correlated state after collectors are signalled."""

        self._update_queues.clear()
        self._direct_prompt_phases.clear()
        self._direct_update_budgets.clear()
        self._expected_model_updates.clear()
        self._available_commands_by_session.clear()
        self._provisional_session_ids.clear()
        self._sessions.clear()

    async def create_session(self, cwd: str, model_id: str | None = None) -> str:
        """Create a new ACP session.

        Returns the session ID. If model_id is provided, the model is
        set after session creation.
        """
        params = {"cwd": cwd, "mcpServers": []}
        direct_mode = (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is CallbackPolicy.DIRECT_DENY
        )
        if direct_mode:
            logger.debug(
                "session/new request: cwd_present=%s mcp_server_count=0",
                bool(cwd),
            )
        else:
            logger.debug("session/new request params: %s", params)
        try:
            result = await self._transport.send_request("session/new", params)
        except AcpError as e:
            if direct_mode:
                logger.error(
                    "session/new failed: error_type=%s", type(e).__name__
                )
            else:
                logger.error(
                    "session/new failed: %s | full error: %s | cwd: %s",
                    e,
                    e.error_obj,
                    cwd,
                )
            raise
        if direct_mode:
            logger.debug(
                "session/new response: session_id_present=%s models_present=%s",
                isinstance(result.get("sessionId"), str),
                "models" in result,
            )
        else:
            logger.debug("session/new response: %s", result)
        session_id = result["sessionId"]
        if direct_mode:
            self._bind_provisional_session(session_id)

        session_current_model: str | None = None
        session_available_models: frozenset[str] | None = None
        # Extract the global catalog while retaining evidence from this exact
        # session separately. A later direct session must never inherit the
        # catalog probe's current model when its own response omits that state.
        if "models" in result:
            models_data = result["models"]
            if not direct_mode or not self._sessions:
                self._extract_models(models_data)
            current_model = models_data.get("currentModelId")
            if isinstance(current_model, str) and current_model:
                session_current_model = current_model
            available_models = models_data.get("availableModels")
            if isinstance(available_models, list):
                session_available_models = frozenset(
                    item["modelId"]
                    for item in available_models
                    if isinstance(item, dict)
                    and isinstance(item.get("modelId"), str)
                    and item["modelId"]
                )

        session = SessionState(
            session_id=session_id,
            model_id=(
                session_current_model
                if direct_mode
                else self._default_model
            ),
            available_model_ids=session_available_models,
        )
        self._sessions[session_id] = session

        # ``session/new`` reports the model already active on this session.
        # Only non-default requests require a separate selection operation.
        if model_id and model_id != session.model_id:
            if direct_mode:
                await self._settle_direct_model(session_id, model_id)
            else:
                await self._try_set_model(session_id, model_id)

        if direct_mode:
            logger.info(
                "Created direct ACP session: model_bound=%s",
                session.model_id is not None,
            )
        else:
            logger.info(
                "Created session %s with model %s", session_id, session.model_id
            )
        return session_id

    async def create_session_exact(
        self, cwd: str, model_id: str
    ) -> AcpSessionDescriptor:
        """Create a session and settle its requested model binding.

        ``session/new`` is authoritative when its per-session current model is
        already the requested model. Otherwise the Copilot-specific
        ``session/set_model`` operation must settle successfully before the
        session is returned. The supported language server does not expose a
        separate post-selection value, so non-default binding evidence is the
        successful settlement of that exact request.
        """

        available = {model.model_id for model in self._models}
        if model_id not in available:
            raise ValueError(
                f"Model {model_id!r} is not advertised. Available: {sorted(available)}"
            )
        session_id = await self.create_session(cwd)
        session = self._sessions[session_id]
        if (
            not isinstance(session.model_id, str)
            or not session.model_id
            or session.available_model_ids is None
            or session.model_id not in session.available_model_ids
        ):
            raise ModelAcknowledgementError(
                "session/new omitted a consistent per-session model catalog"
            )
        if model_id not in session.available_model_ids:
            raise ModelAcknowledgementError(
                "session/new did not advertise the requested session model"
            )
        if session.model_id != model_id:
            await self._settle_direct_model(session_id, model_id)
        bound_model = session.model_id
        if bound_model != model_id:
            raise ModelAcknowledgementError(
                "copilot-language-server did not settle the requested session model"
            )
        return AcpSessionDescriptor(session_id=session_id, model_id=bound_model)

    @staticmethod
    def _model_from_config_options(result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            raise ModelAcknowledgementError(
                "session/set_config_option did not return complete configOptions"
            )
        options = result.get("configOptions")
        if not isinstance(options, list):
            raise ModelAcknowledgementError(
                "session/set_config_option did not return complete configOptions"
            )
        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("id") == "model" or option.get("category") == "model":
                current = option.get("currentValue")
                if not isinstance(current, str) or not current:
                    raise ModelAcknowledgementError(
                        "model config option omitted a non-empty currentValue"
                    )
                return current
        raise ModelAcknowledgementError(
            "configOptions did not contain the model option"
        )

    async def prompt(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        timeout_s: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a prompt and yield streaming update events.

        Translates the OpenAI messages array into an ACP prompt.
        Yields individual session/update events as they arrive.
        The final yield is a sentinel dict with 'done': True and the
        stop reason.

        Args:
            session_id: The ACP session ID.
            messages: OpenAI-format messages array.
            timeout_s: Maximum seconds to wait for the prompt to complete.
                Defaults to DEFAULT_PROMPT_TIMEOUT_S.  If the deadline is
                exceeded, the prompt task is cancelled and PromptTimeout
                is raised with any partial text collected so far.

        Yields:
            Update dicts from ACP session/update notifications.

        Raises:
            PromptTimeout: If the prompt does not complete within the deadline.
            ValueError: If the session ID is unknown.
        """
        # Convert OpenAI messages to ACP prompt content blocks
        prompt_content = self._messages_to_prompt(messages)

        async for update in self._prompt_content(
            session_id,
            prompt_content,
            timeout_s=timeout_s,
            require_known_stop_reason=False,
        ):
            yield update

    async def prompt_blocks(
        self,
        session_id: str,
        blocks: list[dict[str, Any]],
        *,
        timeout_s: float | None = None,
        event_byte_limit: int = 4_000_000,
        event_count_limit: int = 4096,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send already-layered direct content blocks without chat-history logic."""

        if not blocks or any(block.get("type") != "text" for block in blocks):
            raise ValueError("direct v1 accepts one or more ACP text content blocks")
        async for update in self._prompt_content(
            session_id,
            blocks,
            timeout_s=timeout_s,
            require_known_stop_reason=True,
            event_byte_limit=event_byte_limit,
            event_count_limit=event_count_limit,
        ):
            yield update

    async def _prompt_content(
        self,
        session_id: str,
        prompt_content: list[dict[str, Any]],
        *,
        timeout_s: float | None = None,
        require_known_stop_reason: bool,
        event_byte_limit: int | None = None,
        event_count_limit: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute one ACP prompt while retaining ordered session updates."""

        if session_id not in self._sessions:
            raise ValueError(f"Unknown session: {session_id}")
        effective_timeout = (
            timeout_s if timeout_s is not None else DEFAULT_PROMPT_TIMEOUT_S
        )

        # Set up a queue to receive streaming updates for this session
        queue_maxsize = (
            event_count_limit + 1
            if event_count_limit is not None
            else 0
        )
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=queue_maxsize
        )
        self._update_queues[session_id] = queue
        if (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is CallbackPolicy.DIRECT_DENY
        ):
            phase = self._direct_prompt_phases.get(session_id)
            if phase not in {None, "terminal"}:
                self._transport.fail_closed(
                    "direct ACP prompt correlation protocol failure"
                )
                raise ConnectionError(
                    "direct ACP prompt correlation protocol failure"
                )
            self._direct_prompt_phases[session_id] = "preparing"
            if (
                event_byte_limit is None
                or event_byte_limit < 1
                or event_count_limit is None
                or event_count_limit < 1
            ):
                raise ValueError("direct prompt requires positive event bounds")
            self._direct_update_budgets[session_id] = {
                "bytes": 0,
                "count": 0,
                "byte_limit": event_byte_limit,
                "count_limit": event_count_limit,
            }

        deadline = asyncio.get_event_loop().time() + effective_timeout
        partial_text = ""
        prompt_task: asyncio.Task[dict[str, Any]] | None = None

        try:
            # Send prompt — this returns when the turn is complete
            prompt_task = asyncio.create_task(
                self._transport.send_request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": prompt_content},
                )
            )

            # Yield updates as they arrive, enforcing the deadline
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    prompt_task.cancel()
                    if (
                        getattr(
                            self,
                            "_callback_policy",
                            CallbackPolicy.LEGACY_PERMISSIVE,
                        )
                        is CallbackPolicy.DIRECT_DENY
                    ):
                        logger.error(
                            "Direct ACP prompt timed out after %.1fs; "
                            "partial_text_chars=%d",
                            effective_timeout,
                            len(partial_text),
                        )
                    else:
                        logger.error(
                            "Prompt timed out after %.1fs (session %s). "
                            "Partial text collected: %d chars",
                            effective_timeout,
                            session_id[:8],
                            len(partial_text),
                        )
                    raise PromptTimeout(session_id, effective_timeout, partial_text)

                # Poll with the smaller of 0.1s or remaining time
                poll_timeout = min(0.1, remaining)
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=poll_timeout)
                    if update is None:
                        break
                    if update == _DIRECT_PROMPT_TERMINAL_MARKER:
                        break
                    # Track partial text for timeout diagnostics
                    kind = update.get("sessionUpdate", "")
                    if kind == "agent_message_chunk":
                        content = update.get("content", {})
                        if content.get("type") == "text":
                            partial_text += content.get("text", "")
                    yield update
                except TimeoutError:
                    if prompt_task.done():
                        # Drain remaining updates
                        while not queue.empty():
                            update = queue.get_nowait()
                            if update is not None:
                                yield update
                        break

            # Get the final response
            result = await prompt_task
            stop_reason = result.get("stopReason")
            if require_known_stop_reason:
                if not isinstance(stop_reason, str) or stop_reason not in DIRECT_STOP_REASONS:
                    raise RuntimeError(
                        "direct ACP prompt omitted a known non-empty stopReason"
                    )
            elif not isinstance(stop_reason, str) or not stop_reason:
                stop_reason = "end_turn"
            yield {"done": True, "stopReason": stop_reason}

        finally:
            self._update_queues.pop(session_id, None)
            getattr(self, "_direct_update_budgets", {}).pop(session_id, None)
            if prompt_task is not None and not prompt_task.done():
                prompt_task.cancel()
                await asyncio.gather(prompt_task, return_exceptions=True)

    async def cancel_session(self, session_id: str) -> None:
        """Request cancellation using ACP v1's stable session notification."""

        if session_id not in self._sessions:
            raise ValueError(f"Unknown session: {session_id}")
        await self._transport.send_notification(
            "session/cancel", {"sessionId": session_id}
        )

    async def set_model(self, session_id: str, model_id: str) -> None:
        """Change the model for an existing session."""
        await self._try_set_model(session_id, model_id)

    async def _initialize(self) -> None:
        """Complete the ACP initialization handshake."""
        client_capabilities: dict[str, Any]
        if (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is CallbackPolicy.DIRECT_DENY
        ):
            client_capabilities = {}
        else:
            client_capabilities = {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            }
        result = await self._transport.send_request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "acp-proxy", "version": __version__},
                "clientCapabilities": client_capabilities,
            },
        )
        direct_mode = (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is CallbackPolicy.DIRECT_DENY
        )
        if direct_mode:
            logger.debug(
                "initialize response received: protocol_type=%s "
                "agent_info_present=%s capabilities_present=%s auth_present=%s",
                type(result.get("protocolVersion")).__name__,
                isinstance(result.get("agentInfo"), dict),
                isinstance(result.get("agentCapabilities"), dict),
                bool(result.get("authMethods", result.get("signin"))),
            )
        else:
            logger.debug("initialize response: %s", result)
        info = result.get("agentInfo", {})
        protocol_version = result.get("protocolVersion")
        if type(protocol_version) is not int or protocol_version != 1:
            if direct_mode:
                raise RuntimeError("direct ACP protocol version mismatch")
            raise RuntimeError(
                f"ACP protocol version mismatch: expected 1, got {protocol_version!r}"
            )
        self._protocol_version = protocol_version
        self._agent_name = info.get("name")
        self._agent_version = info.get("version")
        if direct_mode:
            logger.info("Initialized direct ACP agent: protocol=%s", protocol_version)
        else:
            logger.info(
                "Initialized: %s v%s, protocol=%s",
                self._agent_name,
                self._agent_version,
                result.get("protocolVersion"),
            )
        # Log capabilities for debugging environment differences
        caps = result.get("agentCapabilities")
        if not isinstance(caps, dict):
            raise TypeError("initialize response omitted agentCapabilities")
        self._agent_capabilities = dict(caps)
        if direct_mode:
            logger.debug("Server capabilities received: count=%d", len(caps))
        else:
            logger.debug("Server capabilities: %s", caps)
        auth = result.get("authMethods", result.get("signin", {}))
        if auth:
            if direct_mode:
                logger.debug("ACP auth methods reported")
            else:
                logger.debug("Auth methods: %s", auth)

    def _extract_models(self, models_data: dict[str, Any]) -> None:
        """Extract available models from a session/new response."""
        self._models = []
        for m in models_data.get("availableModels", []):
            self._models.append(
                ModelInfo(
                    model_id=m["modelId"],
                    name=m.get("name", m["modelId"]),
                    meta=m.get("_meta", {}),
                )
            )
        self._default_model = models_data.get("currentModelId")

    async def _try_set_model(self, session_id: str, model_id: str) -> None:
        """Select a model for the deprecated legacy adapter."""

        methods = [
            ("session/set_model", {"sessionId": session_id, "modelId": model_id}),
            (
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": model_id},
            ),
        ]
        for method, params in methods:
            try:
                await self._transport.send_request(method, params)
                if session_id in self._sessions:
                    self._sessions[session_id].model_id = model_id
                logger.info(
                    "Set model for session %s to %s (via %s)",
                    session_id,
                    model_id,
                    method,
                )
                return
            except AcpError as exc:
                if "not found" in str(exc).lower():
                    logger.debug("%s not supported, trying next method", method)
                    continue
                raise

        raise RuntimeError(
            f"Model selection not supported by this server. "
            f"Tried: {[method for method, _ in methods]}. Requested model: {model_id}"
        )

    async def _settle_direct_model(self, session_id: str, model_id: str) -> None:
        """Set a non-default session model and require request settlement.

        Tries session/set_model (Copilot-specific) first, then falls back
        to session/set_config_option (ACP spec standard). The standard response
        is verified when used; the Copilot-specific method returns no selected
        model value, so its successful JSON-RPC settlement is the available
        evidence. Silent degradation to the default model is never allowed.
        """
        methods: list[tuple[str, dict[str, Any], bool]] = [
            (
                "session/set_model",
                {"sessionId": session_id, "modelId": model_id},
                False,
            ),
            (
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": model_id},
                True,
            ),
        ]
        expected_model_updates = getattr(self, "_expected_model_updates", None)
        if expected_model_updates is None:
            expected_model_updates = {}
            self._expected_model_updates = expected_model_updates
        expected_model_updates[session_id] = model_id
        try:
            for method, params, verifies_current_value in methods:
                try:
                    result = await self._transport.send_request(method, params)
                except AcpError as exc:
                    if exc.error_obj.get("code") == -32601:
                        logger.debug("%s not supported, trying next method", method)
                        continue
                    logger.error("ACP model selection request was rejected")
                    raise ModelAcknowledgementError(
                        "copilot-language-server rejected the requested session model"
                    ) from None

                if verifies_current_value:
                    observed = self._model_from_config_options(result)
                    if observed != model_id:
                        raise ModelAcknowledgementError(
                            "copilot-language-server returned a different session model"
                        )
                if session_id in self._sessions:
                    self._sessions[session_id].model_id = model_id
                if (
                    getattr(
                        self,
                        "_callback_policy",
                        CallbackPolicy.LEGACY_PERMISSIVE,
                    )
                    is CallbackPolicy.DIRECT_DENY
                ):
                    logger.info("Set requested direct model via %s", method)
                else:
                    logger.info(
                        "Set model for session %s to %s (via %s)",
                        session_id,
                        model_id,
                        method,
                    )
                return
        finally:
            expected_model_updates.pop(session_id, None)

        raise ModelAcknowledgementError(
            "copilot-language-server exposes no supported session model selector"
        )

    @staticmethod
    def _extract_text(content: str | list[dict[str, Any]] | None) -> str:
        """Extract plain text from an OpenAI message content field."""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block["text"])
            return "\n".join(parts)
        return ""

    @staticmethod
    def extract_last_user_message(messages: list[dict[str, Any]]) -> str:
        """Extract the final user message from an OpenAI messages array.

        The ACP session is stateful and accumulates context across turns.
        OpenCode sends the full conversation history with every request.
        Forwarding the full history would duplicate context already in the
        session. We extract only the last user message — the new content
        for this turn.
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return AcpClient._extract_text(msg.get("content"))
        # Fallback: if no user message, concatenate everything
        return "\n\n".join(
            AcpClient._extract_text(m.get("content"))
            for m in messages
            if AcpClient._extract_text(m.get("content"))
        )

    @staticmethod
    def extract_first_user_message(messages: list[dict[str, Any]]) -> str:
        """Extract the first user message — the conversation anchor.

        Used to derive a stable session identifier: the first user message
        is the same across all turns of a conversation (OpenCode replays
        the full history each time). Hashing it gives a stable key.
        """
        for msg in messages:
            if msg.get("role") == "user":
                return AcpClient._extract_text(msg.get("content"))
        return ""

    def _messages_to_prompt(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert OpenAI messages array to ACP prompt content blocks.

        Extracts only the last user message. The ACP session maintains
        its own conversation history — we do not replay prior turns.
        """
        text = self.extract_last_user_message(messages)
        return [{"type": "text", "text": text}]

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Route incoming notifications to the appropriate session queue."""
        method = msg.get("method", "")
        params = msg.get("params", {})

        direct_mode = (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is CallbackPolicy.DIRECT_DENY
        )
        if direct_mode:
            if not self._direct_model_integrity_holds(params):
                return
            if self._handle_direct_control_update(method, params):
                return
            if not self._is_valid_direct_session_update(method, params):
                self._transport.fail_closed(
                    "direct ACP session update protocol failure"
                )
                return
            session_id = params["sessionId"]
            update = params["update"]
            if update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
                logger.info(
                    "Tool activity [%s] in direct evidence stream",
                    update["sessionUpdate"],
                )
            self._enqueue_direct_update(session_id, update)
            return

        if method == "session/update":
            session_id = params.get("sessionId", "")
            update = params.get("update", {})
            update_type = update.get("sessionUpdate", "unknown")
            # Log tool_call updates at INFO so we can see what the LSP
            # is doing with tools — this is critical for understanding
            # the tool execution model.
            if update_type in ("tool_call", "tool_call_update"):
                logger.info("Tool activity [%s]: %s", update_type, _summarize(update))
            queue = self._update_queues.get(session_id)
            if queue:
                queue.put_nowait(update)

    def _direct_model_integrity_holds(self, params: Any) -> bool:
        """Fail continuity when any config update changes the selected model."""

        if not isinstance(params, dict):
            return True
        session_id = params.get("sessionId")
        update = params.get("update")
        if (
            not isinstance(session_id, str)
            or not isinstance(update, dict)
            or update.get("sessionUpdate") != "config_option_update"
        ):
            return True
        options = update.get("configOptions")
        if not isinstance(options, list):
            return True
        session = self._sessions.get(session_id)
        selected_model = getattr(self, "_expected_model_updates", {}).get(
            session_id,
            session.model_id if session is not None else None,
        )
        for option in options:
            if (
                not isinstance(option, dict)
                or not isinstance(option.get("id"), str)
                or not option["id"]
            ):
                self._transport.fail_closed(
                    "direct ACP config update malformed"
                )
                return False
            if option.get("id") == "model" or option.get("category") == "model":
                current = option.get("currentValue")
                if not isinstance(current, str) or current != selected_model:
                    self._transport.fail_closed(
                        "direct ACP selected model drifted"
                    )
                    return False
        return True

    def _handle_direct_control_update(self, method: Any, params: Any) -> bool:
        """Validate and retain legitimate known-session updates outside prompts."""

        if method != "session/update" or not isinstance(params, dict):
            return False
        session_id = params.get("sessionId")
        update = params.get("update")
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        provisional_session = (
            isinstance(session_id, str)
            and session_id not in self._sessions
            and kind == "available_commands_update"
            and self._admit_provisional_session_id(session_id)
        )
        if (
            not isinstance(session_id, str)
            or (session_id not in self._sessions and not provisional_session)
            or not isinstance(update, dict)
            or getattr(self, "_direct_prompt_phases", {}).get(session_id) == "active"
        ):
            return False
        if kind == "available_commands_update":
            commands = update.get("availableCommands")
            try:
                control_bytes = len(
                    json.dumps(
                        update,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except (TypeError, ValueError):
                return False
            if (
                not isinstance(commands, list)
                or len(commands) > _MAX_DIRECT_AVAILABLE_COMMANDS
                or control_bytes > _MAX_DIRECT_CONTROL_UPDATE_BYTES
            ):
                return False
            available_commands = getattr(
                self, "_available_commands_by_session", None
            )
            if available_commands is None:
                available_commands = {}
                self._available_commands_by_session = available_commands
            available_commands[session_id] = list(commands)
            return True
        if kind != "config_option_update":
            return False
        options = update.get("configOptions")
        if not isinstance(options, list):
            return False
        for option in options:
            if not isinstance(option, dict):
                return False
        return True

    def _admit_provisional_session_id(self, session_id: str) -> bool:
        """Bind at most one provisional ID per unresolved session/new request."""

        provisional = self._provisional_session_ids
        if session_id in provisional:
            return True
        pending_creates = self._transport.pending_request_count("session/new")
        if pending_creates <= len(provisional):
            return False
        provisional.add(session_id)
        return True

    def _bind_provisional_session(self, session_id: str) -> None:
        """Resolve pre-response command state to the returned session identity."""

        provisional = self._provisional_session_ids
        if session_id in provisional:
            provisional.remove(session_id)
            return
        if provisional and self._transport.pending_request_count("session/new") == 0:
            for orphaned_session_id in provisional:
                self._available_commands_by_session.pop(
                    orphaned_session_id, None
                )
            provisional.clear()
            self._transport.fail_closed(
                "direct ACP provisional session identity mismatch"
            )
            raise ConnectionError(
                "direct ACP provisional session identity mismatch"
            )

    def _enqueue_direct_update(
        self, session_id: str, update: dict[str, Any]
    ) -> bool:
        """Bound direct evidence before retaining it in the reader-side queue."""

        budget = getattr(self, "_direct_update_budgets", {}).get(session_id)
        queue = self._update_queues.get(session_id)
        if budget is None or queue is None:
            self._transport.fail_closed(
                "direct ACP session update protocol failure"
            )
            return False
        try:
            encoded_bytes = len(
                json.dumps(
                    update,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            self._transport.fail_closed(
                "direct ACP session update protocol failure"
            )
            return False
        projected_bytes = budget["bytes"] + encoded_bytes
        projected_count = budget["count"] + 1
        if (
            projected_bytes > budget["byte_limit"]
            or projected_count > budget["count_limit"]
            or queue.full()
        ):
            self._transport.fail_closed(
                "direct ACP evidence stream exceeded reader-side limits"
            )
            return False
        budget["bytes"] = projected_bytes
        budget["count"] = projected_count
        queue.put_nowait(update)
        return True

    def _is_valid_direct_session_update(
        self, method: Any, params: Any
    ) -> bool:
        """Validate the direct evidence stream before admitting any update.

        Direct mode has one active prompt queue per ACP session.  Unknown,
        pre-prompt, or post-prompt updates cannot be assigned truthfully to a
        Meadow request, so they revoke continuity rather than being dropped.
        """

        if method != "session/update" or not isinstance(params, dict):
            return False
        session_id = params.get("sessionId")
        update = params.get("update")
        if (
            not isinstance(session_id, str)
            or not session_id
            or session_id not in self._sessions
            or session_id not in self._update_queues
            or not isinstance(update, dict)
        ):
            return False
        if getattr(self, "_direct_prompt_phases", {}).get(session_id) != "active":
            return False
        kind = update.get("sessionUpdate")
        if kind not in DIRECT_SESSION_UPDATE_TYPES:
            return False
        if kind in {
            "agent_message_chunk",
            "agent_thought_chunk",
            "user_message_chunk",
        }:
            content = update.get("content")
            if not isinstance(content, dict):
                return False
            content_type = content.get("type")
            if not isinstance(content_type, str) or not content_type:
                return False
            return content_type != "text" or isinstance(content.get("text"), str)
        if kind in {"tool_call", "tool_call_update"}:
            return isinstance(update.get("toolCallId"), str) and bool(
                update["toolCallId"]
            )
        if kind == "plan":
            return isinstance(update.get("entries"), list)
        if kind == "available_commands_update":
            return isinstance(update.get("availableCommands"), list)
        if kind == "current_mode_update":
            return isinstance(update.get("currentModeId"), str)
        if kind == "config_option_update":
            return isinstance(update.get("configOptions"), list)
        # These are bounded raw diagnostics. Direct v1 derives no usage or
        # session-information claims from their agent-defined payloads.
        return kind in {"usage_update", "session_info_update"}

    def _observe_request_sent(
        self,
        _request_id: int,
        method: str,
        params: dict[str, Any] | None,
    ) -> None:
        """Open a direct update epoch only after prompt bytes are on the wire."""

        if (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is not CallbackPolicy.DIRECT_DENY
        ):
            return
        if method != "session/prompt":
            return
        session_id = params.get("sessionId") if isinstance(params, dict) else None
        if (
            not isinstance(session_id, str)
            or self._direct_prompt_phases.get(session_id) != "preparing"
        ):
            self._transport.fail_closed(
                "direct ACP prompt correlation protocol failure"
            )
            return
        self._direct_prompt_phases[session_id] = "active"

    def _observe_response(
        self,
        _message: dict[str, Any],
        method: str,
        params: dict[str, Any] | None,
    ) -> None:
        """Put a terminal marker in the same ordered stream as direct updates."""

        if (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is not CallbackPolicy.DIRECT_DENY
        ):
            return
        if method != "session/prompt":
            return
        session_id = params.get("sessionId") if isinstance(params, dict) else None
        queue = (
            self._update_queues.get(session_id)
            if isinstance(session_id, str)
            else None
        )
        if (
            isinstance(session_id, str)
            and self._transport.has_pending_incoming_requests(session_id)
        ):
            self._transport.fail_closed(
                "direct ACP callback settlement protocol failure"
            )
            return
        if (
            not isinstance(session_id, str)
            or getattr(self, "_direct_prompt_phases", {}).get(session_id) != "active"
            or queue is None
        ):
            self._transport.fail_closed(
                "direct ACP prompt correlation protocol failure"
            )
            return
        self._direct_prompt_phases[session_id] = "terminal"
        if queue.full():
            self._transport.fail_closed(
                "direct ACP evidence stream exceeded reader-side limits"
            )
            return
        queue.put_nowait(dict(_DIRECT_PROMPT_TERMINAL_MARKER))

    def _handle_agent_request(self, msg: dict[str, Any]) -> Any:
        """Handle incoming requests from the agent.

        The agent may request:
        - session/request_permission: auto-approve in Agent mode
        - fs/read_text_file: read file from disk
        - fs/write_text_file: write file to disk
        - terminal/*: terminal operations

        For now, auto-approve permissions and handle fs operations directly.
        Terminal operations are handled with basic subprocess execution.
        """
        method = msg.get("method", "")
        params = msg.get("params", {})

        if (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is CallbackPolicy.DIRECT_DENY
        ):
            if method == "session/request_permission":
                return {"outcome": {"outcome": "cancelled"}}
            raise PermissionError(
                f"ACP callback {method!r} was not advertised in Meadow direct mode"
            )

        logger.info("Agent request: %s params=%s", method, _summarize(params))

        handler = {
            "session/request_permission": self._handle_permission_request,
            "fs/read_text_file": self._handle_read_file,
            "fs/write_text_file": self._handle_write_file,
            "terminal/create": self._handle_terminal_create,
            "terminal/output": self._handle_terminal_output,
            "terminal/wait_for_exit": self._handle_terminal_wait,
            "terminal/release": self._handle_terminal_release,
            "terminal/kill": self._handle_terminal_kill,
        }.get(method)

        if handler is None:
            logger.warning("Unhandled agent request: %s params=%s", method, params)
            return None

        try:
            result = handler(params)
            logger.info("Agent request %s → response=%s", method, _summarize(result))
            return result
        except Exception:
            logger.exception("Agent request %s failed", method)
            raise

    def _observe_agent_request(self, msg: dict[str, Any]) -> None:
        """Queue sanitized direct callback evidence at transport-read order."""

        if (
            getattr(self, "_callback_policy", CallbackPolicy.LEGACY_PERMISSIVE)
            is not CallbackPolicy.DIRECT_DENY
        ):
            return
        method = str(msg.get("method", ""))
        params = msg.get("params", {})
        if not isinstance(params, dict):
            self._transport.fail_closed(
                "direct ACP callback correlation protocol failure"
            )
            return
        session_id = params.get("sessionId")
        if (
            not isinstance(session_id, str)
            or getattr(self, "_direct_prompt_phases", {}).get(session_id) != "active"
        ):
            self._transport.fail_closed(
                "direct ACP callback correlation protocol failure"
            )
            return
        queue = getattr(self, "_update_queues", {}).get(session_id)
        if queue is None:
            self._transport.fail_closed(
                "direct ACP callback correlation protocol failure"
            )
            return
        if method == "session/request_permission":
            known_kinds = {"allow_once", "allow_always", "reject_once", "reject_always"}
            options = params.get("options", [])
            offered_kinds = sorted(
                {
                    str(option.get("kind"))
                    for option in options
                    if isinstance(option, dict) and option.get("kind") in known_kinds
                }
            )
            self._enqueue_direct_update(
                session_id,
                {
                    "sessionUpdate": "client_permission_request",
                    "outcome": "cancelled",
                    "offeredKinds": offered_kinds,
                },
            )
            return
        known_method = method if method in {
            "fs/read_text_file",
            "fs/write_text_file",
            "terminal/create",
            "terminal/output",
            "terminal/wait_for_exit",
            "terminal/release",
            "terminal/kill",
        } else "unadvertised"
        self._enqueue_direct_update(
            session_id,
            {
                "sessionUpdate": "client_callback_denied",
                "callbackMethod": known_method,
                "outcome": "denied",
            },
        )

    def _handle_permission_request(self, params: dict[str, Any]) -> dict[str, Any]:
        """Auto-approve all permission requests."""
        options = params.get("options", [])
        # Prefer allow_always, then allow_once
        for opt in options:
            if opt.get("kind") == "allow_always":
                logger.info("Auto-approving (always): %s", opt.get("name"))
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        for opt in options:
            if opt.get("kind") == "allow_once":
                logger.info("Auto-approving (once): %s", opt.get("name"))
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        # Fallback: select first option
        if options:
            logger.info("Auto-selecting first option: %s", options[0].get("name"))
            return {
                "outcome": {"outcome": "selected", "optionId": options[0]["optionId"]}
            }
        return {"outcome": {"outcome": "cancelled"}}

    def _handle_read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read a file from disk."""
        path = params.get("path", "")
        line = params.get("line")
        limit = params.get("limit")
        try:
            with open(path) as f:
                lines = f.readlines()
            if line is not None:
                start = max(0, line - 1)  # 1-based to 0-based
                if limit is not None:
                    lines = lines[start : start + limit]
                else:
                    lines = lines[start:]
            content = "".join(lines)
            return {"content": content}
        except Exception as e:
            logger.error("Failed to read %s: %s", path, e)
            raise

    def _handle_write_file(self, params: dict[str, Any]) -> dict[str, Any]:
        """Write content to a file."""
        import os

        path = params.get("path", "")
        content = params.get("content", "")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return {}
        except Exception as e:
            logger.error("Failed to write %s: %s", path, e)
            raise

    # --- Terminal handling ---
    # Basic implementation using asyncio subprocesses.
    # Terminal state is tracked in _terminals dict.

    _terminals: ClassVar[dict[str, dict[str, Any]]] = {}
    _terminal_counter: ClassVar[int] = 0

    def _handle_terminal_create(self, params: dict[str, Any]) -> Any:
        """Create a terminal (run a command asynchronously)."""
        import subprocess as sp

        command = params.get("command", "")
        args = params.get("args", [])
        cwd = params.get("cwd")
        env_vars = params.get("env", [])

        import os

        env = dict(os.environ)
        for var in env_vars:
            env[var["name"]] = var["value"]

        self.__class__._terminal_counter += 1
        term_id = f"term_{self.__class__._terminal_counter}"

        try:
            proc = sp.Popen(
                [command] + args,
                cwd=cwd,
                env=env,
                stdout=sp.PIPE,
                stderr=sp.STDOUT,
                text=True,
            )
            self.__class__._terminals[term_id] = {
                "process": proc,
                "output": "",
                "byte_limit": params.get("outputByteLimit"),
            }
            logger.info("Created terminal %s: %s %s", term_id, command, args)
            return {"terminalId": term_id}
        except Exception as e:
            logger.error("Failed to create terminal: %s", e)
            raise

    def _handle_terminal_output(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get current terminal output."""
        term_id = params.get("terminalId", "")
        term = self.__class__._terminals.get(term_id)
        if not term:
            return {"output": "", "truncated": False}

        proc = term["process"]
        # Read any available output
        if proc.stdout and proc.poll() is not None:
            remaining = proc.stdout.read()
            if remaining:
                term["output"] += remaining

        exit_status = None
        if proc.poll() is not None:
            exit_status = {"exitCode": proc.returncode, "signal": None}

        return {
            "output": term["output"],
            "truncated": False,
            "exitStatus": exit_status,
        }

    def _handle_terminal_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """Wait for terminal to exit."""
        term_id = params.get("terminalId", "")
        term = self.__class__._terminals.get(term_id)
        if not term:
            return {"exitCode": 1, "signal": None}

        proc = term["process"]
        try:
            stdout, _ = proc.communicate(timeout=120)
            if stdout:
                term["output"] += stdout
        except Exception:  # noqa: BLE001 - legacy subprocess boundary
            proc.kill()
        return {"exitCode": proc.returncode, "signal": None}

    def _handle_terminal_release(self, params: dict[str, Any]) -> dict[str, Any]:
        """Release a terminal."""
        term_id = params.get("terminalId", "")
        term = self.__class__._terminals.pop(term_id, None)
        if term:
            proc = term["process"]
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        return {}

    def _handle_terminal_kill(self, params: dict[str, Any]) -> dict[str, Any]:
        """Kill terminal command without releasing."""
        term_id = params.get("terminalId", "")
        term = self.__class__._terminals.get(term_id)
        if term:
            proc = term["process"]
            if proc.poll() is None:
                proc.kill()
        return {}
