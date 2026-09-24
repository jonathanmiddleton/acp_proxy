"""Private Linux child subreaper; executed as a script, never in the service.

The helper is the sole waiter for this command's group. Root exit is cached
before reaping; adopted children then pin the group until exact reaping proves
ECHILD. Closing the parent's control pipe transfers cleanup to this helper.
"""

import ctypes
from dataclasses import dataclass
import errno
import os
import select
import signal
import struct
import subprocess
import sys
import time


def _prepare() -> None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "command subreaper requires Linux")
    else:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    native = ctypes.CDLL(None, use_errno=True).prctl
    native.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    native.restype = ctypes.c_int
    if native(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "command subreaper setup failed")


@dataclass(frozen=True, slots=True)
class _ChildExit:
    pid: int
    code: int


def _observe(pid: int, *, group: bool = False) -> _ChildExit | None:
    if sys.platform != "linux":
        raise OSError(errno.ENOTSUP, "command observation requires Linux")
    else:
        info = os.waitid(
            os.P_PGID if group else os.P_PID,
            pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT | 0x40000000,
        )
        if info is None:
            return None
        if info.si_code == 1:
            return _ChildExit(info.si_pid, info.si_status)
        if info.si_code in (2, 3):
            return _ChildExit(info.si_pid, -info.si_status)
        raise OSError(errno.EPROTO, "unexpected child exit observation")


def _reap(pid: int) -> None:
    observed, _status = os.waitpid(pid, 0x40000000)
    if observed != pid:
        raise OSError(errno.EPROTO, "reaped an unexpected process")


class _Invocation:
    def __init__(self, command: tuple[str, ...], status: int) -> None:
        self._root = subprocess.Popen(command, start_new_session=True)
        self._status = status
        self._code: int | None = None
        self._root_reaped = False
        self._settled = False

    def observe(self) -> int | None:
        if self._code is None:
            info = _observe(self._root.pid)
            if info is not None:
                self._code = info.code
                # Fixed bounded message; the parent continuously reads this pipe.
                try:
                    os.write(self._status, struct.pack("!i", self._code))
                except BrokenPipeError:
                    # The owner has exited; the guardian still settles its tree.
                    pass
        return self._code

    def settled(self) -> bool:
        if self._settled:
            return True
        if self.observe() is None:
            return False
        if not self._root_reaped:
            _reap(self._root.pid)
            self._root.returncode = self._code
            self._root_reaped = True
        for _ in range(64):
            try:
                info = _observe(self._root.pid, group=True)
            except ChildProcessError:
                # Adoption completes before root exit becomes waitable. With
                # this helper as sole waiter, ECHILD proves group settlement.
                self._settled = True
                return True
            if info is None:
                return False
            _reap(info.pid)
        return False

    def stop(self) -> None:
        if self.settled():
            return
        # No child is consumed between this observation and signaling. An
        # observed child pins the numeric group; ECHILD cannot target reuse.
        _observe(self._root.pid, group=True)
        try:
            if sys.platform != "win32":
                os.killpg(self._root.pid, signal.SIGKILL)
            else:
                raise OSError(errno.ENOTSUP, "process-group signaling requires Linux")
        except ProcessLookupError:
            # The final unreaped member can already be a zombie.
            pass

    def cleanup(self) -> None:
        while not self.settled():
            self.stop()
            time.sleep(0.005)


def main() -> None:
    """Own one command and stop it on control data or owner pipe closure."""
    if len(sys.argv) < 4:
        raise ValueError("invalid command guardian invocation")
    control, status = int(sys.argv[1]), int(sys.argv[2])
    os.set_inheritable(control, False)
    os.set_inheritable(status, False)
    _prepare()
    invocation = _Invocation(tuple(sys.argv[3:]), status)
    try:
        while invocation.observe() is None:
            readable, _writeable, _exceptional = select.select([control], [], [], 0.005)
            if readable:
                os.read(control, 1)  # A stop byte or EOF both require settlement.
                break
    finally:
        invocation.cleanup()
        os.close(control)
        os.close(status)


if __name__ == "__main__":
    main()
