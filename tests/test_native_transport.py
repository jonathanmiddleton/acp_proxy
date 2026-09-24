"""Native framing must preserve payloads and reject ambiguous boundaries."""
import asyncio
from collections.abc import Awaitable, Mapping
import logging
from pathlib import Path
import sys
from typing import NoReturn

import pytest

from meadow_bridge.json_types import JsonValue, json_object, parse_json
from meadow_bridge.native_transport import (
    NativeNotification,
    NativeProtocolError,
    NativeRequest,
    NativeTransport,
    NativeTransportError,
    RpcResponse,
    read_frame,
)
from meadow_bridge.raw_events import RawEventCapture, RawEventCaptureError


_SERVER = r'''
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def receive():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            raise EOFError
        if line == b"\r\n":
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))

def send(*messages):
    for message in messages:
        body = json.dumps(message).encode("utf-8")
        sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()

def notice(method, params=None):
    return {"jsonrpc": "2.0", "method": method, "params": params}

mode = sys.argv[1]
Path("spawned").write_text(str(os.getpid()))
send(notice("ready", {"native_mode": os.environ.get("GITHUB_COPILOT_ACP_USE_CLI")}))
if mode == "blocked":
    time.sleep(30)
    sys.exit(0)
request = receive()
Path("received").write_text(request["method"])
if mode == "ordered":
    send(notice("before"),
         {"jsonrpc": "2.0", "id": "callback-1", "method": "effect", "params": {}},
         {"jsonrpc": "2.0", "id": request["id"], "result": "response"},
         notice("after"))
    send(notice("callback-response", receive()))
elif mode == "callback-hold":
    send({"jsonrpc": "2.0", "id": "callback-1", "method": "effect", "params": {}})
elif mode == "eof":
    sys.exit(7)
elif mode == "descendant":
    subprocess.Popen([sys.executable, "-u", "-c", "\n".join([
        "from pathlib import Path",
        "import time",
        "Path('descendant-ready').write_text('ready')",
        "time.sleep(1)",
        "Path('descendant-survived').write_text('survived')",
    ])])
    deadline = time.monotonic() + 5
    while not Path("descendant-ready").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Descendant did not start")
        time.sleep(.005)
    sys.exit(0)
elif mode == "invalid":
    receive()
    response = json.loads(sys.argv[2])
    response.setdefault("id", request["id"])
    send(response)
elif mode == "logs":
    print(request["params"], file=sys.stderr, flush=True)
    send({"jsonrpc": "2.0", "id": request["id"], "result": request["params"]})
elif mode != "park":
    raise ValueError(mode)
try:
    while True:
        receive()
except EOFError:
    pass
'''


def _transport(tmp_path: Path, mode: str, *arguments: str,
               capture: Path | None = None, write_timeout: float = 30) -> NativeTransport:
    script = tmp_path / "native_fixture.py"
    script.write_text(_SERVER)
    return NativeTransport(
        sys.executable, cwd=tmp_path,
        arguments=("-u", str(script), mode, *arguments),
        raw_event_file=str(capture) if capture else None,
        write_timeout=write_timeout,
    )


async def _start(transport: NativeTransport) -> None:
    ready = asyncio.Event()

    def notification(value: NativeNotification) -> None:
        assert value.method == "ready"
        assert value.params == {"native_mode": "0"}
        ready.set()

    transport.on_notification(notification)
    await transport.start()
    await asyncio.wait_for(ready.wait(), 5)


@pytest.mark.asyncio
async def test_lsp_content_length_counts_utf8_bytes_across_chunks() -> None:
    reader = asyncio.StreamReader()
    body = '{"jsonrpc":"2.0","method":"notice","params":{"text":"Ω"}}'.encode()
    frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    for byte in frame:
        reader.feed_data(bytes([byte]))
    reader.feed_eof()
    assert await read_frame(reader) == {
        "jsonrpc": "2.0", "method": "notice", "params": {"text": "Ω"}
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [
    b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
    b"Content-Length: -1\r\n\r\n",
    b"Content-Length: 999999999\r\n\r\n",
])
async def test_ambiguous_or_unbounded_frames_fail_before_payload_read(header: bytes) -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(header)
    reader.feed_eof()
    with pytest.raises(NativeProtocolError):
        await read_frame(reader)


