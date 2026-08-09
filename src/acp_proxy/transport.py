"""
NDJSON transport layer for ACP communication.

Manages the copilot-language-server subprocess and provides async
send/receive of JSON-RPC messages over stdin/stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)
MAX_INCOMING_REQUEST_TASKS = 64
MAX_ACP_STDOUT_LINE_BYTES = 5_000_000
STDERR_DRAIN_CHUNK_BYTES = 64 * 1024
_SAFE_METHOD_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


def _safe_method_name(message: dict[str, Any]) -> str:
    """Return a bounded protocol method label without persisting payload data."""

    method = message.get("method")
    if isinstance(method, str) and _SAFE_METHOD_PATTERN.fullmatch(method):
        return method
    return "<absent-or-invalid>"


def _message_kind(message: dict[str, Any]) -> str:
    """Classify one JSON-RPC envelope without inspecting its content fields."""

    if "method" in message:
        return "request" if "id" in message else "notification"
    if "id" in message:
        return "error_response" if "error" in message else "response"
    return "invalid"


class AcpTransport:
    """Async NDJSON transport over a subprocess's stdin/stdout.

    Handles:
    - Launching the copilot-language-server process
    - Sending JSON-RPC requests/notifications
    - Dispatching incoming messages to registered handlers
    - Request/response correlation via pending futures
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pending_requests: dict[
            int, tuple[str, dict[str, Any] | None]
        ] = {}
        self._notification_handler: Callable[[dict[str, Any]], None] | None = None
        self._request_handler: (
            Callable[[dict[str, Any]], asyncio.Future[dict[str, Any]] | None] | None
        ) = None
        self._request_observer: Callable[[dict[str, Any]], None] | None = None
        self._request_sent_observer: (
            Callable[[int, str, dict[str, Any] | None], None] | None
        ) = None
        self._response_observer: (
            Callable[[dict[str, Any], str, dict[str, Any] | None], None] | None
        ) = None
        self._incoming_request_tasks: dict[asyncio.Task[None], str | None] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False
        self._failed = False
        self._strict_response_correlation = False
        self._close_handler: Callable[[], None] | None = None

    @property
    def is_open(self) -> bool:
        """Whether the owned ACP stream can still accept new operations."""

        return self._process is not None and not self._closed and not self._failed

    async def start(
        self,
        binary_path: str,
        env: dict[str, str] | None = None,
    ) -> None:
        """Launch the language server subprocess in ACP mode.

        Args:
            binary_path: Path to the copilot-language-server binary.
            env: Environment variables for the subprocess.  If None, the
                current process environment is inherited.  Use
                :func:`config.build_subprocess_env` to construct an env
                dict with proxy settings applied.
        """
        logger.info("Starting copilot-language-server: %s", binary_path)
        self._closed = False
        self._failed = False
        self._process = await asyncio.create_subprocess_exec(
            binary_path,
            "--acp",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=MAX_ACP_STDOUT_LINE_BYTES,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        # Drain stderr to avoid blocking
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def stop(self) -> None:
        """Terminate the subprocess and clean up."""
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        await self._cancel_incoming_request_tasks()
        if self._process:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    self._process.kill()
                except ProcessLookupError:
                    pass
                await self._process.wait()
        await self._settle_stderr_task()
        self._fail_pending(ConnectionError("Transport closed"))

    async def abort(self) -> None:
        """Fail ownership immediately, notify the owner, then kill the child."""

        self.fail_closed("ACP transport was aborted")
        await self.stop()

    def fail_closed(self, public_message: str = "ACP protocol failure") -> None:
        """Synchronously revoke transport continuity and notify its owner once.

        This is used from the read path, where waiting for process teardown would
        self-cancel the reader.  The owner observes the callback and performs the
        asynchronous child shutdown; no further writes are admitted meanwhile.
        ``public_message`` must contain no agent-controlled detail.
        """

        if self._closed or self._failed:
            return
        self._failed = True
        self._fail_pending(ConnectionError(public_message))
        if self._close_handler is not None:
            try:
                self._close_handler()
            except Exception:  # noqa: BLE001 - observer failure must not escape
                logger.warning("ACP transport close observer failed")

    def _fail_pending(self, error: ConnectionError) -> None:
        """Reject every pending request exactly once with a public-safe cause."""

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(error)
        self._pending.clear()
        self._pending_requests.clear()

    def on_close(self, handler: Callable[[], None]) -> None:
        """Register the owner callback for unexpected ACP stdout closure."""

        self._close_handler = handler

    def on_notification(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Register a handler for incoming notifications (no 'id' field)."""
        self._notification_handler = handler

    def on_request(
        self,
        handler: Callable[[dict[str, Any]], asyncio.Future[dict[str, Any]] | None],
    ) -> None:
        """Register a handler for incoming requests (has 'id' and 'method').

        The handler receives the full JSON-RPC request and should return
        a Future that resolves to the response result, or None to reject.
        """
        self._request_handler = handler

    def on_request_observed(
        self, observer: Callable[[dict[str, Any]], None]
    ) -> None:
        """Observe inbound request order before asynchronous callback handling."""

        self._request_observer = observer

    def on_request_sent(
        self,
        observer: Callable[[int, str, dict[str, Any] | None], None],
    ) -> None:
        """Observe an outbound request immediately after its bytes are drained."""

        self._request_sent_observer = observer

    def on_response_observed(
        self,
        observer: Callable[
            [dict[str, Any], str, dict[str, Any] | None], None
        ],
    ) -> None:
        """Observe a response in read order before its future is resolved."""

        self._response_observer = observer

    def set_strict_response_correlation(self, enabled: bool) -> None:
        """Select fail-closed handling for unknown/late response IDs."""

        self._strict_response_correlation = enabled

    def has_pending_incoming_requests(self, session_id: str) -> bool:
        """Whether an agent callback for ``session_id`` remains unsettled."""

        return any(
            task_session_id == session_id and not task.done()
            for task, task_session_id in self._incoming_request_tasks.items()
        )

    def has_pending_request(self, method: str) -> bool:
        """Whether an outbound request of ``method`` remains unresolved."""

        return any(
            pending_method == method
            for pending_method, _params in self._pending_requests.values()
        )

    def pending_request_count(self, method: str) -> int:
        """Count unresolved outbound requests for one method."""

        return sum(
            pending_method == method
            for pending_method, _params in self._pending_requests.values()
        )

    async def send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the response."""
        req_id = self._next_id
        self._next_id += 1

        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params

        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_event_loop().create_future()
        )
        self._pending[req_id] = future
        self._pending_requests[req_id] = (method, params)

        try:
            await self._write(msg)
            if self._request_sent_observer is not None:
                self._request_sent_observer(req_id, method, params)
        except Exception:
            self._pending.pop(req_id, None)
            self._pending_requests.pop(req_id, None)
            future.cancel()
            raise
        return await future

    async def send_notification(
        self, method: str, params: dict[str, Any] | None = None
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def send_response(
        self, req_id: int | str, result: Any = None, error: dict[str, Any] | None = None
    ) -> None:
        """Send a JSON-RPC response to an incoming request."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        await self._write(msg)

    async def _write(self, msg: dict[str, Any]) -> None:
        """Write a single NDJSON line to stdin."""
        if not self.is_open:
            raise ConnectionError("ACP transport is not available")
        assert self._process and self._process.stdin
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        encoded = line.encode()
        self._process.stdin.write(encoded)
        await self._process.stdin.drain()
        logger.debug(
            "ACP wire outbound: kind=%s method=%s bytes=%d",
            _message_kind(msg),
            _safe_method_name(msg),
            len(encoded),
        )

    async def _read_loop(self) -> None:
        """Read NDJSON lines from stdout and dispatch."""
        assert self._process and self._process.stdout
        try:
            while not self._closed and not self._failed:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    logger.info("Subprocess stdout closed")
                    if not self._closed:
                        self.fail_closed(
                            "ACP subprocess stdout closed unexpectedly"
                        )
                    break
                line = line_bytes.decode().strip()
                if not line:
                    continue
                msg = json.loads(line)
                if not isinstance(msg, dict):
                    raise TypeError("ACP JSON-RPC message must be an object")

                logger.debug(
                    "ACP wire inbound: kind=%s method=%s bytes=%d",
                    _message_kind(msg),
                    _safe_method_name(msg),
                    len(line_bytes),
                )
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            logger.warning("Malformed ACP stdout; revoking child continuity")
            self.fail_closed("ACP protocol failure")
        except Exception:  # noqa: BLE001 - any dispatch defect loses continuity
            logger.warning("ACP stdout dispatch failed; revoking child continuity")
            self.fail_closed("ACP protocol failure")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming message to the appropriate handler."""
        if self._strict_response_correlation and not self._valid_strict_envelope(msg):
            logger.warning("Malformed direct ACP envelope; revoking continuity")
            self.fail_closed("direct ACP envelope validation failure")
            return
        if "id" in msg and "method" in msg:
            # Incoming request from agent (e.g., fs/read_text_file, session/request_permission)
            if self._request_observer is not None:
                self._request_observer(msg)
            if self._failed:
                return
            if self._request_handler:
                if len(self._incoming_request_tasks) >= MAX_INCOMING_REQUEST_TASKS:
                    self.fail_closed("ACP callback capacity exceeded")
                    return
                params = msg.get("params")
                session_id = (
                    params.get("sessionId") if isinstance(params, dict) else None
                )
                task = asyncio.create_task(self._handle_incoming_request(msg))
                self._incoming_request_tasks[task] = (
                    session_id if isinstance(session_id, str) else None
                )
                task.add_done_callback(
                    lambda done: self._incoming_request_tasks.pop(done, None)
                )
            else:
                logger.warning(
                    "No request handler for method=%s", _safe_method_name(msg)
                )
        elif "id" in msg:
            # Response to one of our requests
            req_id = msg["id"]
            if self._strict_response_correlation and not self._valid_strict_response(
                msg
            ):
                logger.warning("Malformed direct ACP response; revoking continuity")
                self.fail_closed("direct ACP response correlation failure")
                return
            future = self._pending.get(req_id)
            request = self._pending_requests.get(req_id)
            if future and not future.done():
                if self._response_observer is not None and request is not None:
                    method, params = request
                    self._response_observer(msg, method, params)
                if self._failed or future.done():
                    return
                self._pending.pop(req_id, None)
                self._pending_requests.pop(req_id, None)
                if "error" in msg:
                    logger.debug(
                        "ACP error response received: code_present=%s",
                        isinstance(msg["error"], dict)
                        and "code" in msg["error"],
                    )
                    future.set_exception(
                        AcpError(
                            msg["error"].get("message", "Unknown error"), msg["error"]
                        )
                    )
                else:
                    future.set_result(msg.get("result", {}))
            else:
                if self._strict_response_correlation:
                    logger.warning("Uncorrelated ACP response; revoking continuity")
                    self.fail_closed("direct ACP response correlation failure")
                else:
                    logger.warning(
                        "Unexpected ACP response: id_type=%s",
                        type(req_id).__name__,
                    )
        else:
            # Notification
            if self._notification_handler:
                self._notification_handler(msg)

    async def _handle_incoming_request(self, msg: dict[str, Any]) -> None:
        """Handle an incoming request from the agent."""
        assert self._request_handler
        try:
            result = self._request_handler(msg)
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                result = await result
            await self.send_response(msg["id"], result=result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - callback failures become JSON-RPC errors
            logger.warning(
                "Error handling ACP request method=%s", _safe_method_name(msg)
            )
            try:
                await self.send_response(
                    msg["id"],
                    error={"code": -32603, "message": "Internal error"},
                )
            except ConnectionError:
                logger.debug("Transport closed before callback error response")

    async def _cancel_incoming_request_tasks(self) -> None:
        """Cancel and settle every callback task owned by this transport."""

        current = asyncio.current_task()
        tasks = [
            task
            for task in self._incoming_request_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in list(self._incoming_request_tasks):
            if task is not current:
                self._incoming_request_tasks.pop(task, None)

    async def _drain_stderr(self) -> None:
        """Drain bounded chunks without retaining or parsing agent stderr."""
        assert self._process and self._process.stderr
        while True:
            chunk = await self._process.stderr.read(STDERR_DRAIN_CHUNK_BYTES)
            if not chunk:
                break
            logger.debug("ACP child stderr chunk: bytes=%d", len(chunk))

    async def _settle_stderr_task(self) -> None:
        """Settle the owned stderr drain task after child termination."""

        task = self._stderr_task
        self._stderr_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except (OSError, RuntimeError, ValueError):
            logger.warning("ACP child stderr drain failed")

    @staticmethod
    def _valid_strict_response(message: dict[str, Any]) -> bool:
        """Validate the correlation-critical JSON-RPC response envelope."""

        if message.get("jsonrpc") != "2.0":
            return False
        if type(message.get("id")) is not int:
            return False
        has_result = "result" in message
        has_error = "error" in message
        if has_result == has_error:
            return False
        return not has_error or isinstance(message["error"], dict)

    @classmethod
    def _valid_strict_envelope(cls, message: dict[str, Any]) -> bool:
        """Validate direct-mode requests, notifications, and responses."""

        if message.get("jsonrpc") != "2.0":
            return False
        if "method" not in message:
            return "id" in message and cls._valid_strict_response(message)
        method = message["method"]
        if not isinstance(method, str) or not method:
            return False
        if "result" in message or "error" in message:
            return False
        if "params" in message and not isinstance(message["params"], dict):
            return False
        if "id" not in message:
            return True
        request_id = message["id"]
        return type(request_id) is int or (
            isinstance(request_id, str) and bool(request_id)
        )


class AcpError(Exception):
    """Error returned by the ACP agent."""

    def __init__(self, message: str, error_obj: dict[str, Any] | None = None):
        super().__init__(message)
        self.error_obj = error_obj or {}
