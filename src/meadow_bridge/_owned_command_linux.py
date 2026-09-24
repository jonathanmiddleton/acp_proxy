"""Parent endpoint for the private Linux command subreaper."""

from collections.abc import Mapping
import errno
import os
from pathlib import Path
import struct
import subprocess
import sys
from typing import NoReturn


class LinuxCommand:
    """Own a private subreaper and its command's pipes through settlement."""

    def __init__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        *,
        pipe_input: bool = False,
    ) -> None:
        control_read, control_write = os.pipe()
        try:
            status_read, status_write = os.pipe()
        except BaseException:
            os.close(control_read)
            os.close(control_write)
            raise
        try:
            self._process = subprocess.Popen(
                (
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("_owned_command_linux_guardian.py")),
                    str(control_read),
                    str(status_write),
                    *argv,
                ),
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if pipe_input else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(control_read, status_write),
            )
        except BaseException:
            os.close(control_write)
            os.close(status_read)
            raise
        finally:
            os.close(control_read)
            os.close(status_write)
        self._control: int | None = control_write
        self._status = status_read
        self._status_bytes = bytearray()
        self._code: int | None = None
        self._closed = False
        self._failure: OSError | None = None
        stdout, stderr = self._process.stdout, self._process.stderr
        assert stdout is not None and stderr is not None
        self._stdout, self._stderr = stdout, stderr
        self._stdin = self._process.stdin
        try:
            descriptors = [stdout.fileno(), stderr.fileno(), status_read]
            if self._stdin is not None:
                descriptors.append(self._stdin.fileno())
            for descriptor in descriptors:
                if sys.platform != "win32":
                    os.set_blocking(descriptor, False)
                else:
                    raise OSError(errno.ENOTSUP, "command guardian requires Linux")
        except BaseException:
            self.stop()
            self._process.wait()
            self.close_stdin()
            stdout.close()
            stderr.close()
            os.close(status_read)
            raise

    @staticmethod
    def _read(descriptor: int, limit: int) -> bytes | None:
        try:
            return os.read(descriptor, limit)
        except BlockingIOError:
            # The pipe is live but currently empty.
            return None

    def read_stdout(self, max_bytes: int) -> bytes | None:
        """Read output without blocking command control or stderr drainage."""
        return self._read(self._stdout.fileno(), max_bytes)

    def read_stderr(self, max_bytes: int) -> bytes | None:
        """Read errors without blocking command control or stdout drainage."""
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
        """Close the parent's writer, delivering EOF to the command root."""
        if self._stdin is not None:
            self._stdin.close()
            self._stdin = None

    def poll(self) -> int | None:
        """Return the actual command root exit reported by its sole waiter."""
        if self._failure is not None:
            raise self._failure
        if self._code is not None:
            return self._code
        chunk = self._read(self._status, 4 - len(self._status_bytes))
        if chunk is not None:
            self._status_bytes.extend(chunk)
            if len(self._status_bytes) == 4:
                value: object = struct.unpack("!i", self._status_bytes)[0]
                if not isinstance(value, int):
                    raise TypeError("command guardian returned an invalid exit code")
                self._code = value
            elif not chunk:
                self._lost_guardian(
                    "command guardian exited without root settlement evidence"
                )
        return self._code

    def stop(self) -> None:
        """Transfer settlement to the guardian by closing its owner lifeline."""
        if self._control is not None:
            descriptor, self._control = self._control, None
            os.close(descriptor)

    def tree_stopped(self) -> bool:
        """Require successful guardian exit and its separately reported root code."""
        if self._failure is not None:
            raise self._failure
        code = self.poll()
        guardian_code = self._process.poll()
        if guardian_code is None:
            return False
        if guardian_code != 0:
            self._lost_guardian("command guardian failed before tree settlement")
        return code is not None

    def close(self) -> None:
        """Close pipes only after the guardian has proved descendant settlement."""
        if self._closed:
            return
        if not self.tree_stopped():
            raise OSError(errno.EBUSY, "command guardian has not settled")
        self.stop()
        self.close_stdin()
        self._stdout.close()
        self._stderr.close()
        os.close(self._status)
        self._closed = True

    def _lost_guardian(self, message: str) -> NoReturn:
        # The guardian is the sole child waiter. Its failure removes our group
        # authority; no numeric PID signal can safely reconstruct it. Release
        # parent-owned descriptors, retain the failure, and never claim success.
        self._failure = OSError(errno.ECHILD, message)
        self.stop()
        self.close_stdin()
        self._stdout.close()
        self._stderr.close()
        os.close(self._status)
        self._closed = True
        self._process.wait()
        raise self._failure
