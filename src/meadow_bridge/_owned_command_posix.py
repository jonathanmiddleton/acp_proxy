"""Darwin process-group ownership with an unreaped root pinning its identity.

A process group covers descendants that remain in the group. Deliberate session
or group changes are not contained; this adapter is not an OS sandbox.
"""

from collections.abc import Mapping
import ctypes
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import IO


class _Siginfo(ctypes.Structure):
    _fields_ = [
        ("signo", ctypes.c_int),
        ("error", ctypes.c_int),
        ("code", ctypes.c_int),
        ("pid", ctypes.c_int),
        ("uid", ctypes.c_uint),
        ("status", ctypes.c_int),
        ("address", ctypes.c_void_p),
        ("value", ctypes.c_void_p),
        ("band", ctypes.c_long),
        ("padding", ctypes.c_ulong * 7),
    ]
    pid: int
    code: int
    status: int


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError("native process operation did not return an integer")
    return value


def _observe(pid: int) -> int | None:
    native = ctypes.CDLL(None, use_errno=True).waitid
    native.argtypes = [
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(_Siginfo),
        ctypes.c_int,
    ]
    native.restype = ctypes.c_int
    info = _Siginfo()
    # Darwin P_PID, WNOHANG | WEXITED | WNOWAIT. Python does not expose waitid
    # on Darwin; retaining the child prevents reuse of its PID and group ID.
    if _integer(native(1, pid, ctypes.byref(info), 0x25)) != 0:
        raise OSError(ctypes.get_errno(), "retained-child observation failed")
    if info.pid == 0 or info.code == 5:  # Unconsumed stop is not an exit.
        return None
    if info.pid != pid:
        raise RuntimeError("retained-child observation returned another child")
    if info.code == 1:
        return info.status
    if info.code in (2, 3):
        return -info.status
    raise OSError(errno.EPROTO, "unexpected retained-child observation")


def _other_members(pid: int) -> bool:
    native = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_listpids
    native.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    native.restype = ctypes.c_int
    capacity = 32
    width = ctypes.sizeof(ctypes.c_int)
    while True:
        values = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        size = _integer(native(2, pid, values, ctypes.sizeof(values)))  # PROC_PGRP_ONLY
        error = ctypes.get_errno()
        if size < 0 or (size == 0 and error):
            raise OSError(error or errno.EIO, "process-group census failed")
        if size % width or size > ctypes.sizeof(values):
            raise OSError(errno.EPROTO, "invalid process-group census result")
        count = size // width
        if any(values[index] != pid for index in range(count)):
            return True
        if count < capacity:
            return False
        capacity *= 2


class PosixCommand:
    """Own a Darwin command's process group until every member has settled."""

    _process: subprocess.Popen[bytes]
    _stdout: IO[bytes]
    _stderr: IO[bytes]
    _stdin: IO[bytes] | None
    _closed: bool
    _force_delivered: bool

    def __init__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        *,
        pipe_input: bool = False,
    ) -> None:
        if sys.platform != "darwin":
            raise OSError(
                errno.ENOTSUP, "owned POSIX commands currently require Darwin"
            )
        self._process = subprocess.Popen[bytes](
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if pipe_input else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._closed = False
        self._force_delivered = False
        self._returncode: int | None = None
        stdout, stderr = self._process.stdout, self._process.stderr
        assert stdout is not None and stderr is not None
        self._stdout = stdout
        self._stderr = stderr
        self._stdin = self._process.stdin
        try:
            if sys.platform != "win32":
                os.set_blocking(stdout.fileno(), False)
                os.set_blocking(stderr.fileno(), False)
                if self._stdin is not None:
                    os.set_blocking(self._stdin.fileno(), False)
            else:
                raise OSError(errno.ENOTSUP, "nonblocking process pipes require POSIX")
        except BaseException:
            self.stop()
            while not self.tree_stopped():
                time.sleep(0.01)
            self.close()
            raise

    def read_stdout(self, max_bytes: int) -> bytes | None:
        """Read immediately available bytes, or None while the pipe is empty."""
        return self._read(self._stdout.fileno(), max_bytes)

    def read_stderr(self, max_bytes: int) -> bytes | None:
        """Read immediately available bytes, or None while the pipe is empty."""
        return self._read(self._stderr.fileno(), max_bytes)

    def write_stdin(self, data: bytes) -> int | None:
        """Write finite input; None means pipe capacity is currently exhausted."""
        if self._stdin is None:
            raise ValueError("command input is not an open pipe")
        try:
            return os.write(self._stdin.fileno(), data[:65536])
        except BlockingIOError:
            # The caller retains the unwritten suffix for a later attempt.
            return None

    def close_stdin(self) -> None:
        """Close the parent's writer, delivering EOF to the child."""
        if self._stdin is not None:
            self._stdin.close()
            self._stdin = None

    @staticmethod
    def _read(fd: int, max_bytes: int) -> bytes | None:
        try:
            return os.read(fd, max_bytes)
        except BlockingIOError:
            # Nonblocking pipe has no bytes yet; ownership remains active.
            return None

    def poll(self) -> int | None:
        """Observe exit without consuming the root or releasing its identity."""
        if self._returncode is None:
            self._returncode = _observe(self._process.pid)
        return self._returncode

    def stop(self) -> None:
        """Signal the complete owned group, including an exited root's children."""
        if self._closed or self.tree_stopped():
            return
        _observe(self._process.pid)  # ECHILD fails closed before numeric signaling.
        try:
            if sys.platform != "win32":
                os.killpg(self._process.pid, signal.SIGKILL)
                self._force_delivered = True
            else:
                raise OSError(errno.ENOTSUP, "process-group signaling requires POSIX")
        except PermissionError:
            if not self._force_delivered:
                raise
            # Darwin may deny a repeated signal after successful force while
            # zombie descendants await reaping. Census must still prove empty.
        except ProcessLookupError:
            # The unreaped root can be the group's sole remaining zombie.
            pass

    def tree_stopped(self) -> bool:
        """Require root exit and an atomic census with no other group members."""
        return self.poll() is not None and not _other_members(self._process.pid)

    def close(self) -> None:
        """Reap and release only a settled process group."""
        if self._closed:
            return
        if not self.tree_stopped():
            raise OSError(errno.EBUSY, "command process group has not settled")
        self._process.wait()
        self.close_stdin()
        self._stdout.close()
        self._stderr.close()
        self._closed = True
