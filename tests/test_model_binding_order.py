"""Model-binding notification order at the real client/transport boundary."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from acp_proxy.client import (
    AcpClient,
    CallbackPolicy,
    DirectModelBindingStrategy,
    ModelAcknowledgementError,
)
from acp_proxy.transport import AcpTransport
from tests.test_transport import FakeProcess, FakeStdin


_STRATEGIES = [
    DirectModelBindingStrategy.STANDARD_CONFIG,
    DirectModelBindingStrategy.COPILOT_SET_MODEL,
]


def _options(model_id: str) -> list[dict[str, str]]:
    return [{"id": "model", "category": "model", "currentValue": model_id}]


class _FirstBlockedDrain(FakeStdin):
    """Expose cancellation after one request is written but before drain ends."""

    def __init__(self, written: list[bytes]) -> None:
        super().__init__()
        self.written = written
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_next = True

    async def drain(self) -> None:
        if self.block_next:
            self.block_next = False
            self.entered.set()
            await self.release.wait()


class _Peer:
    """Supply peer messages without replacing any client or transport logic.

    Synchronous dispatch deliberately models consecutive buffered wire messages
    before the request coroutine resumes, as the production reader can do.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = str(workspace)
        self.process = FakeProcess()
        self.transport = AcpTransport()
        self.transport._process = self.process  # type: ignore[assignment]
        self.client = AcpClient("unused", callback_policy=CallbackPolicy.DIRECT_DENY)
        self.client._transport = self.transport
        self.transport.set_strict_response_correlation(True)
        self.transport.on_notification(self.client._handle_notification)
        self.transport.on_response_observed(self.client._observe_response)
        self.transport.on_request_sent(self.client._observe_request_sent)
        self.close_events: list[str] = []
        self.transport.on_close(lambda: self.close_events.append("closed"))
        self.tasks: list[asyncio.Task[Any]] = []
        self.request_index = 0

    def start(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def request(self, expected_method: str) -> dict[str, Any]:
        async def wait_for_write() -> dict[str, Any]:
            while len(self.process.stdin.written) <= self.request_index:
                await asyncio.sleep(0)
            request = json.loads(self.process.stdin.written[self.request_index])
            self.request_index += 1
            assert request["method"] == expected_method
            return request

        return await asyncio.wait_for(wait_for_write(), timeout=1)

    def result(self, request: dict[str, Any], result: dict[str, Any]) -> None:
        self.transport._dispatch(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        )

    def error(self, request: dict[str, Any], code: int) -> None:
        self.transport._dispatch(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": code, "message": "peer rejected request"},
            }
        )

    def session_created(self, request: dict[str, Any], session_id: str) -> None:
        self.result(
            request,
            {
                "sessionId": session_id,
                "models": {
                    "availableModels": [
                        {"modelId": model_id, "name": model_id}
                        for model_id in ("default", "target", "third")
                    ],
                    "currentModelId": "default",
                },
            },
        )

    def config(self, session_id: str, options: list[Any]) -> None:
        self.transport._dispatch(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "config_option_update",
                        "configOptions": options,
                    },
                },
            }
        )

    async def catalog(self) -> None:
        task = self.start(self.client.create_session(self.workspace))
        request = await self.request("session/new")
        self.session_created(request, "catalog")
        assert await task == "catalog"

    async def negotiate(self, strategy: DirectModelBindingStrategy) -> None:
        await self.catalog()
        task = self.start(
            self.client.negotiate_direct_model_binding("catalog", "default")
        )
        request = await self.request("session/set_config_option")
        if strategy is DirectModelBindingStrategy.COPILOT_SET_MODEL:
            self.error(request, -32601)
            request = await self.request("session/set_model")
            self.result(request, {})
        else:
            self.result(request, {"configOptions": _options("default")})
        assert await task is strategy

    async def binding_request(
        self, strategy: DirectModelBindingStrategy
    ) -> dict[str, Any]:
        method = (
            "session/set_config_option"
            if strategy is DirectModelBindingStrategy.STANDARD_CONFIG
            else "session/set_model"
        )
        request = await self.request(method)
        assert request["params"]["sessionId"] == "live"
        selected = request["params"].get("value", request["params"].get("modelId"))
        assert selected == "target"
        return request

    def acknowledge(
        self, request: dict[str, Any], strategy: DirectModelBindingStrategy
    ) -> None:
        self.result(
            request,
            {"configOptions": _options("target")}
            if strategy is DirectModelBindingStrategy.STANDARD_CONFIG
            else {},
        )


