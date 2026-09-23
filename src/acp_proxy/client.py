"""
ACP client for copilot-language-server.

Manages initialization, session lifecycle, model selection, and prompt
execution. Translates between ACP's stateful session model and the
explicit session and prompt primitives needed by the direct service.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

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
_DIRECT_SESSION_STATE_UPDATE_TYPES = {
    "available_commands_update",
    "config_option_update",
    "current_mode_update",
    "session_info_update",
    "usage_update",
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


class DirectModelBindingStrategy(StrEnum):
    """Model-binding method negotiated once for a direct child generation."""

    STANDARD_CONFIG = "standard-config"
    COPILOT_SET_MODEL = "copilot-set-model"


@dataclass
class SessionState:
    """Tracks the state of an ACP session."""

    session_id: str
    model_id: str | None = None
    available_model_ids: frozenset[str] | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class _ModelBinding:
    """One selection RPC, including its transition before response dispatch."""

    prior_model: str | None
    target_model: str
    method: str
    params: dict[str, Any]
    response_received: bool = False


class AcpClient:
    """High-level ACP client wrapping transport + session management.

    Responsibilities:
    - ACP initialization handshake
    - Session creation with model selection
    - Prompt execution with streaming response collection
    - Denying client callbacks while retaining ordered callback evidence
    """

    def __init__(
        self,
        binary_path: str,
        *,
        raw_event_file: str | None = None,
    ) -> None:
        self._binary_path = binary_path
        self._transport = AcpTransport(raw_event_file=raw_event_file)
        self._models: list[ModelInfo] = []
        self._default_model: str | None = None
        self._direct_model_binding_strategy: (
            DirectModelBindingStrategy | None
        ) = None
        self._direct_model_binding_negotiation: object | None = None
        self._direct_model_binding_generation = 0
        self._sessions: dict[str, SessionState] = {}
        self._update_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
        self._direct_prompt_phases: dict[str, str] = {}
        self._direct_update_budgets: dict[str, dict[str, int]] = {}
        self._model_bindings: dict[str, _ModelBinding] = {}
        self._provisional_session_ids: set[str] = set()
        self._session_new_response_ids: set[str] = set()
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
    def direct_model_binding_strategy(self) -> DirectModelBindingStrategy | None:
        """Return the strategy frozen for the current direct child generation."""

        return self._direct_model_binding_strategy

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

    def on_transport_closed(self, handler: Callable[[], None]) -> None:
        """Notify the process owner when the ACP child stream closes unexpectedly."""

        self._transport.on_close(handler)

    async def start(self, env: dict[str, str] | None = None) -> None:
        """Start the language server and complete ACP initialization.

        Args:
            env: Environment variables for the subprocess.  If None, the
                current process environment is inherited.
        """
        self._transport.on_notification(self._handle_notification)
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
        self._model_bindings.clear()
        self._provisional_session_ids.clear()
        self._session_new_response_ids.clear()
        self._sessions.clear()
        self._direct_model_binding_strategy = None
        self._direct_model_binding_negotiation = None
        self._direct_model_binding_generation += 1

    async def create_session(self, cwd: str, model_id: str | None = None) -> str:
        """Create a new ACP session.

        Returns the session ID. If model_id is provided, the model is
        set after session creation.
        """
        params = {"cwd": cwd, "mcpServers": []}
        logger.debug(
            "session/new request: cwd_present=%s mcp_server_count=0",
            bool(cwd),
        )
        try:
            result = await self._transport.send_request("session/new", params)
        except AcpError as e:
            logger.error(
                "session/new failed: error_type=%s", type(e).__name__
            )
            raise
        logger.debug(
            "session/new response: session_id_present=%s models_present=%s",
            isinstance(result.get("sessionId"), str),
            "models" in result,
        )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session/new omitted a non-empty sessionId")
        self._bind_provisional_session(session_id)

        session_current_model: str | None = None
        session_available_models: frozenset[str] | None = None
        # Extract the global catalog while retaining evidence from this exact
        # session separately. A later direct session must never inherit the
        # catalog probe's current model when its own response omits that state.
        if "models" in result:
            models_data = result["models"]
            if not self._sessions:
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
            model_id=session_current_model,
            available_model_ids=session_available_models,
        )
        self._sessions[session_id] = session

        # Bind through the strategy negotiated before HTTP readiness.
        if model_id:
            self._require_direct_session_catalog(session_id, model_id)
            await self._bind_direct_model(session_id, model_id)

        logger.info(
            "Created direct ACP session: model_bound=%s",
            session.model_id is not None,
        )
        return session_id

    async def create_session_exact(
        self, cwd: str, model_id: str
    ) -> AcpSessionDescriptor:
        """Create a session and settle its requested model binding.

        The client applies the startup-negotiated strategy to every logical
        session before it is returned. Standard configuration requires an
        explicitly reported matching current value. The Copilot-specific
        strategy requires successful settlement of the exact
        ``session/set_model`` request.
        """

        if self._direct_model_binding_strategy is None:
            raise ModelAcknowledgementError(
                "direct model binding strategy was not negotiated before session creation"
            )
        available = {model.model_id for model in self._models}
        if model_id not in available:
            raise ValueError(
                f"Model {model_id!r} is not advertised. Available: {sorted(available)}"
            )
        session_id = await self.create_session(cwd)
        session = self._require_direct_session_catalog(session_id, model_id)
        await self._bind_direct_model(session_id, model_id)
        bound_model = session.model_id
        if bound_model is None or bound_model != model_id:
            raise ModelAcknowledgementError(
                "copilot-language-server did not settle the requested session model"
            )
        return AcpSessionDescriptor(session_id=session_id, model_id=bound_model)

    async def negotiate_direct_model_binding(
        self, session_id: str, model_id: str
    ) -> DirectModelBindingStrategy:
        """Select and freeze one direct model-binding strategy before readiness."""

        if self._direct_model_binding_strategy is not None:
            raise RuntimeError("direct model binding strategy is already negotiated")
        if self._direct_model_binding_negotiation is not None:
            raise RuntimeError("direct model binding strategy negotiation is in progress")
        self._require_direct_session_catalog(session_id, model_id)

        generation = self._direct_model_binding_generation
        negotiation = object()
        self._direct_model_binding_negotiation = negotiation

        try:
            strategy: DirectModelBindingStrategy | None = None
            try:
                await self._set_config_option_exact(session_id, model_id)
            except AcpError as exc:
                error_code = exc.error_obj.get("code")
                if type(error_code) is not int or error_code != -32601:
                    logger.error(
                        "Standard direct model binding negotiation was rejected"
                    )
                    raise ModelAcknowledgementError(
                        "copilot-language-server rejected standard model binding negotiation"
                    ) from None
            else:
                strategy = DirectModelBindingStrategy.STANDARD_CONFIG

            if strategy is None:
                try:
                    await self._set_copilot_model_settled(session_id, model_id)
                except AcpError:
                    logger.error("Copilot direct model binding negotiation was rejected")
                    raise ModelAcknowledgementError(
                        "copilot-language-server exposes no usable session model selector"
                    ) from None
                strategy = DirectModelBindingStrategy.COPILOT_SET_MODEL

            if (
                self._direct_model_binding_generation != generation
                or self._direct_model_binding_negotiation is not negotiation
            ):
                raise ModelAcknowledgementError(
                    "direct model binding negotiation was interrupted by child teardown"
                ) from None
            self._direct_model_binding_strategy = strategy
            logger.info("Negotiated direct model binding strategy: %s", strategy)
            return strategy
        finally:
            if self._direct_model_binding_negotiation is negotiation:
                self._direct_model_binding_negotiation = None

    def _require_direct_session_catalog(
        self, session_id: str, model_id: str
    ) -> SessionState:
        session = self._sessions.get(session_id)
        if (
            session is None
            or not isinstance(session.model_id, str)
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
        return session

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

    async def prompt_blocks(
        self,
        session_id: str,
        blocks: list[dict[str, Any]],
        *,
        timeout_s: float | None = None,
        event_byte_limit: int = 4_000_000,
        event_count_limit: int = 4096,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send the direct service's already-layered ACP text content blocks."""

        if not blocks or any(block.get("type") != "text" for block in blocks):
            raise ValueError("direct v1 accepts one or more ACP text content blocks")
        async for update in self._prompt_content(
            session_id,
            blocks,
            timeout_s=timeout_s,
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
                    logger.error(
                        "Direct ACP prompt timed out after %.1fs; "
                        "partial_text_chars=%d",
                        effective_timeout,
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
            if not isinstance(stop_reason, str) or stop_reason not in DIRECT_STOP_REASONS:
                raise RuntimeError(
                    "direct ACP prompt omitted a known non-empty stopReason"
                )
            yield {"done": True, "stopReason": stop_reason}

        finally:
            self._update_queues.pop(session_id, None)
            self._direct_update_budgets.pop(session_id, None)
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

        self._require_direct_session_catalog(session_id, model_id)
        await self._bind_direct_model(session_id, model_id)

    async def _initialize(self) -> None:
        """Complete the ACP initialization handshake."""
        client_capabilities: dict[str, Any]
        client_capabilities = {}
        result = await self._transport.send_request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "acp-proxy", "version": __version__},
                "clientCapabilities": client_capabilities,
            },
        )
        logger.debug(
            "initialize response received: protocol_type=%s "
            "agent_info_present=%s capabilities_present=%s auth_present=%s",
            type(result.get("protocolVersion")).__name__,
            isinstance(result.get("agentInfo"), dict),
            isinstance(result.get("agentCapabilities"), dict),
            bool(result.get("authMethods", result.get("signin"))),
        )
        info = result.get("agentInfo", {})
        protocol_version = result.get("protocolVersion")
        if type(protocol_version) is not int or protocol_version != 1:
            raise RuntimeError("direct ACP protocol version mismatch")
            raise RuntimeError(
                f"ACP protocol version mismatch: expected 1, got {protocol_version!r}"
            )
        self._protocol_version = protocol_version
        self._agent_name = info.get("name")
        self._agent_version = info.get("version")
        logger.info("Initialized direct ACP agent: protocol=%s", protocol_version)
        # Log capabilities for debugging environment differences
        caps = result.get("agentCapabilities")
        if not isinstance(caps, dict):
            raise TypeError("initialize response omitted agentCapabilities")
        self._agent_capabilities = dict(caps)
        logger.debug("Server capabilities received: count=%d", len(caps))
        auth = result.get("authMethods", result.get("signin", {}))
        if auth:
            logger.debug("ACP auth methods reported")

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

    async def _bind_direct_model(self, session_id: str, model_id: str) -> None:
        """Bind one direct session with the frozen generation strategy."""

        strategy = self._direct_model_binding_strategy
        if strategy is None:
            raise ModelAcknowledgementError(
                "direct model binding strategy was not negotiated before session creation"
            )
        try:
            if strategy is DirectModelBindingStrategy.STANDARD_CONFIG:
                await self._set_config_option_exact(session_id, model_id)
            elif strategy is DirectModelBindingStrategy.COPILOT_SET_MODEL:
                await self._set_copilot_model_settled(session_id, model_id)
            else:  # pragma: no cover - enum state is closed by construction
                raise AssertionError("unknown direct model binding strategy")
        except AcpError:
            logger.error("Frozen direct model binding strategy was rejected")
            raise ModelAcknowledgementError(
                "copilot-language-server rejected the requested session model"
            ) from None

        logger.info("Bound requested direct model via %s", strategy)

    async def _set_config_option_exact(
        self, session_id: str, model_id: str
    ) -> str:
        """Apply standard model configuration and verify its reported post-state."""

        binding = self._begin_model_binding(
            session_id, model_id, "session/set_config_option"
        )
        try:
            result = await self._transport.send_request(
                binding.method, binding.params
            )
        finally:
            if self._model_bindings.get(session_id) is binding:
                self._model_bindings.pop(session_id)

        observed = self._model_from_config_options(result)
        if observed != model_id:
            if session_id in self._sessions:
                self._sessions[session_id].model_id = None
            raise ModelAcknowledgementError(
                "copilot-language-server did not acknowledge the requested exact model"
            )
        if session_id in self._sessions:
            self._sessions[session_id].model_id = observed
        return observed

    async def _set_copilot_model_settled(
        self, session_id: str, model_id: str
    ) -> None:
        """Apply Copilot model selection and require successful RPC settlement."""

        binding = self._begin_model_binding(session_id, model_id, "session/set_model")
        try:
            await self._transport.send_request(
                binding.method, binding.params
            )
        finally:
            if self._model_bindings.get(session_id) is binding:
                self._model_bindings.pop(session_id)
        if session_id in self._sessions:
            self._sessions[session_id].model_id = model_id

    def _begin_model_binding(
        self, session_id: str, model_id: str, method: str
    ) -> _ModelBinding:
        """Capture this session's prior state before dispatching its selector."""

        if session_id in self._model_bindings:
            raise RuntimeError("model binding is already in progress for this session")
        session = self._sessions.get(session_id)
        params: dict[str, Any] = {"sessionId": session_id}
        if method == "session/set_config_option":
            params.update(configId="model", value=model_id)
        else:
            params["modelId"] = model_id
        binding = _ModelBinding(
            prior_model=session.model_id if session is not None else None,
            target_model=model_id,
            method=method,
            params=params,
        )
        self._model_bindings[session_id] = binding
        return binding

    def _observe_model_binding_response(
        self, message: dict[str, Any], method: str, params: dict[str, Any] | None
    ) -> None:
        """End the old-model allowance before any following wire notification."""

        if not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        if not isinstance(session_id, str):
            return
        binding = self._model_bindings.get(session_id)
        if binding is None:
            return
        # The transport retains the original params with the JSON-RPC request
        # ID. Identity also distinguishes an earlier cancelled request with the
        # same session, method, and target from the current binding attempt.
        if binding.method != method or binding.params is not params:
            self._transport.fail_closed(
                "direct ACP model binding response correlation failure"
            )
            return
        if "error" in message:
            # Rejection does not establish the target. Restore the prior-model
            # invariant now, including before a method-not-found fallback runs.
            self._model_bindings.pop(session_id)
        else:
            # The coroutine still validates the exact acknowledgement. Meanwhile
            # only the target may appear after this response, even when several
            # messages are buffered and the coroutine has not resumed yet.
            binding.response_received = True

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Route incoming notifications to the appropriate session queue."""
        method = msg.get("method", "")
        params = msg.get("params", {})

        self._log_direct_session_update(method, params)
        if not self._direct_model_integrity_holds(params):
            return
        if self._handle_direct_state_update(method, params):
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

    def _log_direct_session_update(self, method: Any, params: Any) -> None:
        """Log correlation-safe structure for a direct session update."""

        if method != "session/update" or not logger.isEnabledFor(logging.DEBUG):
            return
        session_id = params.get("sessionId") if isinstance(params, dict) else None
        update = params.get("update") if isinstance(params, dict) else None
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if not isinstance(kind, str):
            kind = "invalid"
        elif kind not in DIRECT_SESSION_UPDATE_TYPES:
            kind = "unrecognized"
        known_session = isinstance(session_id, str) and session_id in self._sessions
        provisional_session = (
            isinstance(session_id, str) and session_id in self._provisional_session_ids
        )
        response_observed = (
            isinstance(session_id, str)
            and session_id in self._session_new_response_ids
        )
        prompt_phase = (
            self._direct_prompt_phases.get(session_id)
            if isinstance(session_id, str)
            else None
        )
        if prompt_phase not in {"preparing", "active", "terminal"}:
            prompt_phase = "none"
        pending_session_new = self._transport.pending_request_count("session/new")
        if type(pending_session_new) is not int:
            pending_session_new = -1
        logger.debug(
            "Direct ACP session update: kind=%s session_id_present=%s "
            "session_known=%s provisional_session=%s response_observed=%s "
            "prompt_phase=%s "
            "pending_session_new=%d update_queue_present=%s",
            kind,
            isinstance(session_id, str) and bool(session_id),
            known_session,
            provisional_session,
            response_observed,
            prompt_phase,
            pending_session_new,
            isinstance(session_id, str) and session_id in self._update_queues,
        )
        if kind == "config_option_update" and isinstance(update, dict):
            self._log_direct_config_option_shape(update)

    @staticmethod
    def _log_direct_config_option_shape(update: dict[str, Any]) -> None:
        """Log bounded semantic counts for a configuration snapshot."""

        options = update.get("configOptions")
        if not isinstance(options, list):
            logger.debug("Direct ACP config option shape: options_list=False")
            return
        invalid_options = 0
        model_options = 0
        mode_options = 0
        thought_level_options = 0
        other_options = 0
        for option in options:
            if not isinstance(option, dict):
                invalid_options += 1
                continue
            option_id = option.get("id")
            category = option.get("category")
            if option_id == "model" or category == "model":
                model_options += 1
            elif option_id == "mode" or category == "mode":
                mode_options += 1
            elif option_id == "reasoning_effort" or category == "thought_level":
                thought_level_options += 1
            else:
                other_options += 1
        logger.debug(
            "Direct ACP config option shape: options_list=True option_count=%d "
            "invalid_options=%d model_options=%d mode_options=%d "
            "thought_level_options=%d other_options=%d",
            len(options),
            invalid_options,
            model_options,
            mode_options,
            thought_level_options,
            other_options,
        )

    def _direct_model_integrity_holds(self, params: Any) -> bool:
        """Validate selected-model state against the ordered binding transition."""

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
        binding = self._model_bindings.get(session_id)
        selected_model = (
            binding.target_model
            if binding is not None
            else session.model_id if session is not None else None
        )
        observed_model: str | None = None
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
                if (
                    not isinstance(current, str)
                    or not current
                    or (observed_model is not None and current != observed_model)
                ):
                    self._transport.fail_closed(
                        "direct ACP config update malformed"
                    )
                    return False
                observed_model = current
                prior_model_pending = (
                    binding is not None
                    and not binding.response_received
                    and current == binding.prior_model
                )
                if prior_model_pending and current != selected_model:
                    logger.debug(
                        "Direct ACP model binding accepted prior-model snapshot "
                        "before response"
                    )
                if (
                    isinstance(selected_model, str)
                    and current != selected_model
                    and not prior_model_pending
                ):
                    self._transport.fail_closed(
                        "direct ACP selected model drifted"
                    )
                    return False
        if isinstance(selected_model, str) and selected_model and observed_model is None:
            self._transport.fail_closed(
                "direct ACP selected model state missing"
            )
            return False
        return True

    def _handle_direct_state_update(self, method: Any, params: Any) -> bool:
        """Validate and discard correlated session state outside active prompts."""

        if method != "session/update" or not isinstance(params, dict):
            return False
        session_id = params.get("sessionId")
        update = params.get("update")
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(update, dict)
            or not isinstance(kind, str)
            or kind not in _DIRECT_SESSION_STATE_UPDATE_TYPES
            or self._direct_prompt_phases.get(session_id) == "active"
        ):
            return False
        if not self._is_bounded_direct_state_update(kind, update):
            return False
        if (
            session_id not in self._sessions
            and session_id not in self._session_new_response_ids
            and not self._admit_provisional_session_id(session_id)
        ):
            return False
        return True

    @staticmethod
    def _is_bounded_direct_state_update(kind: str, update: dict[str, Any]) -> bool:
        """Validate one recognized state envelope without retaining its payload."""

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
        if control_bytes > _MAX_DIRECT_CONTROL_UPDATE_BYTES:
            return False
        if kind == "available_commands_update":
            commands = update.get("availableCommands")
            return (
                isinstance(commands, list)
                and len(commands) <= _MAX_DIRECT_AVAILABLE_COMMANDS
                and all(isinstance(command, dict) for command in commands)
            )
        if kind == "config_option_update":
            options = update.get("configOptions")
            return isinstance(options, list) and all(
                isinstance(option, dict) for option in options
            )
        if kind == "current_mode_update":
            current_mode = update.get("currentModeId")
            return isinstance(current_mode, str) and bool(current_mode)
        # usage_update and session_info_update are recognized ACP session-state
        # envelopes, but Meadow v1 derives no claim from their payload fields.
        return kind in {"usage_update", "session_info_update"}

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
        """Resolve creation-correlated state to the returned session identity."""

        provisional = self._provisional_session_ids
        self._session_new_response_ids.discard(session_id)
        if session_id in provisional:
            provisional.remove(session_id)
            return
        if provisional and self._transport.pending_request_count("session/new") == 0:
            provisional.clear()
            self._session_new_response_ids.clear()
            self._transport.fail_closed(
                "direct ACP provisional session identity mismatch"
            )
            raise ConnectionError(
                "direct ACP provisional session identity mismatch"
            )

    def _observe_session_new_response(self, message: dict[str, Any]) -> None:
        """Bridge session state from response dispatch to coroutine registration."""

        pending_creates = self._transport.pending_request_count("session/new")
        if type(pending_creates) is not int or pending_creates <= 0:
            self._transport.fail_closed(
                "direct ACP session/new response correlation failure"
            )
            return
        remaining_creates = pending_creates - 1
        result = message.get("result")
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        provisional = self._provisional_session_ids

        if isinstance(session_id, str) and session_id:
            response_ids = self._session_new_response_ids
            if session_id in self._sessions or session_id in response_ids:
                self._transport.fail_closed(
                    "direct ACP session/new response identity failure"
                )
                return
            provisional.discard(session_id)
            if len(provisional) > remaining_creates:
                provisional.clear()
                response_ids.clear()
                self._transport.fail_closed(
                    "direct ACP provisional session identity mismatch"
                )
                return
            response_ids.add(session_id)
            return

        if "error" not in message:
            provisional.clear()
            self._session_new_response_ids.clear()
            self._transport.fail_closed(
                "direct ACP session/new response identity failure"
            )
            return

        # An error response has no session identity. Any excess provisional ID
        # must therefore have belonged to the request that just settled.
        if len(provisional) > remaining_creates:
            provisional.clear()
            self._session_new_response_ids.clear()
            self._transport.fail_closed(
                "direct ACP provisional session identity mismatch"
            )

    def _enqueue_direct_update(
        self, session_id: str, update: dict[str, Any]
    ) -> bool:
        """Bound direct evidence before retaining it in the reader-side queue."""

        budget = self._direct_update_budgets.get(session_id)
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

        Each ACP session has one active prompt queue. Unknown,
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
        if self._direct_prompt_phases.get(session_id) != "active":
            return False
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str) or kind not in DIRECT_SESSION_UPDATE_TYPES:
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
        """Order session creation, model binding, and prompt settlement."""

        if method == "session/new":
            self._observe_session_new_response(_message)
            return
        if method in {"session/set_config_option", "session/set_model"}:
            self._observe_model_binding_response(_message, method, params)
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
            or self._direct_prompt_phases.get(session_id) != "active"
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

    def _handle_agent_request(self, msg: dict[str, Any]) -> dict[str, object]:
        """Cancel permission requests and deny unadvertised workspace callbacks."""
        method = msg.get("method", "")

        if method == "session/request_permission":
            return {"outcome": {"outcome": "cancelled"}}
        raise PermissionError(
            f"ACP callback {method!r} was not advertised in Meadow direct mode"
        )

    def _observe_agent_request(self, msg: dict[str, Any]) -> None:
        """Queue sanitized direct callback evidence at transport-read order."""

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
            or self._direct_prompt_phases.get(session_id) != "active"
        ):
            self._transport.fail_closed(
                "direct ACP callback correlation protocol failure"
            )
            return
        queue = self._update_queues.get(session_id)
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