@pytest.mark.asyncio
async def test_reservation_and_observer_preserve_reader_order(tmp_path: Path) -> None:
    transport = _transport(tmp_path, "ordered")
    order: list[str] = []
    after = asyncio.Event()
    callback_received = asyncio.Event()
    release_callback = asyncio.Event()

    def notification(value: NativeNotification) -> None:
        if value.method == "callback-response":
            assert value.params == {
                "jsonrpc": "2.0", "id": "callback-1", "result": "effect-result"
            }
            callback_received.set()
        else:
            order.append(value.method)
            if value.method == "after":
                after.set()

    async def callback_work() -> JsonValue:
        await release_callback.wait()
        return "effect-result"

    def callback(request: NativeRequest) -> Awaitable[JsonValue]:
        assert request == NativeRequest("callback-1", "effect", {})
        order.append("callback-admitted")
        return callback_work()

    def observe(response: RpcResponse) -> None:
        assert response == RpcResponse("response", False)
        order.append("response-observed")

    try:
        await _start(transport)
        transport.on_notification(notification)
        transport.on_request(callback)
        request = transport.reserve("inspect", observer=observe)
        assert not request.sent
        assert not request.future.done()
        assert not (tmp_path / "received").exists()
        await transport.dispatch(request)
        assert await asyncio.wait_for(transport.wait(request), 5) == "response"
        await asyncio.wait_for(after.wait(), 5)
        assert order == ["before", "callback-admitted", "response-observed", "after"]
        with pytest.raises(NativeProtocolError, match="repeated"):
            await transport.dispatch(request)

        settlement = asyncio.create_task(transport.settle_callbacks())
        await asyncio.sleep(0)
        assert not settlement.done()
        release_callback.set()
        await asyncio.wait_for(settlement, 5)
        await asyncio.wait_for(callback_received.wait(), 5)
        assert transport.is_open
    finally:
        release_callback.set()
        await transport.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    '{"jsonrpc":"2.0","id":999,"result":"unknown"}',
    '{"jsonrpc":"2.0","id":true,"result":"invalid-id"}',
    '{"jsonrpc":"1.0","result":"wrong-version"}',
    '{"jsonrpc":"2.0","result":null,"error":{"code":1,"message":"both"}}',
    '{"jsonrpc":"2.0","error":{"code":"invalid","message":"bad-error"}}',
    '{"jsonrpc":"2.0","result":null,"unrecognized":true}',
])
async def test_invalid_response_revokes_all_pending_requests(
    tmp_path: Path, response: str,
) -> None:
    transport = _transport(tmp_path, "invalid", response)
    closures: list[str] = []
    transport.on_transport_closed(closures.append)
    try:
        await _start(transport)
        first = transport.reserve("first")
        second = transport.reserve("second")
        await transport.dispatch(first)
        await transport.dispatch(second)
        results = await asyncio.wait_for(
            asyncio.gather(transport.wait(first), transport.wait(second), return_exceptions=True),
            5,
        )
        assert all(isinstance(result, NativeTransportError) for result in results)
        assert transport.failure is not None
        assert not transport.is_open
        assert len(closures) == 1
        with pytest.raises(NativeTransportError):
            transport.reserve("after-loss")
        with pytest.raises(NativeTransportError):
            await transport.notify("after-loss")
    finally:
        await transport.stop()


@pytest.mark.asyncio
async def test_eof_rejects_pending_and_stop_reaps_the_child(tmp_path: Path) -> None:
    transport = _transport(tmp_path, "eof")
    closures: list[str] = []
    transport.on_transport_closed(closures.append)
    try:
        await _start(transport)
        with pytest.raises(NativeTransportError) as caught:
            await transport.request("exit-now", timeout=5)
        assert str(caught.value) in {
            "Invalid or incomplete native LSP frame",
            "Native server root exited before its stdout settled",
        }
        assert len(closures) == 1
        assert not transport.is_open
        process = transport._process
        assert process is not None
        # Pipe EOF may precede root exit; cleanup is allowed to terminate a
        # still-running root, so observe its natural status before cleanup.
        async with asyncio.timeout(5):
            while (exit_code := await transport._io(process.poll)) is None:
                await asyncio.sleep(.005)
        assert exit_code == 7
    finally:
        await transport.stop()
    assert transport._exit_code == 7
    await transport.stop()
    assert len(closures) == 1


@pytest.mark.asyncio
async def test_stop_waits_for_cancelled_callback_cleanup_and_rejects_pending(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path, "callback-hold")
    callback_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def callback(request: NativeRequest) -> JsonValue:
        assert request.method == "effect"
        callback_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_cleanup.wait()
            cleanup_finished.set()
        return None

    try:
        await _start(transport)
        transport.on_request(callback)
        request = transport.reserve("start-callback")
        await transport.dispatch(request)
        await asyncio.wait_for(callback_started.wait(), 5)
        stop = asyncio.create_task(transport.stop())
        await asyncio.wait_for(cleanup_started.wait(), 5)
        assert not stop.done()
        assert not transport.is_open
        with pytest.raises(NativeTransportError, match="stopped"):
            await transport.wait(request)
        stop.cancel()
        await asyncio.sleep(0)
        stop.cancel()
        repeated_stop = asyncio.create_task(transport.stop())
        await asyncio.sleep(0)
        assert not stop.done()
        assert not repeated_stop.done()
        allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stop, 5)
        await asyncio.wait_for(repeated_stop, 5)
        assert cleanup_finished.is_set()
        assert transport._exit_code is not None
    finally:
        allow_cleanup.set()
        await transport.stop()