@asynccontextmanager
async def _peer(workspace: Path) -> AsyncIterator[_Peer]:
    peer = _Peer(workspace)
    try:
        yield peer
    finally:
        for task in peer.tasks:
            if not task.done():
                task.cancel()
        # A failed assertion must still retrieve failures from pending RPCs.
        await asyncio.gather(*peer.tasks, return_exceptions=True)
        await peer.transport.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", _STRATEGIES)
@pytest.mark.parametrize(
    ("notification_phase", "notification_model"),
    [
        ("before_new_response", "default"),
        ("after_new_response", "default"),
        ("binding_pending", "default"),
        ("binding_pending", "target"),
    ],
)
async def test_exact_creation_accepts_correlated_initial_state_during_binding(
    tmp_path: Path,
    strategy: DirectModelBindingStrategy,
    notification_phase: str,
    notification_model: str,
) -> None:
    """Scheduling the initial snapshot cannot change exact creation's result."""

    async with _peer(tmp_path) as peer:
        await peer.negotiate(strategy)
        task = peer.start(peer.client.create_session_exact(peer.workspace, "target"))
        create = await peer.request("session/new")
        if notification_phase == "before_new_response":
            peer.config("live", _options(notification_model))
        peer.session_created(create, "live")
        if notification_phase == "after_new_response":
            assert not task.done()
            peer.config("live", _options(notification_model))
        binding = await peer.binding_request(strategy)
        if notification_phase == "binding_pending":
            peer.config("live", _options(notification_model))
        assert peer.transport.is_open
        peer.acknowledge(binding, strategy)
        descriptor = await task
        assert descriptor.session_id == "live"
        assert descriptor.model_id == "target"
        assert peer.transport.is_open
        assert peer.close_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", _STRATEGIES)
async def test_old_model_after_success_response_revokes_before_waiter_resumes(
    tmp_path: Path, strategy: DirectModelBindingStrategy
) -> None:
    """The read-order acknowledgement boundary ends prior-model admission."""

    async with _peer(tmp_path) as peer:
        await peer.negotiate(strategy)
        task = peer.start(peer.client.create_session_exact(peer.workspace, "target"))
        create = await peer.request("session/new")
        peer.session_created(create, "live")
        binding = await peer.binding_request(strategy)
        peer.acknowledge(binding, strategy)
        assert not task.done()
        peer.config("live", _options("default"))
        assert not peer.transport.is_open
        assert peer.close_events == ["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", _STRATEGIES)
async def test_target_model_after_success_response_is_valid_before_waiter_resumes(
    tmp_path: Path, strategy: DirectModelBindingStrategy
) -> None:
    async with _peer(tmp_path) as peer:
        await peer.negotiate(strategy)
        task = peer.start(peer.client.create_session_exact(peer.workspace, "target"))
        create = await peer.request("session/new")
        peer.session_created(create, "live")
        binding = await peer.binding_request(strategy)
        peer.acknowledge(binding, strategy)
        assert not task.done()
        peer.config("live", _options("target"))
        assert peer.transport.is_open
        assert (await task).model_id == "target"
        assert peer.close_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", _STRATEGIES)
