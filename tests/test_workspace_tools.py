"""Exercise real file effects and settlement at the workspace tool boundary."""

import asyncio
import hashlib
import os
from pathlib import Path
import shlex
import sys
import threading
from unittest.mock import patch

import pytest

from meadow_bridge.permission_policy import PermissionPolicy
from meadow_bridge.json_types import json_object, parse_json
from meadow_bridge.workspace_tools import (
    ToolArgumentError,
    WorkspaceCancelled,
    WorkspaceTools,
)


def owner(workspace: Path) -> WorkspaceTools:
    return WorkspaceTools(workspace, PermissionPolicy(1, "allow_all"), env={})


def python_command(script: Path) -> str:
    """Invoke a test-owned Python script through the platform's real shell."""
    if os.name == "nt":
        executable = str(Path(sys.executable)).replace("'", "''")
        script_path = str(script).replace("'", "''")
        return f"& '{executable}' '{script_path}'; exit $LASTEXITCODE"
    return shlex.join((sys.executable, str(script)))


@pytest.mark.asyncio
async def test_create_and_multifile_exact_edits_preserve_bytes_and_receipts(
    tmp_path: Path,
) -> None:
    tools = owner(tmp_path)
    try:
        create = tools.decode(
            "workspace_create_files",
            {
                "files": [
                    {"path": "first.txt", "content": "one\r\ntwo\r\n"},
                    {"path": "second.txt", "content": "snowman \u2603\n"},
                ]
            },
        )
        assert tools.confirm(create).allowed
        created = await tools.execute(create)
        assert created.error is None and len(created.receipts) == 2
        edited = await tools.execute(
            tools.decode(
                "workspace_edit_files",
                {
                    "files": [
                        {
                            "path": "first.txt",
                            "edits": [
                                {"old_text": "one", "new_text": "ONE"},
                                {"old_text": "two", "new_text": "TWO"},
                            ],
                        },
                        {
                            "path": "second.txt",
                            "edits": [{"old_text": "\u2603", "new_text": "sun"}],
                        },
                    ]
                },
            )
        )
        assert edited.error is None and len(edited.receipts) == 2
        assert (tmp_path / "first.txt").read_bytes() == b"ONE\r\nTWO\r\n"
        assert (tmp_path / "second.txt").read_bytes() == b"snowman sun\n"
        receipt = edited.to_json()["receipts"]
        assert isinstance(receipt, list) and isinstance(receipt[0], dict)
        assert (
            receipt[0]["before_sha256"] == hashlib.sha256(b"one\r\ntwo\r\n").hexdigest()
        )
        assert (
            receipt[0]["after_sha256"] == hashlib.sha256(b"ONE\r\nTWO\r\n").hexdigest()
        )
    finally:
        await tools.close()


@pytest.mark.asyncio
async def test_partial_batch_reports_only_committed_files(tmp_path: Path) -> None:
    (tmp_path / "occupied.txt").write_text("untouched", encoding="utf-8")
    tools = owner(tmp_path)
    try:
        result = await tools.execute(
            tools.decode(
                "workspace_create_files",
                {
                    "files": [
                        {"path": "created.txt", "content": "first"},
                        {"path": "occupied.txt", "content": "replacement"},
                        {"path": "never.txt", "content": "last"},
                    ]
                },
            )
        )
        assert result.error is not None and result.error.code == "file_exists"
        assert len(result.receipts) == 1
        assert (tmp_path / "created.txt").read_text() == "first"
        assert (tmp_path / "occupied.txt").read_text() == "untouched"
        assert not (tmp_path / "never.txt").exists()
        assert not list(tmp_path.glob(".meadow-bridge-*"))
    finally:
        await tools.close()


