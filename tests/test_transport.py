"""Tests for the NDJSON transport layer."""

from __future__ import annotations

import asyncio
import json

import pytest

from acp_proxy.direct_protocol import DirectLimits
from acp_proxy.transport import (
    MAX_ACP_STDOUT_LINE_BYTES,
    STDERR_DRAIN_CHUNK_BYTES,
    AcpError,
    AcpTransport,
)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


class FakeProcess:
    """Simulates an asyncio subprocess for transport testing."""

    def __init__(self) -> None:
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()
        self.stderr = FakeStdout()
        self._returncode: int | None = None

    def terminate(self) -> None:
        self._returncode = 0
        self.stdout.close()
        self.stderr.close()

    def kill(self) -> None:
        self._returncode = -9
        self.stdout.close()
        self.stderr.close()

    async def wait(self) -> int:
        return self._returncode or 0


class FakeStdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass


class FakeStdout:
    def __init__(self) -> None:
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False
        self._read_remainder = b""

    def feed(self, line: str) -> None:
        self._lines.put_nowait((line + "\n").encode())

    def feed_bytes(self, data: bytes) -> None:
        self._lines.put_nowait(data)

    def close(self) -> None:
        self._closed = True
        self._lines.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._lines.get()

    async def read(self, size: int = -1) -> bytes:
        data = self._read_remainder or await self._lines.get()
        self._read_remainder = b""
        if size >= 0 and len(data) > size:
            data, self._read_remainder = data[:size], data[size:]
        return data


def make_transport_with_fake(fake: FakeProcess) -> AcpTransport:
    """Create a transport wired to a FakeProcess (bypass subprocess spawn)."""
    transport = AcpTransport()
    transport._process = fake  # type: ignore[assignment]
    transport._reader_task = asyncio.create_task(transport._read_loop())
    transport._stderr_task = asyncio.create_task(transport._drain_stderr())
    return transport