async def test_overlapping_binding_cannot_replace_pending_model_expectation(
    tmp_path: Path, strategy: DirectModelBindingStrategy
) -> None:
    async with _peer(tmp_path) as peer:
        await peer.negotiate(strategy)
        task = peer.start(peer.client.create_session_exact(peer.workspace, "target"))
        create = await peer.request("session/new")
        peer.session_created(create, "live")
        binding = await peer.binding_request(strategy)
        requests_before_overlap = len(peer.process.stdin.written)
        with pytest.raises(RuntimeError, match="already in progress"):
            await asyncio.wait_for(peer.client.set_model("live", "third"), timeout=1)
        assert len(peer.process.stdin.written) == requests_before_overlap
        assert peer.transport.is_open
        peer.config("live", _options("default"))
        peer.acknowledge(binding, strategy)
        assert (await task).model_id == "target"
        assert peer.close_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", _STRATEGIES)
@pytest.mark.parametrize("second_target", ["target", "third"])
async def test_cancelled_selector_response_cannot_settle_a_later_binding(
    tmp_path: Path,
    strategy: DirectModelBindingStrategy,
    second_target: str,
) -> None:
    """Even identical model values cannot correlate two different RPCs."""

    async with _peer(tmp_path) as peer:
        await peer.negotiate(strategy)
        creation = peer.start(peer.client.create_session(peer.workspace))
        create = await peer.request("session/new")
        peer.session_created(create, "live")
        assert await creation == "live"

        stdin = _FirstBlockedDrain(peer.process.stdin.written)
        peer.process.stdin = stdin
        first_task = peer.start(peer.client.set_model("live", "target"))
        first_request = await peer.binding_request(strategy)
        await asyncio.wait_for(stdin.entered.wait(), timeout=1)
        first_future = peer.transport._pending[first_request["id"]]
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task
        stdin.release.set()

        second_task = peer.start(peer.client.set_model("live", second_target))
        second_request = await peer.request(first_request["method"])
        assert second_request["id"] != first_request["id"]
        assert not second_task.done()
        peer.acknowledge(first_request, strategy)
        assert not peer.transport.is_open
        assert peer.close_events == ["closed"]
        with pytest.raises(ConnectionError, match="model binding response correlation"):
            await second_task
        # Cancellation during drain leaves this future without its original
        # waiter; explicitly observe the transport's failure rather than leak it.
        with pytest.raises(ConnectionError, match="model binding response correlation"):
            await first_future
        assert peer.client._sessions["live"].model_id == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", _STRATEGIES)
@pytest.mark.parametrize(
    ("model_values", "consistent"),
    [
        (("default", "target"), False),
        (("target", "default"), False),
        (("default", "default"), True),
        (("target", "target"), True),
    ],
    ids=["prior-then-target", "target-then-prior", "same-prior", "same-target"],
)
async def test_binding_model_snapshot_requires_consistent_model_entries(
    tmp_path: Path,
    strategy: DirectModelBindingStrategy,
    model_values: tuple[str, str],
    consistent: bool,
) -> None:
    async with _peer(tmp_path) as peer:
        await peer.negotiate(strategy)
        task = peer.start(peer.client.create_session_exact(peer.workspace, "target"))
        create = await peer.request("session/new")
        peer.session_created(create, "live")
        binding = await peer.binding_request(strategy)
        peer.config("live", _options(model_values[0]) + _options(model_values[1]))
        if consistent:
            assert peer.transport.is_open
            peer.acknowledge(binding, strategy)
            assert (await task).model_id == "target"
            assert peer.close_events == []
        else:
            assert not peer.transport.is_open
            with pytest.raises(ConnectionError, match="config update malformed"):
                await task
            assert peer.close_events == ["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", _STRATEGIES)
