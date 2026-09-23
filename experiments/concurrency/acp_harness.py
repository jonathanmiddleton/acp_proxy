"""
Lightweight async ACP harness for concurrency experiments.

Spawns copilot-language-server processes in ACP mode and provides async
primitives for session creation, prompting, and response collection.
Designed for parallel operation — multiple sessions per process, multiple
processes per experiment.

This is experiment infrastructure, not production code. It replicates a
minimal subset of the proxy's transport + client to avoid coupling the
experiment to the proxy's internal structure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _collect_text(update: dict[str, Any], parts: list[str]) -> None:
    """Extract text from an ACP session update into parts list.

    Handles both observed update formats:
    - agent_message_chunk: content is a single {type, text} object
    - turn_update: content is a list of {type, text} blocks
    """
    content = update.get("content")
    if content is None:
        return
    if isinstance(content, dict):
        # agent_message_chunk format: content is a single block
        if content.get("type") == "text":
            parts.append(content["text"])
    elif isinstance(content, list):
        # turn_update format: content is a list of blocks
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block["text"])


@dataclass
class PromptResult:
    """Result of a single prompt call."""

    session_id: str
    text: str
    stop_reason: str
    elapsed_s: float
    model: str = ""
    updates: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class AcpProcess:
    """Manages a single copilot-language-server subprocess and its sessions.

    Provides async methods for the ACP lifecycle:
    - initialize (handshake)
    - create_session
    - prompt (with streaming update collection)
    - stop (graceful shutdown)

    Thread safety: relies on asyncio single-threaded event loop.
    """

    def __init__(self, binary_path: str, label: str = "proc") -> None:
        self._binary_path = binary_path
        self.label = label
        self._process: asyncio.subprocess.Process | None = None
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._update_queues: dict[str, asyncio.Queue[dict[str, Any] | None]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False
        self.models: list[dict[str, Any]] = []
        self.default_model: str | None = None
        self.agent_name: str | None = None
        self.agent_version: str | None = None

    async def start(self, cwd: str | None = None) -> None:
        """Launch the language server and complete ACP init handshake."""
        logger.info("[%s] Starting: %s", self.label, self._binary_path)
        self._process = await asyncio.create_subprocess_exec(
            self._binary_path,
            "--acp",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()
        logger.info(
            "[%s] Initialized: %s v%s",
            self.label,
            self.agent_name,
            self.agent_version,
        )

    async def stop(self) -> None:
        """Terminate the subprocess and clean up."""
        self._closed = True
        for q in self._update_queues.values():
            q.put_nowait(None)
        self._update_queues.clear()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Transport closed"))
        self._pending.clear()
        logger.info("[%s] Stopped", self.label)

    async def create_session(self, cwd: str, model_id: str | None = None) -> str:
        """Create a new ACP session. Returns the session ID."""
        result = await self._send_request("session/new", {"cwd": cwd, "mcpServers": []})
        session_id = result["sessionId"]
        if not isinstance(session_id, str):
            logger.debug("[%s] Unexpected session/new result: %r", self.label, result)
            raise ValueError("Expected session/new to return a string sessionId")

        if "models" in result:
            models_data = result["models"]
            self.models = models_data.get("availableModels", [])
            self.default_model = models_data.get("currentModelId")

        if model_id and model_id != self.default_model:
            await self._set_model(session_id, model_id)

        logger.info("[%s] Created session %s", self.label, session_id)
        return session_id

    async def prompt(
        self,
        session_id: str,
        text: str,
        timeout: float = 120.0,
    ) -> PromptResult:
        """Send a prompt and collect the full response.

        Returns a PromptResult with the accumulated text, timing, and
        any updates received during the prompt.
        """
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._update_queues[session_id] = queue

        t0 = time.monotonic()
        updates: list[dict[str, Any]] = []
        text_parts: list[str] = []

        try:
            prompt_task = asyncio.create_task(
                self._send_request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": text}],
                    },
                )
            )

            deadline = time.monotonic() + timeout
            while True:
                remaining = max(0.01, deadline - time.monotonic())
                try:
                    update = await asyncio.wait_for(
                        queue.get(), timeout=min(0.1, remaining)
                    )
                    if update is None:
                        break
                    updates.append(update)
                    _collect_text(update, text_parts)
                except asyncio.TimeoutError:
                    if prompt_task.done():
                        while not queue.empty():
                            u = queue.get_nowait()
                            if u is not None:
                                updates.append(u)
                                _collect_text(u, text_parts)
                        break
                    if time.monotonic() >= deadline:
                        prompt_task.cancel()
                        return PromptResult(
                            session_id=session_id,
                            text="".join(text_parts),
                            stop_reason="timeout",
                            elapsed_s=time.monotonic() - t0,
                            updates=updates,
                            error=f"Timed out after {timeout}s",
                        )

            result = await prompt_task
            elapsed = time.monotonic() - t0
            return PromptResult(
                session_id=session_id,
                text="".join(text_parts),
                stop_reason=result.get("stopReason", "end_turn"),
                elapsed_s=elapsed,
                updates=updates,
            )
        except Exception as e:
            elapsed = time.monotonic() - t0
            return PromptResult(
                session_id=session_id,
                text="".join(text_parts),
                stop_reason="error",
                elapsed_s=elapsed,
                updates=updates,
                error=str(e),
            )
        finally:
            self._update_queues.pop(session_id, None)

    # --- Internal protocol methods ---

    async def _initialize(self) -> None:
        """Complete the ACP initialization handshake."""
        result = await self._send_request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "concurrency-probe", "version": "0.1.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            },
        )
        info = result.get("agentInfo", {})
        self.agent_name = info.get("name")
        self.agent_version = info.get("version")

    async def _set_model(self, session_id: str, model_id: str) -> None:
        """Set the model for a session."""
        try:
            await self._send_request(
                "session/set_model",
                {"sessionId": session_id, "modelId": model_id},
            )
        except Exception:
            logger.warning(
                "[%s] session/set_model failed, trying set_config_option",
                self.label,
            )
            await self._send_request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": model_id},
            )

    async def _send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the correlated response."""
        req_id = self._next_id
        self._next_id += 1

        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params

        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_event_loop().create_future()
        )
        self._pending[req_id] = future
        await self._write(msg)
        return await future

    async def _write(self, msg: dict[str, Any]) -> None:
        """Write a single NDJSON line to the subprocess stdin."""
        assert self._process and self._process.stdin
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._process.stdin.write(line.encode())
        await self._process.stdin.drain()
        logger.debug("[%s] >>> %s", self.label, line.rstrip())

    async def _read_loop(self) -> None:
        """Read NDJSON lines from stdout and dispatch."""
        assert self._process and self._process.stdout
        while not self._closed:
            try:
                line_bytes = await self._process.stdout.readline()
            except asyncio.CancelledError:
                break
            if not line_bytes:
                logger.info("[%s] Subprocess stdout closed", self.label)
                break
            line = line_bytes.decode().strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("[%s] Non-JSON line: %s", self.label, line[:200])
                continue
            logger.debug("[%s] <<< %s", self.label, line[:500])
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Route an incoming message."""
        if "id" in msg and "method" in msg:
            # Incoming request from the server (permission, fs, terminal)
            asyncio.create_task(self._handle_server_request(msg))
        elif "id" in msg:
            # Response to one of our requests
            req_id = msg["id"]
            future = self._pending.pop(req_id, None)
            if future and not future.done():
                if "error" in msg:
                    future.set_exception(
                        RuntimeError(msg["error"].get("message", "ACP error"))
                    )
                else:
                    future.set_result(msg.get("result", {}))
            else:
                logger.warning("[%s] Orphan response id=%s", self.label, req_id)
        else:
            # Notification
            self._handle_notification(msg)

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        """Route session/update notifications to the appropriate queue."""
        method = msg.get("method", "")
        params = msg.get("params", {})
        if method == "session/update":
            session_id = params.get("sessionId", "")
            update = params.get("update", {})
            queue = self._update_queues.get(session_id)
            if queue:
                queue.put_nowait(update)

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        """Handle incoming requests from the server.

        Auto-approves permissions. Minimal fs/terminal handling.
        """
        method = msg.get("method", "")
        params = msg.get("params", {})
        req_id = msg["id"]

        try:
            if method == "session/request_permission":
                result = self._auto_approve_permission(params)
            elif method == "fs/read_text_file":
                path = params.get("path", "")
                with open(path) as f:
                    content = f.read()
                result = {"content": content}
            elif method == "fs/write_text_file":
                path = params.get("path", "")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(params.get("content", ""))
                result = {}
            else:
                logger.warning("[%s] Unhandled server request: %s", self.label, method)
                result = None

            await self._send_response(req_id, result=result)
        except Exception as e:
            logger.error(
                "[%s] Error handling %s: %s", self.label, method, e, exc_info=True
            )
            await self._send_response(req_id, error={"code": -32603, "message": str(e)})

    def _auto_approve_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        """Auto-approve permission requests, preferring allow_always."""
        options = params.get("options", [])
        for opt in options:
            if opt.get("kind") == "allow_always":
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        for opt in options:
            if opt.get("kind") == "allow_once":
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        if options:
            return {
                "outcome": {"outcome": "selected", "optionId": options[0]["optionId"]}
            }
        return {"outcome": {"outcome": "cancelled"}}

    async def _send_response(
        self,
        req_id: int | str,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Send a JSON-RPC response."""
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        await self._write(msg)

    async def _drain_stderr(self) -> None:
        """Read stderr to prevent buffer blocking."""
        assert self._process and self._process.stderr
        while not self._closed:
            try:
                line = await self._process.stderr.readline()
            except asyncio.CancelledError:
                break
            if not line:
                break
            text = line.decode().strip()
            if text:
                logger.debug("[%s] [stderr] %s", self.label, text)