@pytest.mark.asyncio
async def test_start_configures_stream_limit_above_negotiated_event_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADI-08/15: valid direct evidence fits the subprocess stream reader."""

    captured: dict[str, object] = {}
    fake = FakeProcess()

    async def fake_create(*args, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    transport = AcpTransport()
    await transport.start("/synthetic/acp")

    assert captured["limit"] == MAX_ACP_STDOUT_LINE_BYTES
    assert MAX_ACP_STDOUT_LINE_BYTES > DirectLimits().max_event_bytes
    await transport.stop()


@pytest.mark.asyncio
async def test_near_limit_event_crosses_real_stream_reader() -> None:
    """ADI-08/15: a near-4MB ACP event is read, not rejected at 64 KiB."""

    class StreamReaderProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stdout = asyncio.StreamReader(limit=MAX_ACP_STDOUT_LINE_BYTES)

        def terminate(self) -> None:
            self._returncode = 0
            self.stdout.feed_eof()
            self.stderr.close()

        def kill(self) -> None:
            self._returncode = -9
            self.stdout.feed_eof()
            self.stderr.close()

    fake = StreamReaderProcess()
    transport = make_transport_with_fake(fake)
    observed = asyncio.Event()
    payload_bytes = DirectLimits().max_event_bytes - 1_000
    transport.on_notification(lambda _message: observed.set())
    encoded = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "synthetic-session",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "x" * payload_bytes},
                    },
                },
            }
        ).encode()
        + b"\n"
    )
    assert 65_536 < len(encoded) < MAX_ACP_STDOUT_LINE_BYTES

    fake.stdout.feed_data(encoded)
    await asyncio.wait_for(observed.wait(), timeout=2.0)
    assert transport.is_open is True
    await transport.stop()


@pytest.mark.asyncio
async def test_send_request_receives_response():
    """Sending a request and receiving a matching response resolves the future."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    # Start a request in background
    task = asyncio.create_task(transport.send_request("test/method", {"key": "value"}))

    # Let the send happen
    await asyncio.sleep(0.05)

    # Verify the request was written
    assert len(fake.stdin.written) == 1
    sent = json.loads(fake.stdin.written[0].decode())
    assert sent["method"] == "test/method"
    assert sent["params"] == {"key": "value"}
    req_id = sent["id"]

    # Simulate response from server
    response = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"ok": True}})
    fake.stdout.feed(response)

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result == {"ok": True}

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_send_request_error_raises():
    """An error response raises AcpError."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    task = asyncio.create_task(transport.send_request("bad/method"))
    await asyncio.sleep(0.05)

    sent = json.loads(fake.stdin.written[0].decode())
    error_resp = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": sent["id"],
            "error": {"code": -32600, "message": "Invalid request"},
        }
    )
    fake.stdout.feed(error_resp)

    with pytest.raises(AcpError, match="Invalid request"):
        await asyncio.wait_for(task, timeout=2.0)

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_notification_dispatch():
    """Incoming notifications are dispatched to the registered handler."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    received: list[dict] = []
    transport.on_notification(lambda msg: received.append(msg))

    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "s1",
                    "update": {"sessionUpdate": "agent_message_chunk"},
                },
            }
        )
    )

    await asyncio.sleep(0.1)
    assert len(received) == 1
    assert received[0]["method"] == "session/update"

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_incoming_request_dispatch():
    """Incoming requests from the agent are dispatched and responded to."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    def handle_request(msg):
        if msg["method"] == "session/request_permission":
            return {"outcome": {"outcome": "cancelled"}}
        return None

    transport.on_request(handle_request)

    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "session/request_permission",
                "params": {"sessionId": "s1", "options": []},
            }
        )
    )

    await asyncio.sleep(0.15)

    # Should have written a response back
    assert len(fake.stdin.written) >= 1
    resp = json.loads(fake.stdin.written[-1].decode())
    assert resp["id"] == 99
    assert resp["result"]["outcome"]["outcome"] == "cancelled"

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_request_ids_increment():
    """Each request gets a unique incrementing ID."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    # Fire off two requests without resolving them
    t1 = asyncio.create_task(transport.send_request("m1"))
    t2 = asyncio.create_task(transport.send_request("m2"))
    await asyncio.sleep(0.05)

    ids = [json.loads(w.decode())["id"] for w in fake.stdin.written]
    assert ids[0] < ids[1]

    # Clean up
    t1.cancel()
    t2.cancel()
    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_transport_debug_logs_never_persist_wire_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADI-02/09/15: wire logs expose metadata, never prompt/callback bytes."""

    outbound_canary = "T122-OUTBOUND-STABLE-SCHEMA-SECRET"
    inbound_canary = "T122-INBOUND-CALLBACK-PATH-SECRET"
    error_canary = "T122-INBOUND-ERROR-SECRET"
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    transport.on_request(
        lambda _message: {"outcome": {"outcome": "cancelled"}}
    )
    caplog.set_level("DEBUG", logger="acp_proxy.transport")

    pending = asyncio.create_task(
        transport.send_request(
            "session/prompt",
            {
                "sessionId": "backend-session-secret",
                "prompt": [{"type": "text", "text": outbound_canary}],
            },
        )
    )
    await asyncio.sleep(0.01)
    request_id = json.loads(fake.stdin.written[0])["id"]
    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "fs/read_text_file",
                "params": {
                    "sessionId": "backend-session-secret",
                    "path": inbound_canary,
                },
            }
        )
    )
    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": error_canary},
            }
        )
    )

    with pytest.raises(AcpError):
        await asyncio.wait_for(pending, timeout=1.0)
    await asyncio.sleep(0.01)

    assert outbound_canary not in caplog.text
    assert inbound_canary not in caplog.text
    assert error_canary not in caplog.text
    assert "sessionId" not in caplog.text
    assert "session/prompt" in caplog.text
    assert "fs/read_text_file" in caplog.text

    fake.stdout.close()
    fake.stderr.close()
    await transport.stop()


@pytest.mark.asyncio
async def test_transport_debug_logs_never_persist_child_stderr(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADI-02/09/15: raw child stderr is drained but never persisted."""

    stderr_canary = "T122-CHILD-STDERR-CREDENTIAL-SECRET"
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    caplog.set_level("DEBUG", logger="acp_proxy.transport")

    large_stderr = (stderr_canary.encode() + b"-") * 4_096
    assert len(large_stderr) > 65_536
    fake.stderr.feed_bytes(large_stderr)
    fake.stderr.close()
    await asyncio.sleep(0.01)

    assert stderr_canary not in caplog.text
    assert "ACP child stderr chunk" in caplog.text
    assert caplog.text.count("ACP child stderr chunk") >= 2
    assert all(
        f"bytes={size}" in caplog.text
        for size in (STDERR_DRAIN_CHUNK_BYTES, len(large_stderr) % STDERR_DRAIN_CHUNK_BYTES)
    )

    fake.stdout.close()
    await transport.stop()


