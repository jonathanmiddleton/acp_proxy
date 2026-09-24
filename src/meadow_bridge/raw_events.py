"""Explicit, lossless diagnostic capture separate from payload-safe logs."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ParamSpec, TextIO, TypeVar
from uuid import uuid4

from .json_types import JsonObject, JsonValue

_Parameters = ParamSpec("_Parameters")
_Result = TypeVar("_Result")


class RawEventCaptureError(RuntimeError):
    """Requested diagnostic evidence could not be retained completely."""


class RawEventCapture:
    """Sequence at observation; serialize disk writes off the event loop.

    Each process appends a distinct capture_id, preserving previous attempts.
    Limits apply to queued bytes, never to successful on-disk evidence.
    """

    MAX_PENDING_BYTES = 32 * 1024 * 1024
    MAX_PENDING_RECORDS = 4096

    def __init__(self, path: str, on_failure: Callable[[], None]) -> None:
        self._path = Path(path)
        self._on_failure = on_failure
        self._capture_id = uuid4().hex
        self._sequence = 0
        self._queue: asyncio.Queue[tuple[str, int] | None] = asyncio.Queue()
        self._pending_bytes = 0
        self._pending_records = 0
        self._file: TextIO | None = None
        self._task: asyncio.Task[None] | None = None
        self._failed = False
        self._closed = False

    def _serialize(self, kind: str, fields: JsonObject) -> tuple[str, int]:
        record = {
            "version": 1,
            "capture_id": self._capture_id,
            "sequence": self._sequence,
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            **fields,
        }
        # Escape Unicode in the envelope without changing decoded content,
        # including lone surrogate escapes supplied by the upstream agent.
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        self._sequence += 1
        return line, len(line.encode("utf-8"))

    def _open(self) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("capture destination must be a regular file")
            if info.st_size:
                os.lseek(fd, -1, os.SEEK_END)
                if os.read(fd, 1) != b"\n":
                    raise OSError("capture destination has an incomplete final record")
            self._file = os.fdopen(fd, "a", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    async def _io(
        function: Callable[_Parameters, _Result],
        *args: _Parameters.args,
        **kwargs: _Parameters.kwargs,
    ) -> _Result:
        work = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # The worker still owns filesystem state until it returns.
            await work
            raise

    def _write(self, line: str) -> None:
        assert self._file is not None
        self._file.write(line)
        self._file.flush()

    def _fail(self) -> None:
        if not self._failed:
            self._failed = True
            self._on_failure()

    async def start(self) -> None:
        try:
            await self._io(self._open)
            line, _size = self._serialize("capture_start", {})
            await self._io(self._write, line)
        except BaseException as error:
            if self._file is not None:
                await self._io(self._file.close)
            if isinstance(error, OSError):
                self._fail()
                raise RawEventCaptureError(
                    "Raw native event capture could not start"
                ) from None
            raise
        self._task = asyncio.create_task(self._run())

    def record(self, kind: str, **fields: JsonValue) -> None:
        if self._failed or self._closed:
            raise RawEventCaptureError("Raw native event capture is unavailable")
        line, size = self._serialize(kind, fields)
        if (
            self._pending_bytes + size > self.MAX_PENDING_BYTES
            or self._pending_records >= self.MAX_PENDING_RECORDS
        ):
            self._fail()
            raise RawEventCaptureError("Raw native event capture queue limit exceeded")
        self._pending_bytes += size
        self._pending_records += 1
        self._queue.put_nowait((line, size))

    async def _run(self) -> None:
        try:
            while not self._failed:
                item = await self._queue.get()
                if item is None:
                    break
                line, size = item
                await self._io(self._write, line)
                self._pending_bytes -= size
                self._pending_records -= 1
        except OSError:
            self._fail()
        finally:
            while not self._queue.empty():
                self._queue.get_nowait()
            self._pending_bytes = 0
            self._pending_records = 0
            if self._file is not None:
                try:
                    await self._io(self._file.close)
                except OSError:
                    self._fail()

    async def close(self) -> None:
        if not self._closed:
            if self._task is not None and not self._failed:
                try:
                    self.record("capture_end")
                except RawEventCaptureError:
                    # _fail has already reported incomplete evidence to owner.
                    pass
            self._closed = True
            self._queue.put_nowait(None)
        if self._task is not None:
            # A cancelled to_thread call does not stop its filesystem write.
            # Always join the owner task before allowing teardown to return.
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                await self._task
                raise
        if self._failed:
            raise RawEventCaptureError("Raw native event capture is incomplete")
