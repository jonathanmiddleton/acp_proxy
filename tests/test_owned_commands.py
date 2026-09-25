"""Real command, pipe, and descendant lifetime obligations."""

import asyncio
from collections.abc import Mapping
import importlib
import os
from pathlib import Path
import shlex
import sys
import threading

import pytest

from meadow_bridge.owned_commands import (
    CommandCancelled,
    CommandSpec,
    OwnedCommands,
    ShellSpec,
)


def _python(script: Path) -> str:
    if sys.platform == "win32":
        return (
            "$info = New-Object System.Diagnostics.ProcessStartInfo; "
            "$info.UseShellExecute = $false; $info.FileName = '"
            + sys.executable.replace("'", "''")
            + "'; $info.Arguments = '\""
            + str(script).replace("'", "''")
            + "\"'; $process = [System.Diagnostics.Process]::Start($info); "
            "$process.WaitForExit(); exit $process.ExitCode"
        )
    return shlex.join((sys.executable, str(script)))


def _script(tmp_path: Path, source: str) -> str:
    script = tmp_path / "command with spaces.py"
    script.write_text(source, encoding="utf-8")
    return _python(script)


async def _ready(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


def test_stop_reads_exit_code_after_tree_and_pipe_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-stop running observation cannot supply the settled exit status."""
    class SettlingProcess:
        """Process I/O boundary whose closed pipes precede root termination."""

        def __init__(self, argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]) -> None:
            self.exit_code: int | None = None
            self.closed = False
            self.observations: list[int | None] = []
            processes.append(self)

        def read_stdout(self, max_bytes: int) -> bytes:
            return b""

        def read_stderr(self, max_bytes: int) -> bytes:
            return b""

        def poll(self) -> int | None:
            self.observations.append(self.exit_code)
            return self.exit_code

        def stop(self) -> None:
            self.exit_code = 37

        def tree_stopped(self) -> bool:
            return self.exit_code is not None

        def close(self) -> None:
            assert self.tree_stopped()
            self.closed = True

    processes: list[SettlingProcess] = []
    suffix, name = (
        ("windows", "WindowsCommand") if sys.platform == "win32"
        else ("linux", "LinuxCommand") if sys.platform == "linux"
        else ("posix", "PosixCommand")
    )
    monkeypatch.setattr(importlib.import_module(f"meadow_bridge._owned_command_{suffix}"), name, SettlingProcess)
    stop = threading.Event()
    stop.set()
    result = OwnedCommands(os.environ)._run(CommandSpec("unused", tmp_path, 10), stop)
    assert result.exit_code == 37
    assert result.stdout == result.stderr == b""
    assert not result.timed_out
    assert len(processes) == 1
    assert processes[0].observations[0] is None
    assert processes[0].observations[-1] == 37
    assert processes[0].closed


@pytest.mark.asyncio
async def test_command_observes_cwd_environment_and_drains_both_pipes(
    tmp_path: Path,
) -> None:
    command = _script(
        tmp_path,
        "import os,sys\nassert os.getcwd() == os.environ['EXPECTED_CWD']\nassert sys.stdin.read() == ''\nsys.stdout.buffer.write(b'o' * 150000)\nsys.stderr.buffer.write(b'e' * 150000)\nsys.exit(7)\n",
    )
    owner = OwnedCommands(
        {**os.environ, "EXPECTED_CWD": str(tmp_path)}, output_limit_bytes=1024
    )
    try:
        result = await owner.execute(CommandSpec(command, tmp_path, 10))
    finally:
        await owner.close()
    assert result.stdout == b"o" * 1024
    assert result.stderr == b"e" * 1024
    assert result.stdout_truncated and result.stderr_truncated
    assert result.exit_code == 7
    assert not result.timed_out


@pytest.mark.asyncio
async def test_shell_executes_payload_and_exact_limit_is_not_truncation(
    tmp_path: Path,
) -> None:
    command = _script(tmp_path, "import sys\nsys.stdout.buffer.write(b'1234')\n")
    owner = OwnedCommands(os.environ, ShellSpec(), output_limit_bytes=4)
    try:
        result = await owner.execute(CommandSpec(command, tmp_path, 10))
    finally:
        await owner.close()
    assert result.stdout == b"1234"
    assert result.stderr == b""
    assert not result.stdout_truncated and not result.stderr_truncated
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_shell_errors_remain_plain_text_on_the_error_stream(
    tmp_path: Path,
) -> None:
    canary = "spaces 'single' \"double\" C:\\bridge\\\u03a9"
    command = (
        "param([string]$value = 'spaces ''single'' \"double\" C:\\bridge\\\u03a9')\n"
        "$bytes = [Text.Encoding]::UTF8.GetBytes($value); "
        "[Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length); "
        "Write-Error 'bridge-shell-error'; exit 7"
        if sys.platform == "win32"
        else f"printf %s {shlex.quote(canary)}; printf 'bridge-shell-error' >&2; exit 7"
    )
    owner = OwnedCommands(os.environ)
    try:
        result = await owner.execute(CommandSpec(command, tmp_path, 10))
    finally:
        await owner.close()
    assert result.stdout == canary.encode("utf-8")
    assert b"bridge-shell-error" in result.stderr
    assert b"#< CLIXML" not in result.stderr
    assert not result.stdout_truncated and not result.stderr_truncated
    assert result.exit_code == 7


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "termination", ("timeout", "cancel", "task_cancel", "close", "leader_exit")
)
async def test_termination_settles_descendant_even_after_leader_exit(
    tmp_path: Path, termination: str
) -> None:
    ready = tmp_path / "ready"
    descendant = tmp_path / "descendant.py"
    descendant.write_text(
        "from pathlib import Path\nimport time\n"
        f"heartbeat = Path({str(ready)!r})\n"
        "for counter in range(500):\n"
        "    heartbeat.write_text(str(counter))\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    command = _script(
        tmp_path,
        "import subprocess,sys,time\nfrom pathlib import Path\nsys.stdout.write('captured-before-stop\\n'); sys.stdout.flush()\n"
        f"subprocess.Popen([sys.executable, {str(descendant)!r}])\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        + ("" if termination == "leader_exit" else "time.sleep(30)\n"),
    )
    owner = OwnedCommands(os.environ)
    task = asyncio.create_task(
        owner.execute(
            CommandSpec(command, tmp_path, 2 if termination == "timeout" else 10)
        )
    )
    try:
        await _ready(ready)
        if termination == "cancel":
            await owner.cancel()
        elif termination == "task_cancel":
            task.cancel()
        elif termination == "close":
            await owner.close()
        if termination in ("task_cancel", "cancel", "close"):
            with pytest.raises(CommandCancelled) as caught:
                await task
            assert not caught.value.result.timed_out
            assert caught.value.result.exit_code != 0
            assert b"captured-before-stop" in caught.value.result.stdout
        else:
            result = await asyncio.wait_for(task, 5)
            assert result.timed_out is (termination == "timeout")
            if termination == "leader_exit":
                assert result.exit_code == 0
        settled_heartbeat = ready.read_bytes()
        await asyncio.sleep(0.1)
        assert ready.read_bytes() == settled_heartbeat, (
            "owned descendant continued after execution settlement"
        )
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_owner_rejects_overlapping_and_closed_execution(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    command = _script(
        tmp_path,
        f"from pathlib import Path\nimport time\nPath({str(ready)!r}).touch()\ntime.sleep(30)\n",
    )
    owner = OwnedCommands(os.environ)
    spec = CommandSpec(command, tmp_path, 10)
    task = asyncio.create_task(owner.execute(spec))
    try:
        await _ready(ready)
        with pytest.raises(RuntimeError, match="active"):
            await owner.execute(spec)
        await owner.close()
        with pytest.raises(CommandCancelled):
            await task
        with pytest.raises(RuntimeError, match="closed"):
            await owner.execute(spec)
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_spawn_failure_remains_visible_during_shutdown(tmp_path: Path) -> None:
    shell = tmp_path / ("powershell.exe" if sys.platform == "win32" else "sh")
    owner = OwnedCommands(os.environ, ShellSpec(str(shell)))
    with pytest.raises(FileNotFoundError, match="shell is unavailable"):
        await owner.execute(CommandSpec("unused", tmp_path, 10))
    with pytest.raises(FileNotFoundError, match="shell is unavailable"):
        await owner.close()
    with pytest.raises(RuntimeError, match="closed"):
        await owner.execute(CommandSpec("unused", tmp_path, 10))


@pytest.mark.asyncio
async def test_invalid_executable_cannot_report_successful_settlement(
    tmp_path: Path,
) -> None:
    shell = tmp_path / ("powershell.exe" if sys.platform == "win32" else "sh")
    shell.write_bytes(b"This is deliberately not an executable image.\n")
    shell.chmod(0o700)
    owner = OwnedCommands(os.environ, ShellSpec(str(shell)))
    with pytest.raises((OSError, ExceptionGroup)) as failure:
        await owner.execute(CommandSpec("unused", tmp_path, 10))
    with pytest.raises((OSError, ExceptionGroup)) as shutdown:
        await owner.close()
    assert shutdown.value is failure.value


def test_private_process_adapter_preserves_piped_input_and_eof(tmp_path: Path) -> None:
    import time

    from meadow_bridge._owned_command_linux import LinuxCommand
    from meadow_bridge._owned_command_posix import PosixCommand
    from meadow_bridge._owned_command_windows import WindowsCommand

    adapter = (
        WindowsCommand
        if sys.platform == "win32"
        else LinuxCommand
        if sys.platform == "linux"
        else PosixCommand
    )
    process = adapter(
        (
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ),
        tmp_path,
        os.environ,
        pipe_input=True,
    )
    payload = bytes(range(256)) * 800
    written = 0
    output = bytearray()
    input_closed = False
    deadline = time.monotonic() + 10
    try:
        while True:
            assert time.monotonic() < deadline, "piped process did not finish"
            if written < len(payload):
                count = process.write_stdin(payload[written:])
                if count is not None:
                    written += count
            elif not input_closed:
                process.close_stdin()
                input_closed = True
            part = process.read_stdout(65536)
            if part is not None:
                output.extend(part)
            if part == b"" and process.poll() is not None:
                break
            time.sleep(0.005)
        assert bytes(output) == payload
        assert process.poll() == 0
    finally:
        process.stop()
        while not process.tree_stopped():
            assert time.monotonic() < deadline, "piped process cleanup did not settle"
            process.stop()
            time.sleep(0.005)
        process.close()