@pytest.mark.asyncio
async def test_stop_rejects_pending():
    """Stopping the transport rejects all pending request futures."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    task = asyncio.create_task(transport.send_request("slow/method"))
    await asyncio.sleep(0.05)

    await transport.stop()

    with pytest.raises(ConnectionError):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_unexpected_stdout_close_rejects_pending_and_signals_owner_once():
    """ADI-10/13: ACP child loss cannot leave pending work or readiness alive."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    closed = asyncio.Event()
    close_count = 0

    def observe_close() -> None:
        nonlocal close_count
        close_count += 1
        closed.set()

    transport.on_close(observe_close)
    pending = asyncio.create_task(transport.send_request("session/prompt"))
    await asyncio.sleep(0.01)

    fake.stdout.close()
    await asyncio.wait_for(closed.wait(), timeout=1.0)

    with pytest.raises(ConnectionError, match="stdout closed"):
        await asyncio.wait_for(pending, timeout=1.0)
    assert transport.is_open is False
    assert close_count == 1
    await transport.stop()
    assert close_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_line",
    [
        "not-json",
        json.dumps(["a JSON value, but not a JSON-RPC object"]),
    ],
)
async def test_malformed_stdout_fails_pending_and_signals_owner(
    bad_line: str,
) -> None:
    """ADI-10/13: malformed ACP output is continuity loss, not log-and-skip."""

    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    closed = asyncio.Event()
    transport.on_close(closed.set)
    pending = asyncio.create_task(transport.send_request("session/prompt"))
    await asyncio.sleep(0.01)

    fake.stdout.feed(bad_line)
    await asyncio.wait_for(closed.wait(), timeout=1.0)

    with pytest.raises(ConnectionError, match="protocol failure"):
        await asyncio.wait_for(pending, timeout=1.0)
    assert transport.is_open is False
    await transport.stop()


@pytest.mark.asyncio
async def test_dispatch_exception_fails_pending_and_signals_owner() -> None:
    """ADI-10/13: a dispatch bug cannot strand pending ACP requests."""

    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    closed = asyncio.Event()
    transport.on_close(closed.set)
    transport.on_notification(
        lambda _message: (_ for _ in ()).throw(RuntimeError("private detail"))
    )
    pending = asyncio.create_task(transport.send_request("session/prompt"))
    await asyncio.sleep(0.01)

    fake.stdout.feed(json.dumps({"jsonrpc": "2.0", "method": "session/update"}))
    await asyncio.wait_for(closed.wait(), timeout=1.0)

    with pytest.raises(ConnectionError, match="protocol failure") as raised:
        await asyncio.wait_for(pending, timeout=1.0)
    assert "private detail" not in str(raised.value)
    assert transport.is_open is False
    await transport.stop()


@pytest.mark.asyncio
async def test_request_observer_runs_before_callback_response() -> None:
    """ADI-08: callback evidence is observed in wire order before handling."""

    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    order: list[str] = []
    transport.on_request_observed(lambda _message: order.append("observed"))

    def handle_request(_message):
        order.append("handled")
        return {"outcome": {"outcome": "cancelled"}}

    transport.on_request(handle_request)
    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "session/request_permission",
                "params": {"sessionId": "s1", "options": []},
            }
        )
    )

    await asyncio.sleep(0.05)
    assert order == ["observed", "handled"]

    fake.stdout.close()
    fake.stderr.close()
    await transport.stop()


