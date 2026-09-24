"""Owned LSP stdio framing with ordered JSON-RPC and callback admission."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sys
from typing import ParamSpec, Protocol, TypeVar

from .json_types import JsonObject, JsonValue, json_object, json_text, parse_json
from .raw_events import RawEventCapture

logger = logging.getLogger(__name__)
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_HEADER_BYTES = 8192
_P = ParamSpec("_P")
_T = TypeVar("_T")


class _Reader(Protocol):
    async def readexactly(self, count: int, /) -> bytes: ...


class _OwnedProcess(Protocol):
    def read_stdout(self, max_bytes: int) -> bytes | None: ...
    def read_stderr(self, max_bytes: int) -> bytes | None: ...
    def write_stdin(self, data: bytes) -> int | None: ...
    def close_stdin(self) -> None: ...
    def poll(self) -> int | None: ...
    def stop(self) -> None: ...
    def tree_stopped(self) -> bool: ...
    def close(self) -> None: ...


def _spawn_process(argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]) -> _OwnedProcess:
    if sys.platform == "win32":
        from ._owned_command_windows import WindowsCommand
        return WindowsCommand(argv, cwd, env, pipe_input=True)
    if sys.platform == "linux":
        from ._owned_command_linux import LinuxCommand
        return LinuxCommand(argv, cwd, env, pipe_input=True)
    from ._owned_command_posix import PosixCommand
    return PosixCommand(argv, cwd, env, pipe_input=True)


class _PipeReader:
    def __init__(self, transport: NativeTransport) -> None:
        self._transport = transport
        self._buffer = bytearray()

    async def readexactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = await self._transport.read_stdout()
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(self._buffer), count)
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result


class NativeTransportError(RuntimeError):
    """The owned native transport cannot maintain request continuity."""


class NativeProtocolError(NativeTransportError):
    """A native envelope or ordering violates the admitted protocol."""


class NativeRpcError(RuntimeError):
    """A correlated JSON-RPC error, distinct from transport loss."""

    def __init__(self, error: JsonObject) -> None:
        super().__init__(json_text(error))
        self.error = error


async def read_frame(reader: _Reader) -> JsonObject:
    """Read one bounded UTF-8 LSP frame; never infer NDJSON or another codec."""
    header = bytearray()
    try:
        while not header.endswith(b"\r\n\r\n"):
            if len(header) >= MAX_HEADER_BYTES:
                raise NativeProtocolError("Native LSP header exceeds its limit")
            header.extend(await reader.readexactly(1))
        fields: dict[str, str] = {}
        for line in bytes(header[:-4]).decode("ascii").split("\r\n"):
            key, separator, value = line.partition(":")
            key = key.lower()
            if not separator or key in fields:
                raise NativeProtocolError("Malformed or duplicate native LSP header")
            fields[key] = value.strip()
        length = fields.get("content-length", "")
        if not length.isascii() or not length.isdecimal() or not 0 < int(length) <= MAX_FRAME_BYTES:
            raise NativeProtocolError("Invalid native LSP Content-Length")
        body = await reader.readexactly(int(length))
        return json_object(parse_json(body), "native envelope")
    except (UnicodeError, ValueError, asyncio.IncompleteReadError) as error:
        raise NativeProtocolError("Invalid or incomplete native LSP frame") from error


@dataclass(frozen=True)
class NativeRequest:
    """A validated server request admitted in reader order."""
    id: int | str
    method: str
    params: JsonValue


@dataclass(frozen=True)
class NativeNotification:
    """A validated native notification admitted in reader order."""
    method: str
    params: JsonValue


@dataclass(frozen=True)
class RpcResponse:
    """A correlated response; error and success remain distinct."""
    value: JsonValue
    is_error: bool


@dataclass
class PendingRequest:
    """Transport-owned RPC reservation, allocated before any write."""
    id: int
    method: str
    params: JsonValue
    future: asyncio.Future[JsonValue]
    observer: Callable[[RpcResponse], None] | None
    sent: bool = False


class NativeTransport:
    """One process generation; unexpected input permanently revokes continuity."""

    def __init__(self, binary_path: str, *, cwd: Path,
                 arguments: tuple[str, ...] = ("--stdio",),
                 raw_event_file: str | None = None,
                 max_callbacks: int = 32, write_timeout: float = 30) -> None:
        self._argv = (binary_path, *arguments)
        self._cwd = cwd
        self._process: _OwnedProcess | None = None
        self._exit_code: int | None = None
        self._cleanup: asyncio.Task[None] | None = None
        self._io_lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._pending: dict[int, PendingRequest] = {}
        self._callbacks: set[asyncio.Task[None]] = set()
        self._callback_ids: set[int | str] = set()
        self._next_id = 0
        self._max_callbacks = max_callbacks
        self._write_timeout = write_timeout
        self._failure: NativeTransportError | None = None
        self._stopping = False
        self._expected_exit = False
        self._write_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._notification: Callable[[NativeNotification], None] | None = None
        self._request: Callable[[NativeRequest], Awaitable[JsonValue]] | None = None
        self._closed: list[Callable[[str], None]] = []
        self._capture = RawEventCapture(raw_event_file, self._capture_failed) if raw_event_file else None

    @property
    def is_open(self) -> bool:
        """Readiness requires a running, non-revoked owned child."""
        return self._process is not None and self._exit_code is None and self._failure is None and not self._stopping

    @property
    def failure(self) -> NativeTransportError | None:
        """The first continuity failure, if one occurred."""
        return self._failure

    def on_notification(self, handler: Callable[[NativeNotification], None]) -> None:
        """Register synchronous read-order notification admission."""
        self._notification = handler

    def on_request(self, handler: Callable[[NativeRequest], Awaitable[JsonValue]]) -> None:
        """Register synchronous admission returning the owned callback work."""
        self._request = handler

    def on_transport_closed(self, handler: Callable[[str], None]) -> None:
        """Observe unexpected closure without awaiting owner shutdown recursively."""
        self._closed.append(handler)

    async def start(self, env: Mapping[str, str] | None = None) -> None:
        """Start capture before the child, always selecting native bundled mode."""
        if self._process is not None or self._failure is not None or self._stopping:
            raise NativeTransportError("A transport generation cannot be restarted")
        if self._capture:
            await self._capture.start()
        child_env = dict(os.environ if env is None else env)
        child_env["GITHUB_COPILOT_ACP_USE_CLI"] = "0"
        child = asyncio.create_task(asyncio.to_thread(_spawn_process, self._argv, self._cwd, child_env))
        try:
            self._process = await asyncio.shield(child)
        except asyncio.CancelledError:
            while not child.done():
                try:
                    await asyncio.shield(child)
                except asyncio.CancelledError:
                    continue  # The spawned owner must be recovered and joined.
            self._process = child.result()
            await self.stop()
            raise
        except Exception:
            await self.stop()
            raise
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._drain_stderr())

    def reserve(self, method: str, params: JsonValue = None,
                observer: Callable[[RpcResponse], None] | None = None) -> PendingRequest:
        """Reserve response identity before the caller dispatches its request."""
        if not self.is_open:
            raise self._failure or NativeTransportError("Native transport is not open")
        self._next_id += 1
        request = PendingRequest(self._next_id, method, params, asyncio.get_running_loop().create_future(), observer)
        self._pending[request.id] = request
        return request

    async def dispatch(self, request: PendingRequest) -> None:
        """Dispatch exactly one previously reserved operation."""
        if self._pending.get(request.id) is not request or request.sent:
            raise NativeProtocolError("Unknown or repeated native request dispatch")
        request.sent = True
        await self._send({"jsonrpc": "2.0", "id": request.id, "method": request.method, "params": request.params})

    async def wait(self, request: PendingRequest) -> JsonValue:
        """A cancelled waiter must not discard the original response identity."""
        return await asyncio.shield(request.future)

    async def request(self, method: str, params: JsonValue = None, *, timeout: float = 30) -> JsonValue:
        """Make a bounded control-plane request; a timeout loses continuity."""
        request = self.reserve(method, params)
        try:
            await self.dispatch(request)
            return await asyncio.wait_for(self.wait(request), timeout)
        except (TimeoutError, asyncio.CancelledError):
            self.abort("Native control request did not settle: " + method)
            raise

    async def notify(self, method: str, params: JsonValue = None) -> None:
        """Send a notification without inventing an RPC acknowledgement."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: JsonObject) -> None:
        body = json_text(message).encode("utf-8")
        if len(body) > MAX_FRAME_BYTES:
            raise NativeProtocolError("Native outbound frame exceeds its limit")
        async with self._write_lock:
            process = self._process
            if process is None or self._failure or self._stopping:
                raise self._failure or NativeTransportError("Native input is closed")
            if self._capture:
                self._capture.record("native.outbound", message=message)
            try:
                frame = f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                async with asyncio.timeout(self._write_timeout):
                    offset = 0
                    while offset < len(frame):
                        count = await self._io(process.write_stdin, frame[offset:offset + 65536])
                        if count is None:
                            await asyncio.sleep(.005)
                        elif count <= 0 or count > len(frame) - offset:
                            raise NativeProtocolError("Invalid native pipe write count")
                        else:
                            offset += count
            except TimeoutError as error:
                self.abort("Native dispatch did not drain before its deadline")
                raise NativeTransportError("Native dispatch did not drain before its deadline") from error
            except (OSError, ValueError) as error:
                self.abort("Native input closed during dispatch")
                raise NativeTransportError("Native input closed during dispatch") from error

    def _admit(self, message: JsonObject) -> None:
        if message.get("jsonrpc") != "2.0":
            raise NativeProtocolError("Invalid native JSON-RPC version")
        identifier = message.get("id")
        if "id" in message and (isinstance(identifier, bool) or not isinstance(identifier, (str, int))):
            raise NativeProtocolError("Invalid native JSON-RPC identity")
        if "method" in message:
            method = message["method"]
            if not isinstance(method, str) or set(message) - {"jsonrpc", "id", "method", "params"}:
                raise NativeProtocolError("Malformed native request/notification")
            params = message.get("params")
            if identifier is None:
                if self._notification is None:
                    raise NativeProtocolError("Native notification handler unavailable")
                self._notification(NativeNotification(method, params))
            else:
                if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
                    raise NativeProtocolError("Invalid native callback id")
                if identifier in self._callback_ids or len(self._callbacks) >= self._max_callbacks or len(self._callback_ids) >= 65536:
                    raise NativeProtocolError("Duplicate or excessive native callbacks")
                if self._request is None:
                    raise NativeProtocolError("Native callback handler unavailable")
                self._callback_ids.add(identifier)
                # Admission runs now, before a following end/RPC can overtake it.
                work = self._request(NativeRequest(identifier, method, params))
                task = asyncio.create_task(self._run_callback(identifier, work))
                self._callbacks.add(task)
                task.add_done_callback(self._callback_done)
        else:
            if type(identifier) is not int or set(message) - {"jsonrpc", "id", "result", "error"} or (("result" in message) == ("error" in message)):
                raise NativeProtocolError("Malformed native response")
            request = self._pending.pop(identifier, None)
            if request is None or request.future.done() or not request.sent:
                raise NativeProtocolError("Unknown or duplicate native response id")
            response = RpcResponse(message.get("error") if "error" in message else message["result"], "error" in message)
            try:
                if request.observer:
                    request.observer(response)
                if response.is_error:
                    error = json_object(response.value, "RPC error")
                    if type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                        raise NativeProtocolError("Malformed native RPC error")
                    request.future.set_exception(NativeRpcError(error))
                else:
                    request.future.set_result(response.value)
            except Exception as error:
                request.future.set_exception(error)
                raise

    async def _read_loop(self) -> None:
        process = self._process
        if process is None:
            self.abort("Native stdout is unavailable")
            return
        try:
            reader = _PipeReader(self)
            while True:
                message = await read_frame(reader)
                if self._capture:
                    self._capture.record("native.inbound", message=message)
                self._admit(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._stopping and not self._expected_exit:
                logger.debug("Native input rejected: %s", type(error).__name__)
                self.abort(str(error))

    async def _run_callback(self, identifier: int | str, work: Awaitable[JsonValue]) -> None:
        result = await work
        await self._send({"jsonrpc": "2.0", "id": identifier, "result": result})

    def _callback_done(self, task: asyncio.Task[None]) -> None:
        self._callbacks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.abort("Native callback failed: " + str(error))

    async def settle_callbacks(self) -> None:
        """Join admitted callbacks; an end notification cannot bypass these."""
        while self._callbacks:
            await asyncio.gather(*tuple(self._callbacks))
        if self._failure:
            raise self._failure

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            while True:
                block = await self._io(process.read_stderr, 65536)
                if block is None:
                    await asyncio.sleep(.005)
                elif not block:
                    return
                else:
                    logger.debug("Native stderr received (%d bytes)", len(block))
        except (OSError, ValueError):
            if not self._stopping:
                self.abort("Native stderr pipe failed")

    async def _io(self, action: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
        """Serialize finite adapter calls and join threads before releasing ownership."""
        async with self._io_lock:
            task = asyncio.create_task(asyncio.to_thread(action, *args, **kwargs))
            interrupted: asyncio.CancelledError | None = None
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as error:
                    interrupted = error
            result = task.result()
            if interrupted is not None:
                raise interrupted
            return result

    async def read_stdout(self) -> bytes:
        """Read a finite pipe chunk while observing root loss independently of EOF."""
        process = self._process
        if process is None:
            raise NativeTransportError("Native stdout is unavailable")
        while True:
            chunk = await self._io(process.read_stdout, 65536)
            if chunk is not None:
                return chunk
            self._exit_code = await self._io(process.poll)
            if self._exit_code is not None:
                raise NativeTransportError("Native server root exited before its stdout settled")
            await asyncio.sleep(.005)

    def _capture_failed(self) -> None:
        self.abort("Native raw event capture failed")

    def abort(self, reason: str) -> None:
        """Revoke the generation once; effect/process settlement is joined by stop."""
        if self._failure is not None or self._stopping:
            return
        self._failure = NativeTransportError(reason)
        for request in self._pending.values():
            if not request.future.done():
                request.future.set_exception(self._failure)
                request.future.exception()  # Reserve/dispatch failures may leave no waiter.
        self._pending.clear()
        for handler in self._closed:
            handler(reason)

    def expect_exit(self) -> None:
        """Mark only an explicitly requested clean LSP shutdown as expected."""
        self._expected_exit = True

    async def stop(self) -> None:
        """Settle all owned callbacks, child processes and capture writes."""
        if self._cleanup is None:
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
            self._stopping = True
            for request in self._pending.values():
                if not request.future.done():
                    request.future.set_exception(NativeTransportError("Native transport stopped"))
                    request.future.exception()
            self._pending.clear()
            for task in tuple(self._callbacks):
                task.cancel()
            await asyncio.gather(*tuple(self._callbacks), return_exceptions=True)
            for reader_task in (self._reader, self._stderr):
                if reader_task and not reader_task.done():
                    reader_task.cancel()
            await asyncio.gather(*(task for task in (self._reader, self._stderr) if task), return_exceptions=True)
            errors: list[Exception] = []
            if self._process:
                try:
                    await self._io(self._process.stop)
                    async with asyncio.timeout(10):
                        while not await self._io(self._process.tree_stopped):
                            await asyncio.sleep(.01)
                    self._exit_code = await self._io(self._process.poll)
                    await self._io(self._process.close)
                except Exception as error:
                    errors.append(error)
            if self._capture:
                try:
                    await self._capture.close()
                except Exception as error:
                    errors.append(error)
            if len(errors) == 1:
                raise errors[0]
            if errors:
                raise ExceptionGroup("Native transport cleanup failed", errors)