@pytest.mark.parametrize(
    "invalid_options",
    [
        _options("third"),
        [],
        [{"id": "mode", "currentValue": "agent"}],
        [{"id": "model", "currentValue": None}],
        [{"id": "model", "currentValue": ""}],
        [42],
    ],
    ids=["third-model", "empty", "missing-model", "null-model", "empty-model", "malformed"],
)
async def test_binding_does_not_admit_unrelated_or_malformed_model_updates(
    tmp_path: Path,
    strategy: DirectModelBindingStrategy,
    invalid_options: list[Any],
) -> None:
    async with _peer(tmp_path) as peer:
        await peer.negotiate(strategy)
        task = peer.start(peer.client.create_session_exact(peer.workspace, "target"))
        create = await peer.request("session/new")
        peer.session_created(create, "live")
        await peer.binding_request(strategy)
        peer.config("live", invalid_options)
        with pytest.raises(ConnectionError):
            await task
        assert not peer.transport.is_open
        assert peer.close_events == ["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "acknowledgement",
    [
        {},
        {"configOptions": []},
        {"configOptions": [{"id": "model", "currentValue": None}]},
        {"configOptions": _options("default")},
    ],
    ids=["missing-options", "missing-model", "malformed-model", "wrong-model"],
)
async def test_invalid_standard_acknowledgement_never_adopts_requested_model(
    tmp_path: Path, acknowledgement: dict[str, Any]
) -> None:
    async with _peer(tmp_path) as peer:
        await peer.negotiate(DirectModelBindingStrategy.STANDARD_CONFIG)
        task = peer.start(peer.client.create_session_exact(peer.workspace, "target"))
        create = await peer.request("session/new")
        peer.session_created(create, "live")
        binding = await peer.binding_request(DirectModelBindingStrategy.STANDARD_CONFIG)
        peer.config("live", _options("target"))
        peer.result(binding, acknowledgement)
        with pytest.raises((ModelAcknowledgementError, ConnectionError)):
            await task
        session = peer.client._sessions.get("live")
        assert session is None or session.model_id != "target"


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", _STRATEGIES)
async def test_binding_error_preserves_prior_model_at_response_read_boundary(
    tmp_path: Path, strategy: DirectModelBindingStrategy
) -> None:
    """A target notification is not an acknowledgement when its RPC fails."""

    async with _peer(tmp_path) as peer:
        await peer.negotiate(strategy)
        task = peer.start(peer.client.create_session_exact(peer.workspace, "target"))
        create = await peer.request("session/new")
        peer.session_created(create, "live")
        binding = await peer.binding_request(strategy)
        peer.config("live", _options("target"))
        peer.error(binding, -32000)
        assert not task.done()
        peer.config("live", _options("default"))
        assert peer.transport.is_open
        with pytest.raises(ModelAcknowledgementError):
            await task
        assert peer.client._sessions["live"].model_id == "default"
        assert peer.close_events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("copilot_succeeds", [False, True])
async def test_method_not_found_fallback_keeps_prior_model_until_settlement(
    tmp_path: Path, copilot_succeeds: bool
) -> None:
    async with _peer(tmp_path) as peer:
        await peer.catalog()
        task = peer.start(
            peer.client.negotiate_direct_model_binding("catalog", "target")
        )
        standard = await peer.request("session/set_config_option")
        peer.error(standard, -32601)
        assert not task.done()
        peer.config("catalog", _options("default"))
        assert peer.transport.is_open
        copilot = await peer.request("session/set_model")
        peer.config("catalog", _options("default"))
        assert peer.transport.is_open
        peer.config("catalog", _options("target"))
        if copilot_succeeds:
            peer.result(copilot, {})
            assert await task is DirectModelBindingStrategy.COPILOT_SET_MODEL
            assert peer.client._sessions["catalog"].model_id == "target"
        else:
            peer.error(copilot, -32000)
            peer.config("catalog", _options("default"))
            assert peer.transport.is_open
            with pytest.raises(ModelAcknowledgementError):
                await task
            assert peer.client.direct_model_binding_strategy is None
            assert peer.client._sessions["catalog"].model_id == "default"
        assert peer.close_events == []