@pytest.mark.asyncio
async def test_prompt_response_observer_precedes_future_resolution() -> None:
    """ADI-08: the terminal marker is ordered before prompt completion."""

    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    order: list[str] = []
    transport.on_response_observed(
        lambda _message, method, _params: order.append(f"observed:{method}")
    )
    pending = asyncio.create_task(
        transport.send_request("session/prompt", {"sessionId": "s1"})
    )
    pending.add_done_callback(lambda _task: order.append("resolved"))
    await asyncio.sleep(0.01)
    request_id = json.loads(fake.stdin.written[0])["id"]

    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"stopReason": "end_turn"},
            }
        )
    )

    await asyncio.wait_for(pending, timeout=1.0)
    await asyncio.sleep(0)
    assert order == ["observed:session/prompt", "resolved"]
    await transport.stop()


@pytest.mark.asyncio
async def test_blocked_callback_response_is_visible_at_prompt_terminal() -> None:
    """ADI-08/09: prompt terminal cannot overtake an unsettled callback response."""

    class BlockingStdin(FakeStdin):
        def __init__(self) -> None:
            super().__init__()
            self.drain_started = asyncio.Event()
            self.release = asyncio.Event()

        async def drain(self) -> None:
            self.drain_started.set()
            await self.release.wait()

    fake = FakeProcess()
    fake.stdin = BlockingStdin()
    transport = make_transport_with_fake(fake)
    transport.on_request(
        lambda _message: {"outcome": {"outcome": "cancelled"}}
    )
    terminal_observation: list[bool] = []
    def observe_terminal(_message, _method, params) -> None:
        unsettled = transport.has_pending_incoming_requests(params["sessionId"])
        terminal_observation.append(unsettled)
        if unsettled:
            transport.fail_closed("callback settlement protocol failure")

    transport.on_response_observed(observe_terminal)
    pending = asyncio.get_running_loop().create_future()
    transport._pending[1] = pending
    transport._pending_requests[1] = (
        "session/prompt",
        {"sessionId": "s1"},
    )

    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 99,
                "method": "session/request_permission",
                "params": {"sessionId": "s1", "options": []},
            }
        )
    )
    await asyncio.wait_for(fake.stdin.drain_started.wait(), timeout=1.0)
    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"stopReason": "end_turn"},
            }
        )
    )

    await asyncio.sleep(0.05)
    assert terminal_observation == [True]
    fake.stdin.release.set()
    await transport.stop()
    with pytest.raises(ConnectionError, match="callback settlement"):
        await pending
    assert transport._incoming_request_tasks == {}