@pytest.mark.asyncio
async def test_wrong_or_ambiguous_context_cannot_modify_file(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    tools = owner(tmp_path)
    try:
        for content, old in (
            (b"same same\n", "missing"),
            (b"same same\n", "same"),
            (b"aaa", "aa"),
        ):
            target.write_bytes(content)
            result = await tools.execute(
                tools.decode(
                    "workspace_edit_files",
                    {
                        "files": [
                            {
                                "path": "target.txt",
                                "edits": [{"old_text": old, "new_text": "changed"}],
                            },
                        ]
                    },
                )
            )
            assert result.error is not None and result.error.code == "context_mismatch"
            assert result.receipts == ()
            assert target.read_bytes() == content
    finally:
        await tools.close()


@pytest.mark.asyncio
async def test_execution_revalidates_path_after_confirmation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tools = owner(workspace)
    try:
        operation = tools.decode(
            "workspace_create_files",
            {
                "files": [
                    {"path": "../outside/escaped.txt", "content": "must not write"},
                ]
            },
        )
        assert tools.confirm(operation).allowed
        result = await tools.execute(operation)
        assert (
            result.error is not None and result.error.code == "path_outside_workspace"
        )
        assert result.receipts == () and not (outside / "escaped.txt").exists()
    finally:
        await tools.close()


@pytest.mark.asyncio
async def test_cancellation_settles_offloaded_file_io_before_return(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"before")
    prior = tmp_path / "prior.txt"
    prior.write_bytes(b"first")
    started, release = threading.Event(), threading.Event()
    real_read = Path.read_bytes

    def controlled_read(path: Path) -> bytes:
        if path == target:
            started.set()
            if not release.wait(5):
                raise RuntimeError("test did not release file read")
        return real_read(path)

    tools = owner(tmp_path)
    task: asyncio.Task[object] | None = None
    try:
        with patch.object(Path, "read_bytes", controlled_read):
            task = asyncio.create_task(
                tools.execute(
                    tools.decode(
                        "workspace_edit_files",
                        {
                            "files": [
                                {
                                    "path": "prior.txt",
                                    "edits": [
                                        {"old_text": "first", "new_text": "completed"}
                                    ],
                                },
                                {
                                    "path": "target.txt",
                                    "edits": [
                                        {"old_text": "before", "new_text": "after"}
                                    ],
                                },
                            ]
                        },
                    )
                )
            )
            assert await asyncio.to_thread(started.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(WorkspaceCancelled) as cancelled:
                await task
            assert len(cancelled.value.result.receipts) == 1
            assert cancelled.value.result.receipts[0].path == str(prior)
            assert cancelled.value.result.error is not None
        await tools.close()
        assert target.read_bytes() == b"before"
        assert prior.read_bytes() == b"completed"
        assert not list(tmp_path.glob(".meadow-bridge-*"))
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await tools.close()


@pytest.mark.asyncio
async def test_close_revokes_new_effects(tmp_path: Path) -> None:
    tools = owner(tmp_path)
    operation = tools.decode(
        "workspace_create_files",
        {
            "files": [
                {"path": "not-created.txt", "content": "no"},
            ]
        },
    )
    await tools.close()
    with pytest.raises(RuntimeError, match="closed"):
        await tools.execute(operation)
    assert not (tmp_path / "not-created.txt").exists()


def test_tool_input_does_not_coerce_or_accept_unrecognized_fields(
    tmp_path: Path,
) -> None:
    tools = owner(tmp_path)
    with pytest.raises(ToolArgumentError):
        tools.decode("create_file", {"path": "x", "content": "y"})
    with pytest.raises(ToolArgumentError):
        tools.decode("workspace_create_files", {"files": [{"path": "x", "content": 7}]})
    with pytest.raises(ToolArgumentError):
        tools.decode(
            "workspace_run_command",
            {"command": "echo hello", "cwd": ".", "timeout": True},
        )


@pytest.mark.asyncio
async def test_command_effect_uses_workspace_cwd_and_excludes_launch_authority(
    tmp_path: Path,
) -> None:
    script = tmp_path / "observe.py"
    script.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "Path('effect.txt').write_text('completed')\n"
        "print(json.dumps({'cwd':os.getcwd(),'launch_present':any(k in os.environ for k in "
        "['MEADOW_BRIDGE_MEADOW_SECRET','MEADOW_BRIDGE_CONTAINER_BOUNDARY','ACP_PROXY_MEADOW_SECRET','ACP_PROXY_CONTAINER_BOUNDARY']),"
        "'retained':os.environ.get('WORKSPACE_TOOL_CANARY')}))\n",
        encoding="utf-8",
    )
    command = python_command(script)
    env = dict(os.environ)
    env.update(
        {
            "MEADOW_BRIDGE_MEADOW_SECRET": "private-launch-canary",
            "MEADOW_BRIDGE_CONTAINER_BOUNDARY": "1",
            "ACP_PROXY_MEADOW_SECRET": "old-launch-canary",
            "ACP_PROXY_CONTAINER_BOUNDARY": "1",
            "WORKSPACE_TOOL_CANARY": "preserved",
        }
    )
    tools = WorkspaceTools(tmp_path, PermissionPolicy(1, "allow_all"), env=env)
    try:
        result = await tools.execute(
            tools.decode(
                "workspace_run_command",
                {
                    "command": command,
                    "cwd": ".",
                    "timeout": 10,
                },
            )
        )
        assert result.error is None
        value = result.to_json()["command"]
        assert isinstance(value, dict) and isinstance(value["stdout"], str)
        observed = json_object(parse_json(value["stdout"]))
        observed_cwd = observed["cwd"]
        assert isinstance(observed_cwd, str) and Path(observed_cwd) == tmp_path
        assert observed["launch_present"] is False
        assert observed["retained"] == "preserved"
        assert value["filesystem_effects_enumerated"] is False
        assert (tmp_path / "effect.txt").read_text() == "completed"
    finally:
        await tools.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["cancel", "caller_cancel", "close"])
