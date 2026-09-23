#!/usr/bin/env python3
"""Bounded, standard-library native IDE LSP acceptance probe. No CLI or MCP."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, TypeVar
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from acp_proxy.config import build_subprocess_env, config_path
from acp_proxy.copilot_auth import inject_prior_copilot_oauth
from acp_proxy.discovery import admit_compatible_binary, find_binary

MAX_FRAME = 16 * 1024 * 1024
T = TypeVar("T")
URL_USERINFO = re.compile(r"(https?://)[^/\s@]+@", re.I)
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTHORIZATION|API_KEY", re.I)
TOKEN_PATTERN = re.compile(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|Bearer\s+[^\s\"']+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", re.I)
SETTINGS = {"telemetry": {"telemetryLevel": "off"}, "github": {"copilot": {
    "enableBuiltInGitHubMcpServer": False,
}}}


class ProtocolError(RuntimeError):
    """An observed protocol/effect cannot satisfy this diagnostic contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProtocolError(message)


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "<redacted>")
        return URL_USERINFO.sub(r"\1<redacted>@", TOKEN_PATTERN.sub("<redacted>", value))
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    return value


async def settled_io(function: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Cancellation cannot detach an owned filesystem job from its caller."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def spawn_owned(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
    task = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        process = await task
        await reap(process)
        raise


def file_sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


async def read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        header = await reader.readuntil(b"\r\n\r\n")
        require(len(header) <= 8192, "LSP header exceeds 8192 bytes")
        fields: dict[str, str] = {}
        for line in header[:-4].decode("ascii").split("\r\n"):
            key, sep, value = line.partition(":")
            key = key.lower()
            require(bool(sep) and key not in fields, "Invalid/duplicate LSP header")
            fields[key] = value.strip()
        length = fields.get("content-length", "")
        require(length.isascii() and length.isdecimal(), "Missing/invalid Content-Length")
        require(0 < int(length) <= MAX_FRAME, "LSP body exceeds size limit")
        body = await reader.readexactly(int(length))
        message = json.loads(body)
        require(isinstance(message, dict) and message.get("jsonrpc") == "2.0", "Invalid JSON-RPC envelope")
        ident = message.get("id")
        require("id" not in message or (type(ident) is int or isinstance(ident, str)), "Invalid JSON-RPC id")
        if "method" in message:
            require(isinstance(message["method"], str) and not ({"result", "error"} & message.keys()), "Invalid request envelope")
            require(not (message.keys() - {"jsonrpc", "id", "method", "params"}), "Unknown request envelope fields")
        else:
            require("id" in message and (("result" in message) != ("error" in message)), "Invalid response envelope")
            require(not (message.keys() - {"jsonrpc", "id", "result", "error"}), "Unknown response envelope fields")
        return message
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("Language-server EOF; pending operations have uncertain outcomes") from exc
    except (UnicodeError, ValueError, asyncio.LimitOverrunError) as exc:
        raise ProtocolError("Malformed LSP frame: " + str(exc)) from exc


async def reap(process: asyncio.subprocess.Process) -> None:
    """Reap a directly owned process, including cancellation/failure paths."""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass  # The child exited between returncode inspection and signalling.
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass  # wait() below remains the ownership boundary.
    await process.wait()


class Effects:
    """Three one-shot effects; untrusted tool input never becomes executable argv."""

    def __init__(self, workspace: Path, child_env: dict[str, str], nonce: str) -> None:
        self.workspace = workspace.resolve()
        self.target = self.workspace / "hello_world.py"
        self.initial_source = f"print('initial {nonce}')\n"
        self.final_source = f"print('hello world {nonce}')"
        self.expected_stdout = f"hello world {nonce}\n"
        self.argv = [sys.executable, str(self.target)]
        self.command = subprocess.list2cmdline(self.argv) if os.name == "nt" else shlex.join(self.argv)
        self.child_env = {key: value for key, value in child_env.items()
                          if not SECRET_NAME.search(key) and not key.upper().startswith("GIT_CONFIG_")}
        self.phase = "create"
        self.receipt: str | None = None
        self.execution: dict[str, Any] | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.calls: list[str] = []
        self.lock = asyncio.Lock()

    def validate(self, name: str, input: dict[str, Any], *, confirmation: bool = False) -> dict[str, Any]:
        require(isinstance(input, dict), "Tool input must be an object")
        fields = {"smoke_create_file": {"filePath", "content"},
                  "smoke_edit_file": {"filePath", "code", "explanation"},
                  "run_in_terminal": {"command", "cwd"}}
        require(name in fields, "Unregistered tool: " + str(name))
        allowed = fields[name] | ({"toolType", "commandLineType"} if confirmation else set())
        require(not (input.keys() - allowed) and fields[name] <= input.keys(), "Unknown/missing tool input fields")
        require(all(isinstance(input[key], str) for key in input), "Tool input values must be strings")
        if "toolType" in input:
            require(input["toolType"] == ("terminal" if name == "run_in_terminal" else "safe_tool"), "Invalid confirmation toolType")
        if "commandLineType" in input:
            require(name == "run_in_terminal" and input["commandLineType"] in {"sh", "powershell", "cmd"}, "Invalid confirmation commandLineType")
        expected_phase = {"smoke_create_file": "create", "smoke_edit_file": "edit", "run_in_terminal": "run"}[name]
        require(self.phase == expected_phase, f"Out-of-order/repeated effect: {name} during {self.phase}")
        require(not self.target.is_symlink() and self.workspace.resolve() == self.workspace, "Scratch path changed")
        if name == "run_in_terminal":
            require(input["command"] == self.command and Path(input["cwd"]).is_absolute() and Path(input["cwd"]).resolve() == self.workspace, "Command/cwd is not the exact allowlisted value")
            require(self.target.read_bytes() == self.final_source.encode(), "Scratch source does not match executable allowlist")
        else:
            require(Path(input["filePath"]).is_absolute() and Path(input["filePath"]).resolve() == self.target, "File path is not the exact scratch target")
            if name == "smoke_create_file":
                require(not self.target.exists() and input["content"] == self.initial_source, "Create content/target mismatch")
            else:
                require(input["code"] == self.final_source and self.target.read_bytes() == self.initial_source.encode(), "Edit content/target mismatch")
        return {key: input[key] for key in fields[name]}

    async def invoke(self, name: str, input: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            data = await settled_io(self.validate, name, input)
            if name == "smoke_create_file":
                await settled_io(self._create, data["content"])
                self.phase = "edit"
                value = self.initial_source
            elif name == "smoke_edit_file":
                await settled_io(self.target.write_bytes, data["code"].encode())
                self.phase = "run"
                value = self.final_source  # Return the actual file content to the model.
            else:
                self.phase = "running"
                self.process = await spawn_owned(*self.argv, cwd=self.workspace, env=self.child_env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(self.process.communicate(), 10)
                finally:
                    await reap(self.process)
                self.execution = {"argv": self.argv, "cwd": str(self.workspace), "pid": self.process.pid,
                                  "stdout": stdout.decode("utf-8"), "stderr": stderr.decode("utf-8"), "returncode": self.process.returncode}
                require(self.execution["stdout"].replace("\r\n", "\n") == self.expected_stdout and not stderr and self.process.returncode == 0,
                        "Allowlisted command output/exit mismatch")
                self.receipt = "execution-receipt-" + uuid.uuid4().hex
                value = json.dumps({**self.execution, "execution_receipt": self.receipt})
                self.phase = "done"
            self.calls.append(name)
            return {"content": [{"value": value}], "status": "success"}

    def _create(self, content: str) -> None:
        with self.target.open("xb") as file:
            file.write(content.encode("utf-8"))

    async def close(self) -> None:
        if self.process is not None:
            await reap(self.process)


class NativeClient:
    def __init__(self, process: asyncio.subprocess.Process, wire: Callable[[str, Any], None], effects: Effects) -> None:
        self.process, self.wire, self.effects = process, wire, effects
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.callbacks: set[asyncio.Task[None]] = set()
        self.callback_ids: set[str | int] = set()
        self.turns: dict[str, dict[str, Any]] = {}
        self.active_token: str | None = None
        self.next_id = 0
        self.fatal: ProtocolError | None = None
        self.exiting = False
        self.mcp_catalog_seen = False
        self.lock = asyncio.Lock()
        self.reader = asyncio.create_task(self.read_loop())
        self.stderr = asyncio.create_task(self.drain_stderr())

    def fail(self, error: Exception) -> None:
        if self.fatal is None:
            self.fatal = error if isinstance(error, ProtocolError) else ProtocolError(str(error))
            self.wire("failure", {"message": str(self.fatal)})
        for future in self.pending.values():
            if not future.done():
                future.set_exception(self.fatal)

    async def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        require(len(body) <= MAX_FRAME, "Outbound frame exceeds size limit")
        async with self.lock:
            self.wire("outbound", message)
            require(self.process.stdin is not None, "Language-server stdin unavailable")
            self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await self.process.stdin.drain()

    async def request(self, method: str, params: Any = None, timeout: float = 90) -> Any:
        if self.fatal:
            raise self.fatal
        self.next_id += 1
        ident = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": ident, "method": method, **({"params": params} if params is not None else {})})
            return await asyncio.wait_for(future, timeout)
        except (TimeoutError, asyncio.CancelledError):
            self.fail(ProtocolError("Request timed out/cancelled: " + method))
            raise
        finally:
            self.pending.pop(ident, None)
            if future.done() and not future.cancelled():
                future.exception()  # Retrieve a concurrent transport failure even if send() failed.

    async def notify(self, method: str, params: Any = None) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def begin_turn(self, token: str) -> None:
        require(self.active_token is None and token not in self.turns, "Concurrent/repeated turn token")
        self.active_token = token
        self.turns[token] = {"begin": None, "end": None, "reply": "", "progress_count": 0}

    def progress(self, params: Any) -> None:
        require(isinstance(params, dict) and params.get("token") == self.active_token, "Uncorrelated progress token")
        turn = self.turns[params["token"]]
        value = params.get("value")
        require(isinstance(value, dict), "Invalid progress value")
        kind = value.get("kind")
        require(kind in {"begin", "report", "end"}, "Unknown progress kind")
        require(turn["end"] is None, "Progress after end")
        if kind == "begin":
            require(turn["begin"] is None, "Duplicate turn begin")
            require(all(isinstance(value.get(key), str) and value[key] for key in ("conversationId", "turnId")), "Missing begin identity")
            turn["begin"] = value
        else:
            require(turn["begin"] is not None, "Progress before begin")
            for key in ("conversationId", "turnId"):
                require(value.get(key) == turn["begin"][key], "Progress identity changed")
        turn["progress_count"] += 1
        for chunk in [value, *value.get("editAgentRounds", [])]:
            require(isinstance(chunk, dict), "Invalid progress round")
            if "reply" in chunk:
                require(isinstance(chunk["reply"], str), "Non-text reply chunk")
                turn["reply"] += chunk["reply"]
        if kind == "end":
            turn["end"] = value
            require("error" not in value and "cancellationReason" not in value, "Turn ended with error/cancellation: " + json.dumps(value))

    def finish_turn(self, token: str, result: Any) -> dict[str, Any]:
        if self.fatal:
            raise self.fatal
        require(token == self.active_token and not self.callbacks, "Turn/callbacks not settled")
        turn = self.turns[token]
        require(isinstance(result, dict) and turn["begin"] is not None and turn["end"] is not None, "Turn lacks correlated begin/end and outer result")
        for key in ("conversationId", "turnId"):
            require(result.get(key) == turn["begin"][key], "Outer result/progress identity mismatch")
        self.active_token = None
        return {"result": result, **turn}

    async def read_loop(self) -> None:
        try:
            require(self.process.stdout is not None, "Language-server stdout unavailable")
            while True:
                message = await read_frame(self.process.stdout)
                self.wire("inbound", message)
                if "method" not in message:
                    future = self.pending.get(message["id"])
                    require(future is not None and not future.done(), "Unexpected/duplicate response id")
                    if "error" in message:
                        future.set_exception(ProtocolError("RPC error: " + json.dumps(message["error"])))
                    else:
                        future.set_result(message["result"])
                elif "id" in message:
                    require(message["id"] not in self.callback_ids, "Repeated callback id")
                    self.callback_ids.add(message["id"])
                    task = asyncio.create_task(self.callback(message))
                    self.callbacks.add(task)
                    task.add_done_callback(self.callback_done)
                elif message["method"] == "$/progress":
                    self.progress(message.get("params"))
                elif message["method"] == "copilot/mcpTools":
                    require(message.get("params") == {"servers": []}, "Nonempty/unrecognized MCP tool catalog")
                    self.mcp_catalog_seen = True
                elif message["method"] == "$/cancelRequest":
                    raise ProtocolError("Server cancelled a callback")
                # Other notifications are retained in full without claiming to interpret them.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not (self.exiting and not self.pending and not self.callbacks and isinstance(exc, ProtocolError) and str(exc).startswith("Language-server EOF")):
                self.fail(exc)

    def callback_done(self, task: asyncio.Task[None]) -> None:
        self.callbacks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.fail(error)

    def validate_callback(self, params: Any, confirmation: bool) -> tuple[str, dict[str, Any]]:
        required = {"name", "input", "conversationId", "turnId", "roundId", "toolCallId"}
        allowed = required | ({"title", "message", "annotations", "toolMetadata"} if confirmation else set())
        require(isinstance(params, dict) and required <= params.keys() and not (params.keys() - allowed), "Unknown/missing callback fields")
        require(self.fatal is None, "Tool callback after diagnostic failure")
        require(self.active_token is not None, "Tool callback outside an active turn")
        turn = self.turns[self.active_token]
        require(turn["begin"] is not None and turn["end"] is None, "Tool callback outside begin/end")
        require(all(params[key] == turn["begin"][key] for key in ("conversationId", "turnId")), "Uncorrelated tool callback")
        require(type(params["roundId"]) is int and params["roundId"] >= 0 and isinstance(params["toolCallId"], str), "Invalid tool call identity")
        if "annotations" in params:
            annotations = params["annotations"]
            require(isinstance(annotations, dict) and not (annotations.keys() - {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}), "Unknown tool annotations")
        if "toolMetadata" in params:
            metadata = params["toolMetadata"]
            require(isinstance(metadata, dict) and not (metadata.keys() - {"terminalCommandData"}), "Unknown tool metadata")
            terminal = metadata.get("terminalCommandData", {})
            require(isinstance(terminal, dict) and not (terminal.keys() - {"subCommands", "commandNames"}), "Unknown terminal metadata")
            require(all(isinstance(values, list) and all(isinstance(item, str) for item in values) for values in terminal.values()), "Invalid terminal metadata")
        name = params["name"]
        self.effects.validate(name, params["input"], confirmation=confirmation)
        return name, params["input"]

    async def callback(self, message: dict[str, Any]) -> None:
        try:
            method, params = message["method"], message.get("params", {})
            if method in {"conversation/invokeClientToolConfirmation", "conversation/invokeClientTool"}:
                confirmation = method.endswith("Confirmation")
                name, input = await settled_io(self.validate_callback, params, confirmation)
                result = [{"result": "accept"}, None] if confirmation else [await self.effects.invoke(name, input), None]
            elif method == "window/workDoneProgress/create":
                require(isinstance(params, dict) and set(params) == {"token"}, "Invalid progress-create callback")
                result = None
            elif method == "workspace/workspaceFolders":
                result = [{"uri": self.effects.workspace.as_uri(), "name": self.effects.workspace.name}]
            else:
                raise ProtocolError("Unsupported server callback: " + method)
            await self.send({"jsonrpc": "2.0", "id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(exc)
            try:
                await self.send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": str(exc)}})
            except (BrokenPipeError, ConnectionError):
                self.wire("undeliverable_callback_error", {"id": message["id"]})

    async def settle_callbacks(self) -> None:
        if self.callbacks:
            await asyncio.wait_for(asyncio.gather(*self.callbacks), 12)
        if self.fatal:
            raise self.fatal

    async def drain_stderr(self) -> None:
        require(self.process.stderr is not None, "Language-server stderr unavailable")
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            self.wire("stderr", line.decode("utf-8", errors="replace"))

    async def close(self) -> None:
        if not self.reader.done():
            self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)
        for task in tuple(self.callbacks):
            task.cancel()
        await asyncio.gather(*self.callbacks, return_exceptions=True)
        await self.effects.close()
        for task in (self.reader, self.stderr):
            if not task.done():
                task.cancel()
        await asyncio.gather(self.reader, self.stderr, return_exceptions=True)


def tool_specs(effects: Effects) -> list[dict[str, Any]]:
    descriptions = {
        "smoke_create_file": ("Create only the authorized scratch Python file with the exact requested initial content.", {"filePath": "Exact scratch path", "content": "Exact complete initial source"}),
        "smoke_edit_file": ("Replace only the authorized scratch Python file with the exact requested final source, and return the complete resulting file.", {"filePath": "Exact scratch path", "code": "Exact complete final source", "explanation": "Brief explanation"}),
        "run_in_terminal": ("Run the one authorized Python command and return actual stdout, stderr, exit code and a new execution receipt. The shell is " + ("cmd" if os.name == "nt" else "/bin/sh") + ". Exact command: " + effects.command, {"command": "Exact authorized command", "cwd": "Exact scratch directory"}),
    }
    return [{"name": name, "description": description,
             "inputSchema": {"type": "object", "properties": {key: {"type": "string", "description": text} for key, text in fields.items()}, "required": list(fields)},
             "confirmationMessages": {"title": "Native IDE scratch diagnostic", "message": "Approve only this exact allowlisted scratch operation."}}
            for name, (description, fields) in descriptions.items()]


def prepare_environment(cache: Path, output: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    path = Path(config_path())
    cfg = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    require(isinstance(cfg, dict), "Existing proxy config is not a JSON object")
    env = build_subprocess_env(cfg)
    explicit = {value for key, value in env.items() if key.upper() in {"GH_COPILOT_TOKEN", "GITHUB_COPILOT_TOKEN"} and value.strip()}
    require(len(explicit) <= 1, "Conflicting explicit Copilot OAuth tokens; select one credential before running")
    env = inject_prior_copilot_oauth(env)  # Resolve prior IDE identity before isolating all writable homes.
    secrets = tuple(value for key, value in env.items() if SECRET_NAME.search(key) and value)
    # Proxy URLs can contain credentials; redact their complete values without dropping them from the server environment.
    secrets += tuple(value for key, value in env.items() if "PROXY" in key.upper() and "@" in value)
    for key in list(env):
        upper = key.upper()
        if upper in {"NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "PYTHONHOME", "COPILOT_CLI_PATH"} or upper.startswith("GIT_CONFIG_"):
            env.pop(key)
        elif upper in {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}:
            env.pop(key)
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        child_path = cache / key.lower()
        child_path.mkdir()
        env[key] = str(child_path)
    guard = cache / "guard-bin"
    guard.mkdir()
    guard_log = output / "cli-guard.log"
    for name in ("copilot", "npm", "npx"):
        script = guard / name
        script.write_text("#!/bin/sh\nprintf '%s\\n' '" + name + " denied' >> " + shlex.quote(str(guard_log)) + "\nexit 126\n", encoding="utf-8")
        script.chmod(0o700)
        for extension in (".cmd", ".bat"):
            (guard / (name + extension)).write_bytes(("@echo off\r\necho " + name + " denied>>\"%NATIVE_SMOKE_GUARD_LOG%\"\r\nexit /b 126\r\n").encode("ascii"))
    env["NATIVE_SMOKE_GUARD_LOG"] = str(guard_log)
    env["PATH"] = str(guard) + os.pathsep + env.get("PATH", "")
    env["GITHUB_COPILOT_ACP_USE_CLI"] = "0"
    env["PYTHONNOUSERSITE"] = "1"
    return env, secrets


async def stop_server(process: asyncio.subprocess.Process) -> None:
    """Attempt tree cleanup, always reap the directly owned server even if it fails."""
    try:
        if process.returncode is None and os.name == "nt":
            killer = await spawn_owned("taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(killer.wait(), 5)
                require(killer.returncode == 0 or process.returncode is not None, "Windows process-tree cleanup failed")
            finally:
                await reap(killer)
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # A clean exit can remove the entire owned process group first.
    finally:
        await reap(process)
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # No remaining member in the session this runner created.


async def run(args: argparse.Namespace, output: Path, result: dict[str, Any]) -> None:
    process: asyncio.subprocess.Process | None = None
    client: NativeClient | None = None
    effects: Effects | None = None
    secrets: tuple[str, ...] = ()
    sequence = 0
    result["phase"] = "preflight"

    def wire(direction: str, message: Any) -> None:
        nonlocal sequence
        sequence += 1
        record = {"sequence": sequence, "monotonic": time.monotonic(), "direction": direction, "message": redact(message, secrets)}
        with (output / "wire.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    # This lifetime encloses every server and callback process, never the retained evidence.
    with tempfile.TemporaryDirectory(prefix="native-ide-smoke-") as temporary:
        try:
            binary = args.binary or await settled_io(find_binary)
            require(bool(binary), "No compatible installed copilot-language-server discovered; supply --binary")
            admission = await settled_io(admit_compatible_binary, str(binary))
            digest = await settled_io(file_sha256, Path(admission.path))
            result["binary"] = {"path": admission.path, "version": ".".join(map(str, admission.version)), "sha256": digest}
            env, secrets = await settled_io(prepare_environment, Path(temporary), output)
            workspace = output / "workspace"
            await settled_io(workspace.mkdir)
            effects = Effects(workspace, env, uuid.uuid4().hex)
            result["controls"] = {"transport": "native IDE LSP", "argv": [admission.path, "--stdio"],
                                  "GITHUB_COPILOT_ACP_USE_CLI": "0", "mcp_servers_configured": False,
                                  "cli_path_guards": ["copilot", "npm", "npx"], "credential_cache_retained": False}
            process = await spawn_owned(admission.path, "--stdio", cwd=workspace, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt", limit=MAX_FRAME)
            result["server_pid"] = process.pid
            client = NativeClient(process, wire, effects)
            folders = [{"uri": workspace.as_uri(), "name": workspace.name}]
            result["phase"] = "initialize"
            result["initialize"] = await client.request("initialize", {"processId": os.getpid(), "rootUri": workspace.as_uri(), "workspaceFolders": folders,
                "capabilities": {"workspace": {"workspaceFolders": True}, "window": {"workDoneProgress": True}},
                "initializationOptions": {"copilotCapabilities": {"mcpServerManagement": False, "mcpElicitation": False, "mcpSampling": False, "subAgent": False},
                    "editorInfo": {"name": "Native IDE smoke diagnostic", "version": "1.0"}, "editorPluginInfo": {"name": "acp-proxy diagnostic", "version": "1.0"}}})
            await client.notify("initialized", {})
            await client.notify("workspace/didChangeConfiguration", {"settings": SETTINGS})
            result["phase"] = "model_catalog"
            catalog = await client.request("copilot/models", {})
            require(isinstance(catalog, list) and all(isinstance(model, dict) and isinstance(model.get("id"), str) for model in catalog), "Unrecognized advertised model catalog")
            result["models"] = catalog
            if not args.list_models:
                require(bool(args.model), "Smoke validation requires an explicit --model from --list-models")
                selected = [model for model in catalog if model["id"] == args.model]
                require(len(selected) == 1, "Requested model is missing/ambiguous in the advertised catalog")
                model = selected[0]
                require(isinstance(model.get("scopes"), list) and "agent-panel" in model["scopes"] and isinstance(model.get("modelName"), str), "Requested model does not advertise native agent-panel capability")
                result["selected_model"] = model
                result["phase"] = "register_tools"
                registered = await client.request("conversation/registerTools", {"tools": tool_specs(effects)})
                require(isinstance(registered, list), "Unrecognized native tool registration response")
                for name in ("smoke_create_file", "smoke_edit_file", "run_in_terminal"):
                    matches = [tool for tool in registered if isinstance(tool, dict) and tool.get("name") == name]
                    require(len(matches) == 1, "Required native tool missing/ambiguous: " + name)
                    tool = matches[0]
                    expected_type, expected_provider = "client", "copilot-editor"
                    require(tool.get("status") == "enabled" and tool.get("type") == expected_type
                            and isinstance(tool.get("toolProvider"), dict) and tool["toolProvider"].get("id") == expected_provider,
                            "Required native tool is disabled or has an unfamiliar provider: " + name)
                marker = "continuity-marker-" + uuid.uuid4().hex
                common = {"chatMode": "Agent", "modelInfo": {"id": args.model}, "workspaceFolder": workspace.as_uri(), "workspaceFolders": folders,
                          "capabilities": {"allSkills": False, "skills": []}, "needToolCallConfirmation": True, "source": "panel", "computeSuggestions": False}
                prompt = ("This is an authorized scratch-only native IDE diagnostic. No CLI, MCP, delegation, package installation or other commands. "
                          "Remember this private marker for the next turn: " + marker + ". In this turn only, use smoke_create_file exactly once to create " + str(effects.target)
                          + " with these exact UTF-8 bytes (JSON string): " + json.dumps(effects.initial_source) + ". Do not edit or run yet. Confirm creation.")
                token = "create-" + uuid.uuid4().hex
                result["phase"] = "create_turn"
                client.begin_turn(token)
                first = await client.request("conversation/create", {**common, "workDoneToken": token, "turns": [{"request": prompt}]})
                await client.settle_callbacks()
                result["first_turn"] = client.finish_turn(token, first)
                require(first.get("modelName") == model["modelName"], "Resolved modelName does not match requested catalog entry")
                require(effects.phase == "edit" and effects.target.read_bytes() == effects.initial_source.encode(), "First turn did not create exact initial file via callback")
                prompt2 = ("Continue the same diagnostic. Use smoke_edit_file exactly once to replace " + str(effects.target)
                           + " with this exact complete UTF-8 source with no terminal newline (JSON string): " + json.dumps(effects.final_source)
                           + ". Then use run_in_terminal exactly once, command " + json.dumps(effects.command) + " and cwd " + json.dumps(str(workspace))
                           + ". No other files, commands, tools, CLI, MCP or delegation. After success, state the private marker from the previous turn and copy the actual stdout, exit code, "
                           "and execution_receipt from the command result. That receipt is created at execution time; never invent one. Stop and report any failure.")
                token2 = "edit-run-" + uuid.uuid4().hex
                result["phase"] = "edit_run_turn"
                client.begin_turn(token2)
                second = await client.request("conversation/turn", {**common, "workDoneToken": token2, "conversationId": first["conversationId"], "message": prompt2})
                await client.settle_callbacks()
                result["second_turn"] = client.finish_turn(token2, second)
                require(second.get("conversationId") == first["conversationId"] and second.get("turnId") != first["turnId"], "Followup did not preserve conversation and advance turn")
                require(second.get("modelName") == model["modelName"], "Followup resolved a different model")
                reply = result["second_turn"]["reply"]
                require(effects.phase == "done" and effects.calls == ["smoke_create_file", "smoke_edit_file", "run_in_terminal"], "Required effects were not observed exactly once")
                require(effects.target.read_bytes() == effects.final_source.encode(), "Final scratch file bytes changed")
                require(marker in reply and effects.receipt is not None and effects.receipt in reply and effects.expected_stdout.strip() in reply, "Final reply did not recall private marker and actual command result/receipt")
                result["evidence"] = {"callbacks": effects.calls, "final_source": effects.final_source, "execution": effects.execution,
                                      "execution_receipt": effects.receipt, "continuity_marker_recalled": True}
                result["phase"] = "destroy"
                require(await client.request("conversation/destroy", {"conversationId": first["conversationId"]}, timeout=15) == "OK", "Conversation destroy did not return OK")
            result["phase"] = "shutdown"
            await client.settle_callbacks()
            await client.request("shutdown", timeout=15)
            client.exiting = True
            await client.notify("exit")
            await asyncio.wait_for(process.wait(), 10)
            require(process.returncode == 0, "Language-server did not exit cleanly")
            await asyncio.wait_for(asyncio.gather(client.reader, client.stderr), 5)
            require(client.fatal is None and not client.pending and not client.callbacks, "Transport/callbacks did not settle cleanly")
            guard_log = output / "cli-guard.log"
            require(not guard_log.exists() or not guard_log.read_bytes(), "A prohibited CLI PATH guard was invoked")
            result["controls"]["empty_mcp_catalog_observed"] = client.mcp_catalog_seen
            require(args.list_models or client.mcp_catalog_seen, "No empty MCP server catalog was observed")
            result["status"] = "catalog_only" if args.list_models else "PASS"
            result["phase"] = "complete"
        except BaseException as exc:
            result["status"] = "FAIL"
            result["failure"] = {"type": type(exc).__name__, "message": str(exc) or "Interrupted"}
            wire("failure", result["failure"])
        finally:
            cleanup_errors: list[str] = []
            try:
                if client is not None:
                    await client.close()
                elif effects is not None:
                    await effects.close()
            except BaseException as exc:
                cleanup_errors.append("Callback cleanup: " + str(exc))
            finally:
                if process is not None:
                    try:
                        await stop_server(process)
                    except BaseException as exc:
                        cleanup_errors.append("Server/tree cleanup: " + str(exc))
                    result["server_returncode"] = process.returncode
            result["cleanup"] = {"server_reaped": process is None or process.returncode is not None,
                                 "effect_child_reaped": effects is None or effects.process is None or effects.process.returncode is not None,
                                 "errors": cleanup_errors}
            if cleanup_errors or not all(result["cleanup"][key] for key in ("server_reaped", "effect_child_reaped")):
                result["status"] = "FAIL"
                result["cleanup_failure"] = cleanup_errors or ["An owned process was not reaped"]
            if effects is not None:
                result["effect_phase"] = effects.phase
                result["observed_callbacks"] = effects.calls
            if client is not None:
                result["controls"]["empty_mcp_catalog_observed"] = client.mcp_catalog_seen
            result["cli_guard_invoked"] = (output / "cli-guard.log").exists()
            result.update(redact(result, secrets))
    result["cleanup"]["temporary_credentials_removed"] = not Path(temporary).exists()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", help="Installed copilot-language-server path; otherwise use repo discovery")
    parser.add_argument("--model", help="Exact model id advertised by --list-models (required for smoke)")
    parser.add_argument("--list-models", action="store_true", help="Only read model catalog; does not validate tool capability")
    parser.add_argument("--output-dir", type=Path, help="New or empty directory for retained redacted evidence")
    args = parser.parse_args(argv)
    output = (args.output_dir or Path(__file__).resolve().parent / "results" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])).resolve()
    result: dict[str, Any] = {"status": "FAIL", "phase": "setup", "platform": sys.platform, "python": sys.version,
                              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        output.mkdir(parents=True, exist_ok=True)
        require(not any(output.iterdir()), "Evidence output directory must be empty")
        if not args.list_models and not args.model:
            raise ProtocolError("Smoke validation requires --model; use --list-models to inspect availability")
        asyncio.run(run(args, output, result))
    except BaseException as exc:
        result["status"] = "FAIL"
        result["failure"] = {"type": type(exc).__name__, "message": str(exc) or "Interrupted"}
    try:
        with (output / "result.json").open("x", encoding="utf-8") as file:
            json.dump(redact(result), file, indent=2, ensure_ascii=False)
            file.write("\n")
    except OSError as exc:
        print("FAIL: cannot retain result.json: " + str(exc), file=sys.stderr)
        return 1
    print(result["status"] + ": " + str(output / "result.json"))
    if result["status"] == "catalog_only":
        for model in result.get("models", []):
            print(model["id"])
    elif result["status"] == "FAIL":
        print(str(result.get("failure", {})), file=sys.stderr)
    return 0 if result["status"] in {"PASS", "catalog_only"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