@pytest.mark.asyncio
async def test_non_json_line_revokes_transport_before_followup_response():
    """Malformed output cannot be hidden by a later valid response."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    # Send a non-JSON line followed by a valid response
    task = asyncio.create_task(transport.send_request("test/method"))
    await asyncio.sleep(0.05)

    sent = json.loads(fake.stdin.written[0].decode())
    req_id = sent["id"]

    # Garbage line invalidates continuity before the later response.
    fake.stdout.feed("this is not json {{{")
    # Valid response — should still be processed
    fake.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"recovered": True}})
    )

    with pytest.raises(ConnectionError, match="protocol failure"):
        await asyncio.wait_for(task, timeout=2.0)
    assert transport.is_open is False

    fake.stdout.close()
    fake.stderr.close()
    await transport.stop()


@pytest.mark.asyncio
async def test_unexpected_response_id_ignored():
    """A response with an unknown ID is logged and ignored."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    # Send a response for an ID that was never requested
    fake.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": 99999, "result": {"orphan": True}})
    )

    # Give the read loop time to process
    await asyncio.sleep(0.1)

    # Transport should still be functional
    task = asyncio.create_task(transport.send_request("test/method"))
    await asyncio.sleep(0.05)

    sent = json.loads(fake.stdin.written[0].decode())
    fake.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": sent["id"], "result": {"ok": True}})
    )

    result = await asyncio.wait_for(task, timeout=2.0)
    assert result == {"ok": True}

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_unknown_response_id_fails_strict_direct_correlation() -> None:
    """ADI-08/10: direct mode cannot assign an unknown response truthfully."""

    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    transport.set_strict_response_correlation(True)
    closed = asyncio.Event()
    transport.on_close(closed.set)

    fake.stdout.feed(
        json.dumps({"jsonrpc": "2.0", "id": 99999, "result": {}})
    )

    await asyncio.wait_for(closed.wait(), timeout=1.0)
    assert transport.is_open is False
    await transport.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "2.0", "id": 1.0, "result": {}},
        {"jsonrpc": "1.0", "id": 1, "result": {}},
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {},
            "error": {"code": -1, "message": "synthetic"},
        },
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "error": ["not-an-error-object"]},
    ],
)
async def test_malformed_response_cannot_collide_with_direct_request(
    response: dict[str, object],
) -> None:
    """ADI-08/10: invalid response envelopes never settle pending work."""

    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    transport.set_strict_response_correlation(True)
    closed = asyncio.Event()
    transport.on_close(closed.set)
    pending = asyncio.create_task(transport.send_request("session/prompt"))
    await asyncio.sleep(0.01)

    fake.stdout.feed(json.dumps(response))
    await asyncio.wait_for(closed.wait(), timeout=1.0)

    with pytest.raises(ConnectionError, match="validation failure"):
        await asyncio.wait_for(pending, timeout=1.0)
    assert transport.is_open is False
    await transport.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "1.0", "method": "session/update", "params": {}},
        {"method": "session/update", "params": {}},
        {"jsonrpc": "2.0", "method": "", "params": {}},
        {"jsonrpc": "2.0", "method": True, "params": {}},
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {},
            "result": {},
        },
        {"jsonrpc": "2.0", "method": "session/update", "params": []},
        {"jsonrpc": "2.0", "id": True, "method": "fs/read_text_file"},
        {"jsonrpc": "2.0", "id": 1.0, "method": "fs/read_text_file"},
        {"jsonrpc": "2.0", "id": None, "method": "fs/read_text_file"},
    ],
)
async def test_malformed_direct_request_or_notification_fails_closed(
    message: dict[str, object],
) -> None:
    """ADI-02/08: invalid child envelopes cannot become evidence or callbacks."""

    fake = FakeProcess()
    transport = make_transport_with_fake(fake)
    transport.set_strict_response_correlation(True)
    closed = asyncio.Event()
    observed: list[dict] = []
    transport.on_close(closed.set)
    transport.on_notification(observed.append)
    transport.on_request(lambda request: observed.append(request))

    fake.stdout.feed(json.dumps(message))
    await asyncio.wait_for(closed.wait(), timeout=1.0)

    assert observed == []
    assert transport.is_open is False
    await transport.stop()


@pytest.mark.asyncio
async def test_handler_exception_returns_error_response():
    """If a request handler raises, an error response is sent back."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    def exploding_handler(msg):
        raise ValueError("handler blew up")

    transport.on_request(exploding_handler)

    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "some/request",
                "params": {},
            }
        )
    )

    await asyncio.sleep(0.15)

    # Should have sent an error response back
    assert len(fake.stdin.written) >= 1
    resp = json.loads(fake.stdin.written[-1].decode())
    assert resp["id"] == 42
    assert "error" in resp
    assert resp["error"]["code"] == -32603
    assert resp["error"]["message"] == "Internal error"

    fake.stdout.close()
    fake.stderr.close()


@pytest.mark.asyncio
async def test_send_notification_no_id():
    """send_notification sends a message without an 'id' field."""
    fake = FakeProcess()
    transport = make_transport_with_fake(fake)

    await transport.send_notification("test/notify", {"data": "value"})

    assert len(fake.stdin.written) == 1
    sent = json.loads(fake.stdin.written[0].decode())
    assert "id" not in sent
    assert sent["method"] == "test/notify"
    assert sent["params"] == {"data": "value"}

    fake.stdout.close()
    fake.stderr.close()