@pytest.mark.asyncio
async def test_capture_startup_failure_prevents_process_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _transport(tmp_path, "park", capture=tmp_path)
    spawned = False

    def forbidden_spawn(argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]) -> NoReturn:
        nonlocal spawned
        spawned = True
        raise AssertionError("Process must not start without requested capture")

    monkeypatch.setattr("meadow_bridge.native_transport._spawn_process", forbidden_spawn)
    with pytest.raises(RawEventCaptureError, match="could not start"):
        await transport.start()
    assert not spawned
    assert not transport.is_open
    assert transport.failure is not None
    with pytest.raises(RawEventCaptureError, match="incomplete"):
        await transport.stop()


@pytest.mark.asyncio
async def test_capture_write_failure_revokes_live_pending_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "events.jsonl"
    transport = _transport(tmp_path, "park", capture=capture)
    failed = asyncio.Event()
    transport.on_transport_closed(lambda reason: failed.set())
    try:
        await _start(transport)
        original_write = RawEventCapture._write

        def fail_outbound_write(owner: RawEventCapture, line: str) -> None:
            record = json_object(parse_json(line))
            if record["kind"] == "native.outbound":
                raise OSError("test-owned capture disk failure")
            original_write(owner, line)

        monkeypatch.setattr(RawEventCapture, "_write", fail_outbound_write)
        pending = transport.reserve("unsettled")
        await transport.dispatch(pending)
        await asyncio.wait_for(failed.wait(), 5)
        with pytest.raises(NativeTransportError, match="capture failed"):
            await transport.wait(pending)
        assert not transport.is_open
        with pytest.raises(NativeTransportError, match="capture failed"):
            transport.reserve("after-capture-loss")
    finally:
        with pytest.raises(RawEventCaptureError, match="incomplete"):
            await transport.stop()
    assert transport._exit_code is not None


@pytest.mark.asyncio
async def test_payload_and_stderr_stay_out_of_ordinary_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    capture = tmp_path / "events.jsonl"
    transport = _transport(tmp_path, "logs", capture=capture)
    payload = "TEST-OWNED-PRIVATE-PAYLOAD-Ω"
    stderr_received = asyncio.Event()

    class StderrObserved(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage().startswith("Native stderr received"):
                stderr_received.set()

    handler = StderrObserved()
    logger = logging.getLogger("meadow_bridge.native_transport")
    logger.addHandler(handler)
    caplog.set_level(logging.DEBUG, logger="meadow_bridge.native_transport")
    try:
        await _start(transport)
        assert await transport.request("echo", payload, timeout=5) == payload
        await asyncio.wait_for(stderr_received.wait(), 5)
    finally:
        try:
            await transport.stop()
        finally:
            logger.removeHandler(handler)
    records = [json_object(parse_json(line)) for line in capture.read_text().splitlines()]
    inbound = [record for record in records if record["kind"] == "native.inbound"]
    assert any(json_object(record["message"]).get("result") == payload for record in inbound)
    assert payload not in caplog.text
    assert "Native stderr received" in caplog.text


@pytest.mark.asyncio
async def test_stop_kills_descendant_after_server_root_exit(tmp_path: Path) -> None:
    transport = _transport(tmp_path, "descendant")
    try:
        await _start(transport)
        with pytest.raises(NativeTransportError, match="root exited"):
            await transport.request("leave-descendant", timeout=5)
        assert transport._exit_code == 0
        assert (tmp_path / "descendant-ready").exists()
        assert not (tmp_path / "descendant-survived").exists()
    finally:
        await transport.stop()
    await asyncio.sleep(1.1)
    assert not (tmp_path / "descendant-survived").exists()
    assert transport._exit_code == 0
    await transport.stop()


@pytest.mark.asyncio
async def test_blocked_dispatch_deadline_revokes_pending_and_stop_settles(tmp_path: Path) -> None:
    transport = _transport(tmp_path, "blocked", write_timeout=.05)
    closures: list[str] = []
    transport.on_transport_closed(closures.append)
    try:
        await _start(transport)
        request = transport.reserve("blocked-write", "x" * (4 * 1024 * 1024))
        with pytest.raises(NativeTransportError, match="did not drain"):
            await asyncio.wait_for(transport.dispatch(request), 5)
        with pytest.raises(NativeTransportError, match="did not drain"):
            await transport.wait(request)
        assert not transport.is_open
        assert len(closures) == 1
        assert not (tmp_path / "received").exists()
    finally:
        await transport.stop()
    assert transport._exit_code is not None
