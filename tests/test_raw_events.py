"""Lossless capture owns bounded, ordered file writes and explicit failure."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from meadow_bridge.json_types import JsonObject, json_object, parse_json
from meadow_bridge.raw_events import RawEventCapture, RawEventCaptureError


def _records(path: Path) -> list[JsonObject]:
    return [json_object(parse_json(line)) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["Checking…\n✓", "x" * 65_536, "\ud800"],
    ids=["unicode", "large-payload", "unpaired-surrogate"],
)
async def test_capture_preserves_complete_ordered_records(
    tmp_path: Path, text: str,
) -> None:
    path = tmp_path / "events.jsonl"
    failures: list[None] = []
    capture = RawEventCapture(str(path), lambda: failures.append(None))
    envelope: JsonObject = {
        "jsonrpc": "2.0",
        "method": "$/progress",
        "params": {
            "token": "turn-one",
            "value": {"reply": text, "metadata": {"nested": [True, None, 3]}},
        },
    }
    await capture.start()
    capture.record("native_notification", message=envelope)
    capture.record("native_response", request_id=7, message={"result": {}})
    await capture.close()

    records = _records(path)
    assert [record["sequence"] for record in records] == [0, 1, 2, 3]
    assert [record["kind"] for record in records] == [
        "capture_start", "native_notification", "native_response", "capture_end",
    ]
    assert all(record["timestamp"] and record["capture_id"] for record in records)
    assert records[1]["message"] == envelope
    assert records[2]["request_id"] == 7
    assert records[2]["message"] == {"result": {}}
    assert failures == []


@pytest.mark.asyncio
async def test_capture_start_failure_reports_owner_once(tmp_path: Path) -> None:
    failures: list[None] = []
    capture = RawEventCapture(str(tmp_path), lambda: failures.append(None))
    with pytest.raises(RawEventCaptureError, match="could not start"):
        await capture.start()
    assert failures == [None]
    with pytest.raises(RawEventCaptureError, match="incomplete"):
        await capture.close()
    assert failures == [None]


@pytest.mark.asyncio
async def test_capture_appends_distinct_lifetimes(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    failures: list[None] = []
    for _ in range(2):
        capture = RawEventCapture(str(path), lambda: failures.append(None))
        await capture.start()
        await capture.close()

    records = _records(path)
    assert [record["kind"] for record in records] == ["capture_start", "capture_end"] * 2
    assert [record["sequence"] for record in records] == [0, 1, 0, 1]
    assert records[0]["capture_id"] == records[1]["capture_id"]
    assert records[2]["capture_id"] == records[3]["capture_id"]
    assert records[0]["capture_id"] != records[2]["capture_id"]
    assert failures == []


@pytest.mark.asyncio
async def test_unterminated_capture_is_rejected_without_modifying_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    partial = b'{"sequence":0'
    path.write_bytes(partial)
    failures: list[None] = []
    capture = RawEventCapture(str(path), lambda: failures.append(None))
    with pytest.raises(RawEventCaptureError, match="could not start"):
        await capture.start()
    assert path.read_bytes() == partial
    assert failures == [None]
    with pytest.raises(RawEventCaptureError, match="incomplete"):
        await capture.close()


@pytest.mark.asyncio
async def test_start_cancellation_joins_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    failures: list[None] = []
    capture = RawEventCapture(str(tmp_path / "cancelled.jsonl"), lambda: failures.append(None))
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
    assert failures == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_slow_writer_does_not_block_recording_and_close_joins_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_close: bool,
) -> None:
    path = tmp_path / "slow.jsonl"
    release = threading.Event()
    writing = threading.Event()
    original_write = RawEventCapture._write

    def delayed_write(self: RawEventCapture, line: str) -> None:
        if '"kind":"native_notification"' in line:
            writing.set()
            if not release.wait(timeout=5):
                raise OSError("test writer was not released")
        original_write(self, line)

    monkeypatch.setattr(RawEventCapture, "_write", delayed_write)
    failures: list[None] = []
    capture = RawEventCapture(str(path), lambda: failures.append(None))
    await capture.start()
    capture.record("native_notification", message={"text": "first"})
    closing: asyncio.Task[None] | None = None
    cancelled = False
    try:
        assert await asyncio.to_thread(writing.wait, 1)
        capture.record("native_response", message={"text": "second"})
        closing = asyncio.create_task(capture.close())
        await asyncio.sleep(0)
        assert not closing.done()
        if cancel_close:
            closing.cancel()
            cancelled = True
            await asyncio.sleep(0)
            assert not closing.done()
    finally:
        release.set()
        if closing is None:
            await capture.close()
        elif cancelled:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing

    records = _records(path)
    assert [record["kind"] for record in records] == [
        "capture_start", "native_notification", "native_response", "capture_end",
    ]
    assert records[1]["message"] == {"text": "first"}
    assert records[2]["message"] == {"text": "second"}
    assert capture._file is not None and capture._file.closed
    assert failures == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "byte_capacity", "record_capacity"])
async def test_capture_failure_reports_owner_and_remains_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    failures: list[None] = []
    failed = asyncio.Event()

    def report_failure() -> None:
        failures.append(None)
        failed.set()

    path = tmp_path / "failed.jsonl"
    capture = RawEventCapture(str(path), report_failure)
    await capture.start()
    if failure == "write":

        def broken_write(self: RawEventCapture, line: str) -> None:
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(RawEventCapture, "_write", broken_write)
        capture.record("native_notification", message={"text": "unwritten"})
    else:
        limit = "MAX_PENDING_BYTES" if failure == "byte_capacity" else "MAX_PENDING_RECORDS"
        monkeypatch.setattr(RawEventCapture, limit, 0)
        with pytest.raises(RawEventCaptureError, match="queue limit exceeded"):
            capture.record("native_notification", message={"text": "unwritten"})

    await asyncio.wait_for(failed.wait(), timeout=1)
    with pytest.raises(RawEventCaptureError, match="unavailable"):
        capture.record("native_notification", message={"text": "later"})
    with pytest.raises(RawEventCaptureError, match="incomplete"):
        await capture.close()
    assert failures == [None]
    assert capture._file is not None and capture._file.closed
    assert [record["kind"] for record in _records(path)] == ["capture_start"]
