"""Synchronous Windows command ownership for the command owner's worker thread."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as W
import errno
import ntpath
from pathlib import Path
import subprocess
import sys
import time
from collections.abc import Mapping
import uuid


_PIPE_CAPACITY = 65536
_CLEANUP_SECONDS = 5.0
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_ERROR_BROKEN_PIPE = 109
_ERROR_NO_DATA = 232
_ERROR_PIPE_CONNECTED = 535


class _Security(ctypes.Structure):
    _fields_ = [("size", W.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", W.BOOL)]


class _Startup(ctypes.Structure):
    size: int
    flags: int
    stdin: int | None
    stdout: int | None
    stderr: int | None
    _fields_ = [
        ("size", W.DWORD),
        ("reserved", W.LPWSTR),
        ("desktop", W.LPWSTR),
        ("title", W.LPWSTR),
        ("x", W.DWORD),
        ("y", W.DWORD),
        ("xsize", W.DWORD),
        ("ysize", W.DWORD),
        ("xchars", W.DWORD),
        ("ychars", W.DWORD),
        ("fill", W.DWORD),
        ("flags", W.DWORD),
        ("show", W.WORD),
        ("reserved_size", W.WORD),
        ("reserved_bytes", ctypes.POINTER(ctypes.c_ubyte)),
        ("stdin", W.HANDLE),
        ("stdout", W.HANDLE),
        ("stderr", W.HANDLE),
    ]


class _StartupEx(ctypes.Structure):
    startup: _Startup
    attributes: int | None
    _fields_ = [("startup", _Startup), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    process: int | None
    thread: int | None
    _fields_ = [
        ("process", W.HANDLE),
        ("thread", W.HANDLE),
        ("pid", W.DWORD),
        ("tid", W.DWORD),
    ]


class _BasicLimits(ctypes.Structure):
    flags: int
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", W.DWORD),
        ("min_set", ctypes.c_size_t),
        ("max_set", ctypes.c_size_t),
        ("active_limit", W.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", W.DWORD),
        ("scheduling", W.DWORD),
    ]


class _Counters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _Limits(ctypes.Structure):
    basic: _BasicLimits
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", _Counters),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    active: int
    _fields_ = [
        ("user", ctypes.c_longlong),
        ("kernel", ctypes.c_longlong),
        ("period_user", ctypes.c_longlong),
        ("period_kernel", ctypes.c_longlong),
        ("faults", W.DWORD),
        ("total", W.DWORD),
        ("active", W.DWORD),
        ("terminated", W.DWORD),
    ]


_kernel: ctypes.CDLL
if sys.platform == "win32":
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
    _kernel.CreateJobObjectW.restype = W.HANDLE
    _kernel.SetInformationJobObject.argtypes = [
        W.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        W.DWORD,
    ]
    _kernel.SetInformationJobObject.restype = W.BOOL
    _kernel.QueryInformationJobObject.argtypes = [
        W.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        W.DWORD,
        ctypes.c_void_p,
    ]
    _kernel.QueryInformationJobObject.restype = W.BOOL
    _kernel.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
    _kernel.TerminateJobObject.restype = W.BOOL
    _kernel.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
    _kernel.GetExitCodeProcess.restype = W.BOOL
    _kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    _kernel.WaitForSingleObject.restype = W.DWORD
    _kernel.ResumeThread.argtypes = [W.HANDLE]
    _kernel.ResumeThread.restype = W.DWORD
    _kernel.CloseHandle.argtypes = [W.HANDLE]
    _kernel.CloseHandle.restype = W.BOOL
    _kernel.CreateFileW.argtypes = [
        W.LPCWSTR,
        W.DWORD,
        W.DWORD,
        ctypes.POINTER(_Security),
        W.DWORD,
        W.DWORD,
        W.HANDLE,
    ]
    _kernel.CreateFileW.restype = W.HANDLE
    _kernel.CreateNamedPipeW.argtypes = [
        W.LPCWSTR,
        W.DWORD,
        W.DWORD,
        W.DWORD,
        W.DWORD,
        W.DWORD,
        W.DWORD,
        ctypes.c_void_p,
    ]
    _kernel.CreateNamedPipeW.restype = W.HANDLE
    _kernel.ConnectNamedPipe.argtypes = [W.HANDLE, ctypes.c_void_p]
    _kernel.ConnectNamedPipe.restype = W.BOOL
    _kernel.ReadFile.argtypes = [
        W.HANDLE,
        ctypes.c_void_p,
        W.DWORD,
        ctypes.POINTER(W.DWORD),
        ctypes.c_void_p,
    ]
    _kernel.ReadFile.restype = W.BOOL
    _kernel.WriteFile.argtypes = _kernel.ReadFile.argtypes
    _kernel.WriteFile.restype = W.BOOL
    _kernel.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        W.DWORD,
        W.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel.InitializeProcThreadAttributeList.restype = W.BOOL
    _kernel.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        W.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _kernel.UpdateProcThreadAttribute.restype = W.BOOL
    _kernel.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _kernel.DeleteProcThreadAttributeList.restype = None
    _kernel.CreateProcessW.argtypes = [
        W.LPCWSTR,
        W.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        W.BOOL,
        W.DWORD,
        ctypes.c_void_p,
        W.LPCWSTR,
        ctypes.POINTER(_StartupEx),
        ctypes.POINTER(_ProcessInfo),
    ]
    _kernel.CreateProcessW.restype = W.BOOL
    _kernel.GetSystemDirectoryW.argtypes = [W.LPWSTR, W.UINT]
    _kernel.GetSystemDirectoryW.restype = W.UINT


def _windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows command operations require Windows")


def _last_error() -> OSError:
    if sys.platform == "win32":
        return ctypes.WinError(ctypes.get_last_error())
    raise RuntimeError("Windows command operations require Windows")


def _error_code(error: OSError) -> int:
    if sys.platform == "win32":
        return error.winerror
    raise RuntimeError("Windows command operations require Windows")


def _integer(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError("Windows returned a non-integer result")
    return value


def _check(value: object) -> None:
    if _integer(value) == 0:
        raise _last_error()


def _handle(value: object) -> int:
    if value is None or value == 0 or value == ctypes.c_void_p(-1).value:
        raise _last_error()
    return _integer(value)


def _close_handle(handle: int) -> None:
    _check(_kernel.CloseHandle(handle))


def _release_handle(handle: int | None, failures: list[Exception]) -> int | None:
    if handle is None:
        return None
    try:
        _close_handle(handle)
    except OSError as failure:
        failures.append(failure)
        return handle
    return None


def _environment(env: Mapping[str, str]) -> str:
    entries: dict[str, tuple[str, str]] = {}
    for key, value in env.items():
        if not key or "\0" in key or "=" in key or "\0" in value:
            raise ValueError("command environment contains an invalid entry")
        folded = key.upper()
        if folded in entries:
            raise ValueError(
                "command environment contains case-insensitive duplicate keys"
            )
        entries[folded] = (key, value)
    return (
        "\0".join(f"{key}={value}" for _, (key, value) in sorted(entries.items()))
        + "\0\0"
    )


def system_powershell() -> str:
    """Resolve Windows PowerShell 5.1 from the native system directory."""
    _windows()
    size = 260
    while True:
        directory = ctypes.create_unicode_buffer(size)
        count = _integer(_kernel.GetSystemDirectoryW(directory, size))
        if count == 0:
            raise _last_error()
        if count < size:
            observed: object = directory.value
            if not isinstance(observed, str):
                raise TypeError("Windows system directory did not return text")
            return ntpath.join(observed, "WindowsPowerShell", "v1.0", "powershell.exe")
        size = count + 1


class WindowsCommand:
    """Own a process tree in a non-breakaway Job and two finite output pipes.

    All methods run on one owning worker thread. A root exit leaves the Job
    alive; callers stop and settle its descendants before releasing ownership.
    """

    def __init__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        *,
        pipe_input: bool = False,
    ) -> None:
        _windows()
        if not argv or not argv[0] or any("\0" in word for word in argv):
            raise ValueError("command argv must contain an executable and no NUL bytes")
        if "\0" in str(cwd):
            raise ValueError("command cwd contains a NUL byte")
        environment = _environment(env)
        self._job: int | None = None
        self._process: int | None = None
        self._thread: int | None = None
        self._stdin: int | None = None
        self._stdout: int | None = None
        self._stderr: int | None = None
        self._children: list[int] = []
        try:
            self._spawn(argv, cwd, environment, pipe_input=pipe_input)
        except BaseException as failure:
            try:
                self._cleanup_failed_spawn()
            except BaseException as cleanup_failure:
                raise BaseExceptionGroup(
                    "Windows command spawn failed and cleanup did not settle",
                    [failure, cleanup_failure],
                ) from failure
            raise

    def _spawn(
        self, argv: tuple[str, ...], cwd: Path, environment: str, *, pipe_input: bool
    ) -> None:
        job = _handle(_kernel.CreateJobObjectW(None, None))
        self._job = job
        limits = _Limits()
        # Neither BREAKAWAY_OK nor SILENT_BREAKAWAY_OK is enabled.
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(
            _kernel.SetInformationJobObject(
                job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            )
        )
        if pipe_input:
            self._stdin, stdin = self._pipe(parent_writes=True)
        else:
            security = _Security(ctypes.sizeof(_Security), None, True)
            stdin = _handle(
                _kernel.CreateFileW(
                    "NUL", 0x80000000, 3, ctypes.byref(security), 3, 0, None
                )
            )
            self._children.append(stdin)
        self._stdout, stdout = self._pipe(parent_writes=False)
        self._stderr, stderr = self._pipe(parent_writes=False)
        startup = _StartupEx()
        startup.startup.size = ctypes.sizeof(startup)
        startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
        startup.startup.stdin = stdin
        startup.startup.stdout = stdout
        startup.startup.stderr = stderr
        size = ctypes.c_size_t()
        _kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if size.value == 0:
            raise _last_error()
        attributes = ctypes.create_string_buffer(size.value)
        _check(
            _kernel.InitializeProcThreadAttributeList(
                attributes, 2, 0, ctypes.byref(size)
            )
        )
        try:
            startup.attributes = ctypes.addressof(attributes)
            inherited = (W.HANDLE * len(self._children))(*self._children)
            _check(
                _kernel.UpdateProcThreadAttribute(
                    attributes,
                    0,
                    0x20002,
                    inherited,
                    ctypes.sizeof(inherited),
                    None,
                    None,
                )
            )  # PROC_THREAD_ATTRIBUTE_HANDLE_LIST
            jobs = (W.HANDLE * 1)(job)
            # Atomic assignment closes the owner-death gap before ResumeThread.
            # Unsupported Windows versions fail here, before any child exists.
            _check(
                _kernel.UpdateProcThreadAttribute(
                    attributes, 0, 0x2000D, jobs, ctypes.sizeof(jobs), None, None
                )
            )  # PROC_THREAD_ATTRIBUTE_JOB_LIST
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            environment_buffer = ctypes.create_unicode_buffer(environment)
            info = _ProcessInfo()
            _check(
                _kernel.CreateProcessW(
                    argv[0],
                    command,
                    None,
                    None,
                    True,
                    0x08080404,  # NO_WINDOW | EXTENDED_STARTUPINFO | UNICODE_ENVIRONMENT | SUSPENDED
                    environment_buffer,
                    str(cwd),
                    ctypes.byref(startup),
                    ctypes.byref(info),
                )
            )
            self._process = _handle(info.process)
            self._thread = _handle(info.thread)
        finally:
            _kernel.DeleteProcThreadAttributeList(attributes)
        while self._children:
            _close_handle(self._children[-1])
            self._children.pop()
        thread = self._thread
        if thread is None:
            raise RuntimeError("Windows did not return the suspended process thread")
        suspend_count = _integer(_kernel.ResumeThread(thread))
        if suspend_count == 0xFFFFFFFF:
            raise _last_error()
        if suspend_count != 1:
            raise RuntimeError(
                "Windows returned an unexpected initial thread suspend count"
            )
        _close_handle(thread)
        self._thread = None

    def _pipe(self, *, parent_writes: bool) -> tuple[int, int]:
        name = "\\\\.\\pipe\\MeadowBridgeCommand" + uuid.uuid4().hex
        # FIRST_PIPE_INSTANCE plus a fresh name refuses an existing endpoint;
        # PIPE_NOWAIT keeps parent I/O finite, with local clients only. The
        # child opens its endpoint in the default blocking mode.
        parent = _handle(
            _kernel.CreateNamedPipeW(
                name,
                0x80002 if parent_writes else 0x80001,
                0x9,
                1,
                _PIPE_CAPACITY,
                _PIPE_CAPACITY,
                0,
                None,
            )
        )
        try:
            security = _Security(ctypes.sizeof(_Security), None, True)
            child = _handle(
                _kernel.CreateFileW(
                    name,
                    0x80000000 if parent_writes else 0x40000000,
                    0,
                    ctypes.byref(security),
                    3,
                    0,
                    None,
                )
            )
            self._children.append(child)
            if _integer(_kernel.ConnectNamedPipe(parent, None)) == 0:
                error = _last_error()
                if _error_code(error) != _ERROR_PIPE_CONNECTED:
                    raise error
            return parent, child
        except BaseException as failure:
            try:
                _close_handle(parent)
            except OSError as cleanup_failure:
                self._children.append(parent)
                failure.add_note(f"Windows pipe cleanup failed: {cleanup_failure}")
            raise

    def _read(self, handle: int | None, max_bytes: int) -> bytes | None:
        if handle is None:
            raise ValueError("Windows command output has been closed")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        capacity = min(max_bytes, _PIPE_CAPACITY)
        buffer = ctypes.create_string_buffer(capacity)
        count = W.DWORD()
        if (
            _integer(
                _kernel.ReadFile(handle, buffer, capacity, ctypes.byref(count), None)
            )
            == 0
        ):
            error = _last_error()
            if _error_code(error) == _ERROR_NO_DATA:
                return None
            if _error_code(error) == _ERROR_BROKEN_PIPE:
                return b""
            raise error
        return buffer.raw[: count.value]

    def read_stdout(self, max_bytes: int) -> bytes | None:
        """Read at most max_bytes; None means pending, and empty bytes mean EOF."""
        return self._read(self._stdout, max_bytes)

    def read_stderr(self, max_bytes: int) -> bytes | None:
        """Read at most max_bytes; None means pending, and empty bytes mean EOF."""
        return self._read(self._stderr, max_bytes)

    def write_stdin(self, data: bytes) -> int | None:
        """Write at most 65,536 bytes; None means the live pipe made no progress."""
        if self._stdin is None:
            raise ValueError("Windows command stdin is not a pipe or has been closed")
        if not data:
            return 0
        pending = data[:_PIPE_CAPACITY]
        buffer = ctypes.create_string_buffer(pending)
        count = W.DWORD()
        _check(
            _kernel.WriteFile(
                self._stdin, buffer, len(pending), ctypes.byref(count), None
            )
        )
        # A full PIPE_NOWAIT byte pipe succeeds with zero bytes written.
        # ERROR_NO_DATA instead means its reader has closed and must surface.
        return count.value if count.value else None

    def close_stdin(self) -> None:
        """Close the parent writer to deliver EOF; repeated calls are harmless."""
        if self._stdin is not None:
            _close_handle(self._stdin)
            self._stdin = None

    def poll(self) -> int | None:
        """Return the root's exit status once its handle is signaled."""
        if self._process is None:
            raise ValueError("Windows command process has been closed")
        status = _integer(_kernel.WaitForSingleObject(self._process, 0))
        if status == _WAIT_TIMEOUT:
            return None
        if status != _WAIT_OBJECT_0:
            raise _last_error()
        code = W.DWORD()
        _check(_kernel.GetExitCodeProcess(self._process, ctypes.byref(code)))
        return code.value

    def stop(self) -> None:
        """Request forceful termination of the whole Job, including descendants."""
        if self._job is not None and not self.tree_stopped():
            _check(_kernel.TerminateJobObject(self._job, 1))

    def tree_stopped(self) -> bool:
        """Prove that the Job is empty and the root process is signaled."""
        if self._job is None:
            return self._process is None
        accounting = _Accounting()
        _check(
            _kernel.QueryInformationJobObject(
                self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
            )
        )
        return accounting.active == 0 and (
            self._process is None or self.poll() is not None
        )

    def close(self) -> None:
        """Release a settled process tree; retain live resources for owner cleanup."""
        if not self.tree_stopped():
            raise OSError(errno.EBUSY, "Windows command process tree is still running")
        self._close_all()

    def _cleanup_failed_spawn(self) -> None:
        failures: list[BaseException] = []
        try:
            self.stop()
            deadline = time.monotonic() + _CLEANUP_SECONDS
            while not self.tree_stopped():
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Windows command spawn cleanup did not settle within five seconds"
                    )
                time.sleep(0.01)
        except BaseException as failure:
            failures.append(failure)
        try:
            # Closing the final Job handle also requests termination if explicit
            # settlement failed. The original constructor failure remains fatal.
            self._close_all()
        except BaseException as failure:
            failures.append(failure)
        if failures:
            raise BaseExceptionGroup("Windows command spawn cleanup failed", failures)

    def _close_all(self) -> None:
        failures: list[Exception] = []
        self._stdin = _release_handle(self._stdin, failures)
        self._stdout = _release_handle(self._stdout, failures)
        self._stderr = _release_handle(self._stderr, failures)
        self._thread = _release_handle(self._thread, failures)
        self._process = _release_handle(self._process, failures)
        self._job = _release_handle(self._job, failures)
        remaining: list[int] = []
        for handle in self._children:
            try:
                _close_handle(handle)
            except OSError as failure:
                failures.append(failure)
                remaining.append(handle)
        self._children = remaining
        if failures:
            raise ExceptionGroup("Windows command handle cleanup failed", failures)
