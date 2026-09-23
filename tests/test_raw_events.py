"""Opt-in diagnostics retain wire evidence without changing dispatch order."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from meadow_bridge.raw_events import RawEventCapture, RawEventCaptureError
from meadow_bridge.transport import AcpError, AcpTransport
from tests.test_transport import FakeProcess


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_error", [False, True])
async def test_capture_preserves_envelopes_and_prompt_correlation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_error: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    capture_path = tmp_path / "events.jsonl"
    fake = FakeProcess()
    caplog.set_level("DEBUG")

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    transport = AcpTransport(raw_event_file=str(capture_path))
    observed: list[dict[str, Any]] = []
    transport.on_notification(observed.append)
    await transport.start("synthetic-acp")
    pending = asyncio.create_task(
        transport.send_request(
            "session/prompt",
            {
                "sessionId": "session-one",
                "prompt": [{"type": "text", "text": "PROMPT-NOT-CAPTURED"}],
            },
        )
    )
    while not fake.stdin.written:
        await asyncio.sleep(0)
    request_id = json.loads(fake.stdin.written[0])["id"]
    updates = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "session-one",
                "_meta": {"outer": ["untouched"]},
                "update": {
                    "sessionUpdate": kind,
                    "messageId": message_id,
                    "_meta": {"vendor/phase": phase},
                    "content": {
                        "type": "text",
                        "text": text,
                        "_meta": {"nested": True},
                    },
                },
            },
        }
        for kind, message_id, phase, text in [
            ("agent_message_chunk", "intro", "commentary", "Checking…\n"),
            ("agent_thought_chunk", "thought", "reasoning", "Separate thought"),
            ("agent_message_chunk", "answer", "final", '{"answer":"✓"}'),
        ]
    ]
    for update in updates:
        fake.stdout.feed(json.dumps(update))
    terminal: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if terminal_error:
        terminal["error"] = {"code": -32000, "message": "failure", "data": {"extra": 1}}
    else:
        terminal["result"] = {"stopReason": "end_turn", "_meta": {"terminal": True}}
    fake.stdout.feed(json.dumps(terminal))
    if terminal_error:
        with pytest.raises(AcpError):
            await pending
    else:
        assert await pending == terminal["result"]
    await transport.stop()

    records = [json.loads(line) for line in capture_path.read_text().splitlines()]
    assert [record["sequence"] for record in records] == list(range(len(records)))
    assert all(record["timestamp"] and record["capture_id"] for record in records)
    assert [record["kind"] for record in records] == [
        "capture_start",
        "prompt_request",
        "session_update",
        "session_update",
        "session_update",
        "prompt_response",
        "capture_end",
    ]
    assert [
        record["message"] for record in records if record["kind"] == "session_update"
    ] == updates
    assert observed == updates
    for record in (records[1], records[-2]):
        assert record["request_id"] == request_id
        assert record["session_id"] == "session-one"
    assert records[-2]["message"] == terminal
    assert "PROMPT-NOT-CAPTURED" not in capture_path.read_text()
    assert "Checking" not in caplog.text
    assert "Separate thought" not in caplog.text


@pytest.mark.asyncio
async def test_capture_failure_prevents_child_start(tmp_path: Path) -> None:
    transport = AcpTransport(raw_event_file=str(tmp_path))
    with pytest.raises(RuntimeError, match="Raw ACP event capture"):
        await transport.start("must-not-be-started")
    assert transport._process is None
    with pytest.raises(RawEventCaptureError, match="incomplete"):
        await transport.stop()


@pytest.mark.asyncio
async def test_capture_appends_distinct_lifetimes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "events.jsonl"

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    for _ in range(2):
        transport = AcpTransport(raw_event_file=str(path))
        await transport.start("synthetic-acp")
        await transport.stop()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["kind"] for r in records] == ["capture_start", "capture_end"] * 2
    assert records[0]["capture_id"] == records[1]["capture_id"]
    assert records[2]["capture_id"] == records[3]["capture_id"]
    assert records[0]["capture_id"] != records[2]["capture_id"]


@pytest.mark.asyncio
async def test_unterminated_capture_is_rejected_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    partial = b'{"sequence":0'
    path.write_bytes(partial)
    transport = AcpTransport(raw_event_file=str(path))
    with pytest.raises(RawEventCaptureError, match="could not start"):
        await transport.start("must-not-start")
    assert path.read_bytes() == partial


@pytest.mark.asyncio
async def test_start_cancellation_joins_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = threading.Event()
    release = threading.Event()
    original_open = RawEventCapture._open

    def delayed_open(self: RawEventCapture) -> None:
        original_open(self)
        opened.set()
        if not release.wait(timeout=5):
            raise OSError("test opener was not released")

    monkeypatch.setattr(RawEventCapture, "_open", delayed_open)
    capture = RawEventCapture(str(tmp_path / "cancelled.jsonl"), lambda: None)
    starting = asyncio.create_task(capture.start())
    try:
        assert await asyncio.to_thread(opened.wait, 1)
        starting.cancel()
        await asyncio.sleep(0)
        assert not starting.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert capture._file is not None and capture._file.closed
    await capture.close()


@pytest.mark.asyncio
async def test_slow_capture_does_not_delay_observers_and_shutdown_joins_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "slow.jsonl"
    fake = FakeProcess()
    release = threading.Event()
    writing = threading.Event()
    original_write = RawEventCapture._write

    def delayed_write(self: RawEventCapture, line: str) -> None:
        if '"kind":"session_update"' in line:
            writing.set()
            if not release.wait(timeout=5):
                raise OSError("test writer was not released")
        original_write(self, line)

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(RawEventCapture, "_write", delayed_write)
    transport = AcpTransport(raw_event_file=str(path))
    observed: list[str] = []
    transport.on_request_sent(lambda *_args: observed.append("sent"))
    transport.on_notification(lambda _message: observed.append("update"))
    transport.on_response_observed(lambda *_args: observed.append("response"))
    await transport.start("synthetic-acp")
    pending = asyncio.create_task(
        transport.send_request("session/prompt", {"sessionId": "s"})
    )
    while not fake.stdin.written:
        await asyncio.sleep(0)
    request_id = json.loads(fake.stdin.written[0])["id"]
    fake.stdout.feed(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "s",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "answer"},
                    },
                },
            }
        )
    )
    fake.stdout.feed(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}}
        )
    )
    try:
        await asyncio.wait_for(pending, timeout=1)
        assert observed == ["sent", "update", "response"]
        assert await asyncio.to_thread(writing.wait, 1)
        stopping = asyncio.create_task(transport.stop())
        await asyncio.sleep(0)
        assert not stopping.done()
    finally:
        release.set()
    await stopping
    assert json.loads(path.read_text().splitlines()[-1])["kind"] == "capture_end"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "capacity"])
async def test_capture_failure_revokes_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    fake = FakeProcess()

    async def spawn(*args: Any, **kwargs: Any) -> FakeProcess:
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    transport = AcpTransport(raw_event_file=str(tmp_path / "failed.jsonl"))
    closed = asyncio.Event()
    transport.on_close(closed.set)
    await transport.start("synthetic-acp")
    if failure == "capacity":
        monkeypatch.setattr(RawEventCapture, "MAX_PENDING_BYTES", 1)
    else:

        def broken_write(self: RawEventCapture, line: str) -> None:
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(RawEventCapture, "_write", broken_write)
    pending = asyncio.create_task(
        transport.send_request("session/prompt", {"sessionId": "s"})
    )
    with pytest.raises((ConnectionError, RawEventCaptureError)):
        await asyncio.wait_for(pending, timeout=1)
    await asyncio.wait_for(closed.wait(), timeout=1)
    assert not transport.is_open
    with pytest.raises(RawEventCaptureError, match="incomplete"):
        await transport.stop()