async def test_workspace_command_cancellation_preserves_settled_evidence(
    tmp_path: Path,
    termination: str,
) -> None:
    ready = tmp_path / "ready"
    script = tmp_path / "controlled.py"
    script.write_text(
        "from pathlib import Path\nimport time\n"
        "print('captured effect',flush=True)\n"
        f"Path({str(ready)!r}).touch()\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    tools = WorkspaceTools(tmp_path, PermissionPolicy(1, "allow_all"))
    task = asyncio.create_task(
        tools.execute(
            tools.decode(
                "workspace_run_command",
                {
                    "command": python_command(script),
                    "cwd": ".",
                    "timeout": 10,
                },
            )
        )
    )
    try:
        async with asyncio.timeout(5):
            while not ready.exists():
                await asyncio.sleep(0.01)
        if termination == "cancel":
            await tools.cancel()
        elif termination == "close":
            await tools.close()
        else:
            task.cancel()
        if termination == "caller_cancel":
            with pytest.raises(WorkspaceCancelled) as caught:
                await task
            result = caught.value.result
        else:
            result = await task
        assert result.error is not None and result.error.code == "cancelled"
        command = result.to_json()["command"]
        assert isinstance(command, dict) and isinstance(command["stdout"], str)
        assert "captured effect" in command["stdout"]
        assert command["exit_code"] != 0
        assert command["timed_out"] is False
    finally:
        await tools.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_publication_cleanup_failure_still_reports_committed_effect(
    tmp_path: Path,
) -> None:
    real_unlink = Path.unlink

    def deny_staging_cleanup(path: Path, missing_ok: bool = False) -> None:
        if path.name.startswith(".meadow-bridge-"):
            raise PermissionError("controlled cleanup denial")
        real_unlink(path, missing_ok=missing_ok)

    tools = owner(tmp_path)
    try:
        with patch.object(Path, "unlink", deny_staging_cleanup):
            result = await tools.execute(
                tools.decode(
                    "workspace_create_files",
                    {
                        "files": [
                            {"path": "committed.txt", "content": "published"},
                        ]
                    },
                )
            )
        assert (
            result.error is not None and result.error.code == "staging_cleanup_failed"
        )
        assert len(result.receipts) == 1
        assert (tmp_path / "committed.txt").read_text() == "published"
        for staged in tmp_path.glob(".meadow-bridge-*"):
            staged.unlink()
    finally:
        await tools.close()
