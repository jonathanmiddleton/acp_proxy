"""Offline evidence for the portable diagnostic's consequential failure paths."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("native_ide_smoke", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class EffectsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.effects = probe.Effects(self.workspace, dict(os.environ), "offline-witness")

    async def asyncTearDown(self) -> None:
        await self.effects.close()
        self.temp.cleanup()

    async def test_wrong_path_and_code_cannot_smoke_create_files(self) -> None:
        outside = self.workspace.parent / "outside.py"
        for file_path, content in (
            (outside, self.effects.initial_source),
            (self.effects.target, "raise RuntimeError('unapproved code')\n"),
        ):
            with self.subTest(file_path=file_path, content=content):
                with self.assertRaises((ValueError, RuntimeError, PermissionError)):
                    await self.effects.invoke(
                        "smoke_create_file", {"filePath": str(file_path), "content": content}
                    )
                self.assertFalse(outside.exists())
                self.assertFalse(self.effects.target.exists())

    async def test_out_of_order_execution_does_not_start_a_command(self) -> None:
        with self.assertRaises((ValueError, RuntimeError, PermissionError)):
            await self.effects.invoke(
                "run_in_terminal",
                {"command": self.effects.command, "cwd": str(self.workspace)},
            )
        self.assertFalse(self.effects.target.exists())
        self.assertFalse(self.effects.execution)

    async def test_owned_canonical_path_spelling_is_accepted(self) -> None:
        (self.workspace / "nested").mkdir()
        equivalent = str(self.workspace) + os.sep + "nested" + os.sep + ".." + os.sep + "hello_world.py"
        await self.effects.invoke(
            "smoke_create_file", {"filePath": equivalent, "content": self.effects.initial_source}
        )
        self.assertEqual(self.effects.target.read_bytes(), self.effects.initial_source.encode("utf-8"))

    async def test_rejected_edit_preserves_created_file(self) -> None:
        await self.effects.invoke(
            "smoke_create_file",
            {"filePath": str(self.effects.target), "content": self.effects.initial_source},
        )
        before = self.effects.target.read_bytes()
        with self.assertRaises((ValueError, RuntimeError, PermissionError)):
            await self.effects.invoke(
                "smoke_edit_file",
                {"filePath": str(self.effects.target), "code": "arbitrary replacement", "explanation": "test"},
            )
        self.assertEqual(self.effects.target.read_bytes(), before)
        self.assertFalse(self.effects.execution)

    async def test_source_tampering_prevents_execution(self) -> None:
        await self.effects.invoke(
            "smoke_create_file",
            {"filePath": str(self.effects.target), "content": self.effects.initial_source},
        )
        await self.effects.invoke(
            "smoke_edit_file",
            {"filePath": str(self.effects.target), "code": self.effects.final_source, "explanation": "test"},
        )
        self.effects.target.write_text("raise RuntimeError('tampered')\n", encoding="utf-8")
        with self.assertRaises((ValueError, RuntimeError, PermissionError)):
            await self.effects.invoke(
                "run_in_terminal",
                {"command": self.effects.command, "cwd": str(self.workspace)},
            )
        self.assertFalse(self.effects.execution)


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_utf8_content_length_and_multiple_frames(self) -> None:
        messages = [
            {"jsonrpc": "2.0", "id": 1, "result": "Résumé 日本語"},
            {"jsonrpc": "2.0", "id": 2, "result": None},
        ]
        reader = asyncio.StreamReader()
        for message in messages:
            body = json.dumps(message, ensure_ascii=False).encode("utf-8")
            reader.feed_data(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        reader.feed_eof()
        self.assertEqual(await probe.read_frame(reader), messages[0])
        self.assertEqual(await probe.read_frame(reader), messages[1])

    async def test_truncated_or_oversized_frames_cannot_be_accepted(self) -> None:
        for payload in (
            b"Content-Length: 100\r\n\r\n{}",
            b"Content-Length: 67108864\r\n\r\n",
            b"Content-Length: -1\r\n\r\n",
            b"Content-Length: 2\r\nContent-Length: 3\r\n\r\n{}",
        ):
            with self.subTest(payload=payload):
                reader = asyncio.StreamReader()
                reader.feed_data(payload)
                reader.feed_eof()
                with self.assertRaises((probe.ProtocolError, asyncio.IncompleteReadError, ValueError)):
                    await asyncio.wait_for(probe.read_frame(reader), timeout=1)


class CommandLineTests(unittest.TestCase):
    def test_help_needs_no_site_packages(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-S", str(SCRIPT), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--list-models", completed.stdout)

    def test_missing_binary_fails_with_durable_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "result"
            completed = subprocess.run(
                [sys.executable, "-S", str(SCRIPT), "--binary", str(root / "missing"),
                 "--model", "unused-model", "--output-dir", str(output)],
                capture_output=True, text=True, timeout=15,
            )
            self.assertNotEqual(completed.returncode, 0)
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result.get("status"), "FAIL")
            self.assertEqual(result.get("phase"), "preflight")
            self.assertTrue(result.get("error") or result.get("failure"), result)


class SettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_waits_for_real_filesystem_worker(self) -> None:
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "worker-result"

            def write_after_release() -> None:
                started.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test worker was never released")
                target.write_bytes(b"settled")
                finished.set()

            task = asyncio.create_task(probe.settled_io(write_after_release))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "Cancellation forgot an active filesystem effect")
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
                self.assertTrue(finished.is_set())
                self.assertEqual(target.read_bytes(), b"settled")
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)


class RedactionTests(unittest.TestCase):
    def test_nested_token_and_proxy_credentials_are_redacted(self) -> None:
        payload = {"stderr": ["proxy failed https://canary-user:canary-password@proxy.example:8443/path",
                              "credential=exact-canary-token", "Bearer bearer-canary-token"]}
        encoded = json.dumps(probe.redact(payload, ("exact-canary-token",)))
        for secret in ("canary-user", "canary-password", "exact-canary-token", "bearer-canary-token"):
            self.assertNotIn(secret, encoded)
        self.assertIn("proxy.example:8443/path", encoded)


_PEER = r"""
import json, sys
header = {}
while True:
    line = sys.stdin.buffer.readline()
    if line == b"\r\n":
        break
    key, value = line.decode().split(":", 1)
    header[key.lower()] = value.strip()
