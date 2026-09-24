"""Bounded, noninteractive commands with explicit shell and process ownership."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ShellSpec:
    """Select a supported shell; PowerShell 7 requires explicit selection."""

    executable: str | None = None

    def __post_init__(self) -> None:
        if self.executable is not None and (
            not self.executable or "\0" in self.executable
        ):
            raise ValueError("shell executable must be a nonempty path or name")
        if self.executable is not None:
            name = Path(self.executable).name.lower()
            supported = (
                {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
                if sys.platform == "win32"
                else {"sh", "bash", "dash", "zsh", "ksh"}
            )
            if name not in supported:
                raise ValueError("unsupported shell executable")

    def _argv(self, command: str, env: Mapping[str, str]) -> tuple[str, ...]:
        executable = self.executable
        if executable is None:
            if sys.platform == "win32":
                from ._owned_command_windows import system_powershell

                executable = system_powershell()
            else:
                executable = "/bin/sh"
        resolved = shutil.which(executable, path=env.get("PATH", os.defpath))
        if resolved is None:
            raise FileNotFoundError(f"configured shell is unavailable: {executable}")
        if sys.platform == "win32":
            # Windows PowerShell serializes automatic module-load progress to
            # stderr even with text output. Suppress progress, not error records.
            command = "$ProgressPreference='SilentlyContinue'\n& {\n" + command + "\n}"
            return (
                resolved,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            )
        return (resolved, "-c", command)


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One command source, existing absolute working directory, and deadline."""

    command: str
    cwd: Path
    timeout_s: float

    def __post_init__(self) -> None:
        if not self.command or "\0" in self.command:
            raise ValueError("command must be nonempty and cannot contain NUL")
        if not self.cwd.is_absolute() or not self.cwd.is_dir():
            raise ValueError("command cwd must be an existing absolute directory")
        if not math.isfinite(self.timeout_s) or not 0 < self.timeout_s <= 300:
            raise ValueError("command timeout must be finite and in (0, 300] seconds")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Settled root exit and independently bounded raw output streams."""

    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    exit_code: int
    timed_out: bool


class CommandCancelled(asyncio.CancelledError):
    """Cancellation after settlement, retaining captured command effects."""

    def __init__(self, result: CommandResult) -> None:
        super().__init__("command cancelled after process settlement")
        self.result = result


class _Process(Protocol):
    def read_stdout(self, max_bytes: int) -> bytes | None: ...
    def read_stderr(self, max_bytes: int) -> bytes | None: ...
    def poll(self) -> int | None: ...
    def stop(self) -> None: ...
    def tree_stopped(self) -> bool: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class _Capture:
    limit: int
    data: bytearray
    truncated: bool = False
    eof: bool = False

    def accept(self, chunk: bytes | None) -> None:
        if chunk is None:
            return
        if not chunk:
            self.eof = True
            return
        available = max(0, self.limit - len(self.data))
        self.data.extend(chunk[:available])
        self.truncated |= len(chunk) > available


@dataclass(frozen=True, slots=True)
class _Active:
    stop: threading.Event
    task: asyncio.Future[CommandResult]


class OwnedCommands:
    """Serialize commands and settle owned children before cancellation returns.

    The worker owns spawn, finite pipe reads and cleanup without async gaps.
    Shielding its future keeps event-loop cancellation from detaching ownership.
    Commands run with the caller's OS identity; this is not a sandbox.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        shell: ShellSpec | None = None,
        output_limit_bytes: int = 1_000_000,
    ) -> None:
        if (
            not isinstance(output_limit_bytes, int)
            or isinstance(output_limit_bytes, bool)
            or output_limit_bytes <= 0
        ):
            raise ValueError("output limit must be a positive integer")
        self._env = dict(env)
        self._shell = shell or ShellSpec()
        self._limit = output_limit_bytes
        self._active: _Active | None = None
        self._closed = False
        self._failure: Exception | None = None

    async def execute(self, spec: CommandSpec) -> CommandResult:
        """Run one command, retaining its result even when cancellation wins."""
        if self._closed:
            raise RuntimeError("command owner is closed")
        if self._active is not None:
            raise RuntimeError("command owner already has an active command")
        stop = threading.Event()
        task = asyncio.get_running_loop().run_in_executor(None, self._run, spec, stop)
        active = _Active(stop, task)
        self._active = active
        try:
            result = await self._settle(active)
            if stop.is_set():
                raise CommandCancelled(result)
            return result
        except Exception as error:
            # A failed native operation cannot prove that ownership settled.
            # Retire admission and preserve that failure for shutdown reporting.
            self._closed = True
            self._failure = error
            raise
        finally:
            self._active = None

    async def cancel(self) -> None:
        """Stop and await the current command without discarding its result."""
        active = self._active
        if active is not None:
            active.stop.set()
            await self._settle(active)

    async def close(self) -> None:
        """Permanently close admission and settle any active command."""
        self._closed = True
        await self.cancel()
        if self._failure is not None:
            raise self._failure

    @staticmethod
    async def _settle(active: _Active) -> CommandResult:
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(active.task)
                break
            except asyncio.CancelledError:
                # Repeated cancellation cannot detach the worker while it owns
                # a spawn or cleanup. Preserve the result for the caller.
                cancelled = True
                active.stop.set()
        if cancelled:
            raise CommandCancelled(result)
        return result

    def _run(self, spec: CommandSpec, stop: threading.Event) -> CommandResult:
        argv = self._shell._argv(spec.command, self._env)
        process: _Process
        if sys.platform == "win32":
            from ._owned_command_windows import WindowsCommand

            process = WindowsCommand(argv, spec.cwd, self._env)
        elif sys.platform == "linux":
            from ._owned_command_linux import LinuxCommand

            process = LinuxCommand(argv, spec.cwd, self._env)
        else:
            from ._owned_command_posix import PosixCommand

            process = PosixCommand(argv, spec.cwd, self._env)
        stdout = _Capture(self._limit, bytearray())
        stderr = _Capture(self._limit, bytearray())
        deadline = time.monotonic() + spec.timeout_s
        timed_out = False
        stopped = False
        pipe_deadline: float | None = None
        try:
            while True:
                if not stdout.eof:
                    stdout.accept(process.read_stdout(65536))
                if not stderr.eof:
                    stderr.accept(process.read_stderr(65536))
                code = process.poll()
                if not stopped:
                    timed_out = (
                        code is None
                        and not stop.is_set()
                        and time.monotonic() >= deadline
                    )
                    if code is not None or stop.is_set() or timed_out:
                        # Root exit does not end ownership: it can leave live
                        # descendants holding pipes or continuing effects.
                        process.stop()
                        stopped = True
                if stopped:
                    process.stop()
                if stopped and process.tree_stopped():
                    if stdout.eof and stderr.eof:
                        assert code is not None
                        return CommandResult(
                            bytes(stdout.data),
                            bytes(stderr.data),
                            stdout.truncated,
                            stderr.truncated,
                            code,
                            timed_out,
                        )
                    if pipe_deadline is None:
                        pipe_deadline = time.monotonic() + 5.0
                    elif time.monotonic() >= pipe_deadline:
                        raise RuntimeError(
                            "command pipes remained open after the owned process tree settled"
                        )
                time.sleep(0.005)
        finally:
            original_failure = sys.exception()
            try:
                process.stop()
                while not process.tree_stopped():
                    process.stop()
                    time.sleep(0.005)
                process.close()
            except Exception as cleanup_error:
                if original_failure is not None:
                    raise BaseExceptionGroup(
                        "command execution and ownership cleanup failed",
                        [original_failure, cleanup_error],
                    ) from original_failure
                raise
