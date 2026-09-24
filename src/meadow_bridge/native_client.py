"""Native IDE session ownership and truthful prompt/effect settlement."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .json_types import JsonObject, JsonValue, json_object, json_text, required_string
from .native_transport import (
    NativeNotification, NativeProtocolError, NativeRequest, NativeRpcError,
    NativeTransport, NativeTransportError, PendingRequest, RpcResponse,
)
from .native_types import (
    AllocatedBinding, ConversationBinding, NativeBinding, NativeCancelled,
    NativeCompleted, NativeEffectObservation, NativeEvent, NativeFailed,
    NativeModel, NativeObservation, NativePermissionObservation, NativeServerInfo,
    NativeTerminal, NativeToolObservation, NativeUnsettledError,
)
from .permission_policy import PermissionPolicy
from .owned_commands import ShellSpec
from .workspace_tools import (
    FileOutcome, ToolArgumentError, ToolFailure, WorkspaceCancelled,
    WorkspaceOperation, WorkspaceResult, WorkspaceTools,
)

logger = logging.getLogger(__name__)


@dataclass
class _Session:
    binding: NativeBinding
    cwd: Path
    tools: WorkspaceTools
    retired: bool = False
    effect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class _Turn:
    session: _Session
    token: str
    event_byte_limit: int
    event_count_limit: int
    response_byte_limit: int
    request: PendingRequest | None = None
    begun: bool = False
    ended: bool = False
    rpc_ended: bool = False
    cancel_requested: bool = False
    cancel_reason: str | None = None
    native_error: str | None = None
    overflow: bool = False
    event_bytes: int = 0
    response_bytes: int = 0
    response: list[str] = field(default_factory=list)
    events: list[NativeEvent] = field(default_factory=list)
    tools: dict[str, NativeToolObservation] = field(default_factory=dict)
    permissions: list[NativePermissionObservation] = field(default_factory=list)
    effects: list[NativeEffectObservation] = field(default_factory=list)
    callback_phases: set[tuple[str, bool]] = field(default_factory=set)
    callback_inputs: dict[str, str] = field(default_factory=dict)
    cancellation: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    def observation(self, *, settled: bool) -> NativeObservation:
        return NativeObservation(
            self.session.binding, "".join(self.response), tuple(self.events),
            tuple(self.tools.values()), tuple(self.permissions), tuple(self.effects),
            settled and not self.overflow,
        )


class NativeClient:
    """One initialized server with allocation-only logical session admission."""

    def __init__(self, binary_path: str, *, cwd: str | Path,
                 env: Mapping[str, str] | None = None,
                 command_env: Mapping[str, str] | None = None,
                 raw_event_file: str | None = None, request_timeout: float = 30,
                 arguments: tuple[str, ...] = ("--stdio",), shell: ShellSpec | None = None) -> None:
        self.workspace = Path(cwd).resolve()
        self._env = dict(env) if env is not None else None
        self._command_env = dict(os.environ if command_env is None else command_env)
        self._request_timeout = request_timeout
        self._shell = shell
        self._transport = NativeTransport(binary_path, cwd=self.workspace,
                                          arguments=arguments, raw_event_file=raw_event_file)
        self._transport.on_notification(self._notification)
        self._transport.on_request(self._callback)
        self._sessions: dict[str, _Session] = {}
        self._active: _Turn | None = None
        self._models: tuple[NativeModel, ...] = ()
        self._server_info: NativeServerInfo | None = None
        self._started = False
        self._stopped = False
        self._stop_lock = asyncio.Lock()
        self._cleanup: asyncio.Task[None] | None = None

    @property
    def is_alive(self) -> bool:
        """Only a completely initialized, continuous generation is ready."""
        return self._started and not self._stopped and self._transport.is_open

    @property
    def models(self) -> tuple[NativeModel, ...]:
        """Native advertised models, without a synthetic default/fallback."""
        return self._models

    @property
    def server_info(self) -> NativeServerInfo:
        """Return startup identity only after actual initialization evidence."""
        if self._server_info is None:
            raise NativeProtocolError("Native server has not initialized")
        return self._server_info

    def on_transport_closed(self, handler: Callable[[str], None]) -> None:
        """Register admission revocation; the observer must schedule shutdown."""
        self._transport.on_transport_closed(handler)

    async def start(self, env: Mapping[str, str] | None = None) -> None:
        """Initialize/models/tools only; startup performs no model inference."""
        if env is not None:
            self._env = dict(env)
        try:
            await self._transport.start(self._env)
            folders: list[JsonValue] = [{"uri": self.workspace.as_uri(), "name": self.workspace.name}]
            response = json_object(await self._transport.request("initialize", {
                "rootUri": self.workspace.as_uri(), "workspaceFolders": folders,
                "capabilities": {"workspace": {"workspaceFolders": True}, "window": {"workDoneProgress": True}},
                "initializationOptions": {
                    "copilotCapabilities": {"mcpServerManagement": False, "mcpElicitation": False, "mcpSampling": False, "subAgent": False},
                    "editorInfo": {"name": "Meadow Bridge", "version": "2"},
                    "editorPluginInfo": {"name": "Meadow native IDE", "version": "2"},
                },
            }, timeout=self._request_timeout), "initialize result")
            info = json_object(response.get("serverInfo"), "serverInfo")
            self._server_info = NativeServerInfo(required_string(info, "name"), required_string(info, "version"))
            await self._transport.notify("initialized", {})
            await self._transport.notify("workspace/didChangeConfiguration", {
                "settings": {"telemetry": {"telemetryLevel": "off"}, "github": {"copilot": {
                    "mcp": "{}", "enableBuiltInGitHubMcpServer": False,
                }}},
            })
            await self._check_mcp()
            catalog = await self._transport.request("copilot/models", {}, timeout=self._request_timeout)
            if not isinstance(catalog, list) or not catalog:
                raise NativeProtocolError("Native model catalog is absent")
            models: list[NativeModel] = []
            for item in catalog:
                model = json_object(item, "model catalog entry")
                identifier = required_string(model, "id")
                scopes = model.get("scopes")
                if identifier == "auto" or not isinstance(scopes, list) or "agent-panel" not in scopes:
                    continue
                models.append(NativeModel(identifier, required_string(model, "modelName")))
            if not models:
                raise NativeProtocolError("Native catalog has no concrete agent-panel model")
            if len({model.id for model in models}) != len(models):
                raise NativeProtocolError("Native model catalog contains duplicate identities")
            self._models = tuple(models)
            definitions: list[JsonValue] = [definition.to_json() for definition in WorkspaceTools.definitions()]
            catalog = await self._transport.request("conversation/registerTools", {"tools": definitions}, timeout=self._request_timeout)
            if not isinstance(catalog, list):
                raise NativeProtocolError("Native tool catalog must be an array")
            registered: dict[str, JsonObject] = {}
            for item in catalog:
                tool = json_object(item, "registered tool")
                name = required_string(tool, "name")
                if name in registered:
                    raise NativeProtocolError("Native tool catalog contains duplicate identities")
                registered[name] = tool
            for definition in WorkspaceTools.definitions():
                required_tool = registered.get(definition.name)
                if required_tool is None or required_tool.get("status") != "enabled" or required_tool.get("type") != "client":
                    raise NativeProtocolError("Native server did not enable every required client tool")
                provider = json_object(required_tool.get("toolProvider"), "native tool provider")
                if provider.get("id") != "copilot-editor":
                    raise NativeProtocolError("Native client tool has an unexpected owner")
            await self._check_mcp()
            self._started = True
        except BaseException:
            await self.stop()
            raise

    async def _check_mcp(self) -> None:
        catalog = await self._transport.request("mcp/getTools", {}, timeout=self._request_timeout)
        if catalog != []:
            raise NativeProtocolError("Native MCP catalog is not empty")

    def allocate_session(self, logical_id: str, cwd: str | Path,
                         model_id: str, policy: PermissionPolicy) -> None:
        """Reserve a logical session; no conversation/create or inference occurs."""
        if not self.is_alive or not logical_id or logical_id in self._sessions:
            raise NativeProtocolError("Cannot allocate this native logical session")
        workspace = Path(cwd).resolve()
        if workspace != self.workspace or model_id not in {model.id for model in self._models}:
            raise NativeProtocolError("Session workspace or model was not admitted")
        self._sessions[logical_id] = _Session(
            AllocatedBinding(logical_id, model_id), workspace,
            WorkspaceTools(workspace, policy, env=self._command_env, shell=self._shell),
        )

    def binding(self, logical_id: str) -> NativeBinding:
        """Return observed backend identity, including first-turn failure evidence."""
        return self._session(logical_id, allow_retired=True).binding

    def _session(self, logical_id: str, *, allow_retired: bool = False) -> _Session:
        session = self._sessions.get(logical_id)
        if session is None or (session.retired and not allow_retired):
            raise NativeProtocolError("Unknown or retired native logical session")
        return session

    async def run_turn(self, logical_id: str, text: str, timeout_s: float,
                       event_byte_limit: int, event_count_limit: int,
                       response_byte_limit: int) -> NativeTerminal:
        """Submit new text once, retaining correlation through effect settlement."""
        if not self.is_alive or self._active is not None:
            raise NativeProtocolError("Native prompts must be globally serialized")
        if min(timeout_s, event_byte_limit, event_count_limit, response_byte_limit) <= 0:
            raise ValueError("Native prompt limits must be positive")
        session = self._session(logical_id)
        context = _Turn(session, str(uuid4()), event_byte_limit, event_count_limit, response_byte_limit)
        self._active = context
        common: JsonObject = {
            "workDoneToken": context.token, "chatMode": "Agent",
            "modelInfo": {"id": session.binding.model_id},
            "workspaceFolder": session.cwd.as_uri(),
            "workspaceFolders": [{"uri": session.cwd.as_uri(), "name": session.cwd.name}],
            "capabilities": {"allSkills": False, "skills": []},
            "needToolCallConfirmation": True, "source": "panel", "computeSuggestions": False,
        }
        if isinstance(session.binding, AllocatedBinding):
            method = "conversation/create"
            common["turns"] = [{"request": text}]
        else:
            method = "conversation/turn"
            common.update(conversationId=session.binding.conversation_id, message=text)
        try:
            request = self._transport.reserve(method, common, lambda response: self._terminal_rpc(context, response))
            context.request = request
            await self._transport.dispatch(request)
            try:
                await asyncio.wait_for(self._transport.wait(request), timeout_s)
            except TimeoutError:
                await self._signal_cancel(context)
                await asyncio.wait_for(self._transport.wait(request), self._request_timeout)
            except NativeRpcError:
                # A correlated RPC error is retained separately from transport loss.
                if not context.ended:
                    raise NativeProtocolError("Native prompt error lacks terminal progress")
            if context.cancellation:
                await context.cancellation
            await self._transport.settle_callbacks()
            if not context.ended or not context.rpc_ended:
                raise NativeProtocolError("Native prompt did not fully terminate")
            # A disabled MCP manager can omit catalog-change notifications.
            await self._check_mcp()
            observation = context.observation(settled=True)
            if context.cancel_reason is not None:
                return NativeCancelled(observation, "evidence_limit" if context.overflow else context.cancel_reason)
            if context.overflow or context.cancel_requested:
                raise NativeProtocolError("Native request did not acknowledge requested cancellation")
            if context.native_error is not None:
                return NativeFailed(observation, context.native_error)
            return NativeCompleted(observation)
        except asyncio.CancelledError as error:
            self._transport.abort("Native prompt owner cancelled before settlement")
            await self.stop()
            raise NativeUnsettledError("Native prompt owner cancelled before settlement", context.observation(settled=False)) from error
        except Exception as error:
            self._transport.abort("Native prompt lost settlement: " + str(error))
            await self.stop()
            raise NativeUnsettledError(str(error), context.observation(settled=False)) from error
        finally:
            context.done.set()
            self._active = None

    def _terminal_rpc(self, context: _Turn, response: RpcResponse) -> None:
        if context is not self._active or context.rpc_ended:
            raise NativeProtocolError("Uncorrelated native terminal RPC")
        if not context.ended:
            raise NativeProtocolError("Native terminal RPC overtook terminal progress")
        if response.is_error:
            context.native_error = json_text(response.value)
        else:
            value = json_object(response.value, "native prompt result")
            self._identity(context, value)
            info = json_object(value.get("modelInfo"), "result modelInfo")
            model = next(model for model in self._models if model.id == context.session.binding.model_id)
            if info.get("id") != model.id or value.get("modelName") != model.name:
                raise NativeProtocolError("Native result model differs from the admitted model")
        context.rpc_ended = True

    def _identity(self, context: _Turn, value: JsonObject) -> None:
        binding = context.session.binding
        if not isinstance(binding, ConversationBinding) or value.get("conversationId") != binding.conversation_id or value.get("turnId") != binding.turn_id:
            raise NativeProtocolError("Native conversation/turn identity changed")

    def _retain(self, context: _Turn, kind: str, value: JsonObject) -> bool:
        encoded = json_text(value)
        size = len(encoded.encode("utf-8"))
        if context.overflow or len(context.events) >= context.event_count_limit or context.event_bytes + size > context.event_byte_limit:
            context.overflow = True
            self._schedule_cancel(context)
            return False
        context.events.append(NativeEvent(kind, encoded))
        context.event_bytes += size
        return True

    def _notification(self, notification: NativeNotification) -> None:
        if notification.method == "copilot/mcpTools":
            if notification.params != {"servers": []}:
                raise NativeProtocolError("Native MCP tools appeared during the generation")
            return
        if notification.method == "$/cancelRequest":
            raise NativeProtocolError("Native server cancelled a client callback")
        if notification.method != "$/progress":
            logger.debug("Native notification observed: %s", notification.method)
            return
        context = self._active
        params = json_object(notification.params, "native progress params")
        if context is None or params.get("token") != context.token or context.rpc_ended or context.ended:
            raise NativeProtocolError("Stale or uncorrelated native progress")
        value = json_object(params.get("value"), "native progress value")
        kind = value.get("kind")
        if kind == "begin":
            if context.begun:
                raise NativeProtocolError("Repeated native turn begin")
            conversation = required_string(value, "conversationId")
            turn = required_string(value, "turnId")
            prior = context.session.binding
            if isinstance(prior, ConversationBinding) and (prior.conversation_id != conversation or prior.turn_id == turn):
                raise NativeProtocolError("Native continuation changed conversation or reused a turn")
            context.session.binding = ConversationBinding(prior.logical_session_id, prior.model_id, conversation, turn)
            context.begun = True
        elif kind in {"report", "end"}:
            self._identity(context, value)
        else:
            raise NativeProtocolError("Unknown native progress kind")
        self._retain(context, "native.progress." + str(kind), value)
        if not context.overflow:
            self._observe_report(context, value)
        if kind == "end":
            context.ended = True
            if "cancellationReason" in value:
                context.cancel_reason = required_string(value, "cancellationReason")
                self._schedule_cancel(context)
            if "error" in value:
                context.native_error = json_text(value["error"])

    def _observe_report(self, context: _Turn, value: JsonObject) -> None:
        rounds = value.get("editAgentRounds", [])
        if not isinstance(rounds, list):
            raise NativeProtocolError("Native editAgentRounds must be an array")
        chunks = [value, *(json_object(item, "native round") for item in rounds)]
        for chunk in chunks:
            if "reply" in chunk:
                reply = chunk["reply"]
                if not isinstance(reply, str):
                    raise NativeProtocolError("Native reply must be text")
                size = len(reply.encode("utf-8"))
                if context.response_bytes + size > context.response_byte_limit:
                    context.overflow = True
                    self._schedule_cancel(context)
                elif not context.overflow:
                    context.response.append(reply)
                    context.response_bytes += size
            tools = chunk.get("toolCalls", [])
            if not isinstance(tools, list):
                raise NativeProtocolError("Native toolCalls must be an array")
            for item in tools:
                tool = json_object(item, "native tool call")
                identifier = required_string(tool, "id")
                name, status = required_string(tool, "name"), required_string(tool, "status")
                scope: Literal["bridge", "server"] = "bridge" if tool.get("toolType") == "client" else "server"
                self._observe_tool(context, NativeToolObservation(identifier, name, status, scope))

    @staticmethod
    def _observe_tool(context: _Turn, tool: NativeToolObservation) -> None:
        prior = context.tools.get(tool.tool_call_id)
        if prior and (prior.name != tool.name or prior.scope != tool.scope):
            raise NativeProtocolError("Native tool identity changed")
        context.tools[tool.tool_call_id] = tool

    def _callback(self, request: NativeRequest) -> asyncio.Future[JsonValue] | asyncio.Task[JsonValue]:
        loop = asyncio.get_running_loop()
        if request.method == "workspace/workspaceFolders":
            future: asyncio.Future[JsonValue] = loop.create_future()
            future.set_result([{"uri": self.workspace.as_uri(), "name": self.workspace.name}])
            return future
        if request.method == "window/workDoneProgress/create":
            params = json_object(request.params, "progress creation")
            if set(params) != {"token"} or not isinstance(params["token"], (str, int)):
                raise NativeProtocolError("Invalid native progress creation")
            empty: asyncio.Future[JsonValue] = loop.create_future()
            empty.set_result(None)
            return empty
        if request.method not in {"conversation/invokeClientToolConfirmation", "conversation/invokeClientTool"}:
            raise NativeProtocolError("Unadvertised native callback: " + request.method)
        context = self._active
        if context is None or not context.begun or context.ended or context.rpc_ended or context.overflow or context.cancel_requested:
            raise NativeProtocolError("Native effect callback outside its active turn")
        params = json_object(request.params, "native tool callback")
        self._identity(context, params)
        confirmation = request.method.endswith("Confirmation")
        required = {"name", "input", "conversationId", "turnId", "roundId", "toolCallId"}
        allowed = required | ({"title", "message", "annotations", "toolMetadata"} if confirmation else set())
        round_id = params.get("roundId")
        if not required <= params.keys() or params.keys() - allowed or type(round_id) is not int or round_id < 0:
            raise NativeProtocolError("Malformed native tool callback envelope")
        identifier, name = required_string(params, "toolCallId"), required_string(params, "name")
        phase = (identifier, confirmation)
        if phase in context.callback_phases:
            raise NativeProtocolError("Repeated native tool callback phase")
        data = dict(json_object(params.get("input"), "native tool input"))
        if confirmation:
            if "toolType" in data and data.pop("toolType") not in {"safe_tool", "terminal"}:
                raise NativeProtocolError("Unknown native confirmation tool type")
            if "commandLineType" in data and data.pop("commandLineType") not in {"sh", "powershell", "cmd"}:
                raise NativeProtocolError("Unknown native command-line type")
        fingerprint = name + json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        prior_input = context.callback_inputs.get(identifier)
        if prior_input is not None and prior_input != fingerprint:
            raise NativeProtocolError("Native tool input changed between phases")
        if name not in {definition.name for definition in WorkspaceTools.definitions()}:
            raise NativeProtocolError("Unregistered native client tool")
        context.callback_phases.add(phase)
        context.callback_inputs[identifier] = fingerprint
        if not confirmation and self._retain(context, "native.client_tool.invocation", params):
            self._observe_tool(context, NativeToolObservation(identifier, name, "running", "bridge"))
        try:
            operation = context.session.tools.decode(name, data)
        except ToolArgumentError as error:
            invalid: asyncio.Future[JsonValue] = loop.create_future()
            result = WorkspaceResult(FileOutcome(()), ToolFailure("invalid_arguments", str(error)))
            if confirmation:
                self._retain(context, "native.client_tool.confirmation", {"toolCallId": identifier, "validation_error": result.to_json()})
                invalid.set_result([{"result": "reject"}, None])
            else:
                self._effect(context, identifier, result)
                invalid.set_result([{"content": [{"value": result.as_text()}], "status": "error"}, None])
            return invalid
        if confirmation:
            decision = context.session.tools.confirm(operation)
            retained = self._retain(context, "native.client_tool.confirmation", {"toolCallId": identifier, "decision": decision.as_json()})
            if retained:
                context.permissions.append(NativePermissionObservation(identifier, decision.allowed, decision.policy_digest))
            accepted: asyncio.Future[JsonValue] = loop.create_future()
            accepted.set_result([{"result": "accept" if decision.allowed and retained else "reject"}, None])
            return accepted
        return asyncio.create_task(self._execute(context, identifier, operation))

    async def _execute(self, context: _Turn, identifier: str, operation: WorkspaceOperation) -> JsonValue:
        async with context.session.effect_lock:
            return await self._execute_owned(context, identifier, operation)

    async def _execute_owned(self, context: _Turn, identifier: str, operation: WorkspaceOperation) -> JsonValue:
        if context.overflow or context.cancel_requested or not self._transport.is_open:
            result = WorkspaceResult(FileOutcome(()), ToolFailure("cancelled", "Invocation cancelled before effect admission"))
            self._effect(context, identifier, result)
            return [{"content": [{"value": result.as_text()}], "status": "cancelled"}, None]
        try:
            result = await context.session.tools.execute(operation)
        except WorkspaceCancelled as error:
            self._effect(context, identifier, error.result)
            raise
        self._effect(context, identifier, result)
        status = "success" if result.error is None else ("cancelled" if result.error.code == "cancelled" else "error")
        return [{"content": [{"value": result.as_text()}], "status": status}, None]

    def _effect(self, context: _Turn, identifier: str, result: WorkspaceResult) -> None:
        encoded = result.to_json()
        if self._retain(context, "native.client_tool.result", {"toolCallId": identifier, "result": encoded}):
            context.effects.append(NativeEffectObservation(identifier, json_text(encoded)))
            tool = context.tools.get(identifier)
            if tool is not None:
                status = "completed" if result.error is None else ("cancelled" if result.error.code == "cancelled" else "error")
                self._observe_tool(context, NativeToolObservation(identifier, tool.name, status, tool.scope))

    def _schedule_cancel(self, context: _Turn) -> None:
        if context.cancellation is None:
            context.cancellation = asyncio.create_task(self._signal_cancel(context))

    async def _signal_cancel(self, context: _Turn) -> None:
        already_requested = context.cancel_requested
        context.cancel_requested = True
        request = context.request
        if not already_requested and request is not None and request.sent and not context.rpc_ended and self._transport.is_open:
            await self._transport.notify("$/cancelRequest", {"id": request.id})
        await context.session.tools.cancel()

    async def cancel_session(self, logical_id: str) -> None:
        """Cancel the exact active request, then join its settlement owner."""
        self._session(logical_id)
        context = self._active
        if context is None or context.session.binding.logical_session_id != logical_id:
            return
        await self._signal_cancel(context)
        try:
            await asyncio.wait_for(context.done.wait(), self._request_timeout)
        except TimeoutError:
            self._transport.abort("Native cancellation did not settle")
            await self.stop()
            raise NativeTransportError("Native cancellation did not settle") from None

    async def retire_session(self, logical_id: str) -> None:
        """Release native in-memory conversation state; never claim disk deletion."""
        session = self._session(logical_id)
        if self._active and self._active.session is session:
            raise NativeProtocolError("Cannot retire an active native session")
        await session.tools.close()
        if isinstance(session.binding, ConversationBinding):
            result = await self._transport.request("conversation/destroy", {"conversationId": session.binding.conversation_id}, timeout=self._request_timeout)
            if result != "OK":
                self._transport.abort("Native conversation retirement was not acknowledged")
                raise NativeProtocolError("Native conversation retirement was not acknowledged")
        session.retired = True

    async def stop(self) -> None:
        """Stop effects before process/callback teardown; observers never await us."""
        if self._cleanup is None:
            self._started = False
            if self._active is not None:
                self._active.cancel_requested = True
            self._cleanup = asyncio.create_task(self._stop_owned())
        interrupted: asyncio.CancelledError | None = None
        while not self._cleanup.done():
            try:
                await asyncio.shield(self._cleanup)
            except asyncio.CancelledError as error:
                interrupted = error
        self._cleanup.result()
        if interrupted is not None:
            raise interrupted

    async def _stop_owned(self) -> None:
        async with self._stop_lock:
            if self._stopped:
                return
            self._started = False
            failures: list[Exception] = []
            for session in self._sessions.values():
                try:
                    await session.tools.close()
                except Exception as error:
                    failures.append(error)
            if self._transport.is_open and self._active is None:
                try:
                    await self._transport.request("shutdown", timeout=self._request_timeout)
                    self._transport.expect_exit()
                    await self._transport.notify("exit")
                except (NativeTransportError, NativeRpcError, TimeoutError):
                    logger.warning("Native shutdown did not acknowledge; settling owned process")
            try:
                await self._transport.stop()
            except Exception as error:
                failures.append(error)
            if failures:
                raise ExceptionGroup("Native cleanup did not settle", failures)
            self._stopped = True