request = json.loads(sys.stdin.buffer.read(int(header["content-length"])))
if sys.argv[1] == "eof":
    sys.exit(0)
def send(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()
base = {"conversationId": "conversation", "turnId": "turn"}
for value in [dict(base, kind="begin", title="test"),
              dict(base, kind="end", error={"code": -1, "message": "model rejected", "responseIsIncomplete": True})]:
    send({"jsonrpc": "2.0", "method": "$/progress", "params": {"token": "token", "value": value}})
send({"jsonrpc": "2.0", "id": request["id"], "result": dict(base, modelName="test")})
"""


_MCP_PEER = r"""
import json, sys
def send(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()
while True:
    header = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        header[key.lower()] = value.strip()
    request = json.loads(sys.stdin.buffer.read(int(header["content-length"])))
    assert request["method"] == "mcp/getTools" and request["params"] == {}
    if sys.argv[1] == "notification":
        send({"jsonrpc": "2.0", "method": "copilot/mcpTools", "params": {"servers": [{"name": "unexpected"}]}})
    response = {"jsonrpc": "2.0", "id": request["id"]}
    response.update(json.loads(sys.argv[2]))
    send(response)
"""


class McpCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_catalog(self, response: dict, *, notification: bool = False, accepted: bool = False) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            effects = probe.Effects(Path(tmp), dict(os.environ), "offline")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-c", _MCP_PEER,
                "notification" if notification else "silent", json.dumps(response),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            client = probe.NativeClient(process, lambda *_: None, effects)
            try:
                if accepted:
                    await asyncio.wait_for(client.check_mcp_catalog("before"), timeout=3)
                    await asyncio.wait_for(client.check_mcp_catalog("after"), timeout=3)
                    self.assertEqual(client.mcp_catalog_snapshots, {"before": [], "after": []})
                    self.assertFalse(client.mcp_catalog_seen)
                else:
                    with self.assertRaises(probe.ProtocolError):
                        await asyncio.wait_for(client.check_mcp_catalog("before"), timeout=3)
            finally:
                await client.close()
                if process.returncode is None:
                    process.kill()
                await process.wait()

    async def test_explicit_empty_snapshots_need_no_change_notification(self) -> None:
        await self.exercise_catalog({"result": []}, accepted=True)

    async def test_nonempty_or_malformed_snapshot_is_rejected(self) -> None:
        for catalog in ([{"name": "unexpected", "status": "stopped"}], {"servers": []}, None):
            with self.subTest(catalog=catalog):
                await self.exercise_catalog({"result": catalog})

    async def test_unavailable_catalog_method_is_not_assumed_empty(self) -> None:
        await self.exercise_catalog({"error": {"code": -32601, "message": "Method not found"}})

    async def test_empty_snapshot_cannot_hide_nonempty_notification(self) -> None:
        await self.exercise_catalog({"result": []}, notification=True)


class TransportFailureTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_peer(self, mode: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            effects = probe.Effects(Path(tmp), dict(os.environ), "offline")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", "-c", _PEER, mode,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            client = probe.NativeClient(process, lambda *_: None, effects)
            try:
                client.begin_turn("token")
                with self.assertRaises((probe.ProtocolError, ConnectionError)):
                    result = await asyncio.wait_for(
                        client.request("conversation/create", {"workDoneToken": "token"}, timeout=3),
                        timeout=4,
                    )
                    client.finish_turn("token", result)
            finally:
                await client.close()
                await effects.close()
                if process.returncode is None:
                    process.kill()
                await process.wait()
            self.assertIsNotNone(process.returncode)

    async def test_eof_rejects_pending_request(self) -> None:
        await self.exercise_peer("eof")

    async def test_normal_rpc_result_does_not_hide_progress_error(self) -> None:
        await self.exercise_peer("progress-error")


if __name__ == "__main__":
    unittest.main()
