"""Discovery and version admission for ``copilot-language-server``.

Auto-discovery combines executable paths reported by running processes with a
recursive search below the platform's JetBrains data directory. IDE product,
release, plugin layout, and bundled architecture are discovery details, not
compatibility evidence. Every candidate is admitted by its own strict
``--version`` report before it can be selected or executed with credentials.

This module is the single source of truth for binary resolution. Both the CLI
entry point and the test suite import from here.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import signal
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass

from .application_policy import MIN_COPILOT_LANGUAGE_SERVER_VERSION

logger = logging.getLogger(__name__)

_VERSION_PATTERN = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
)
_VERSION_PROBE_TIMEOUT_S = 10
_VERSION_OUTPUT_LIMIT_BYTES = 256
_VERSION_PROBE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    }
)


class BinaryCompatibilityError(RuntimeError):
    """The selected language-server binary is not safely usable."""


@dataclass(frozen=True)
class BinaryAdmission:
    """Canonical executable path and strictly observed semantic version."""

    path: str
    version: tuple[int, int, int]


def _format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def parse_copilot_language_server_version(value: object) -> tuple[int, int, int]:
    """Parse the exact ``MAJOR.MINOR.PATCH`` emitted by ``--version``.

    Values with coercible-but-ambiguous Python types or decorated output are
    rejected. The binary admission boundary must never guess which version it
    is about to execute.
    """

    if not isinstance(value, str) or _VERSION_PATTERN.fullmatch(value) is None:
        raise BinaryCompatibilityError(
            "copilot-language-server version must be canonical MAJOR.MINOR.PATCH"
        )
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _version_probe_env(source: dict[str, str]) -> dict[str, str]:
    """Project a non-credential environment for the pre-admission executable."""

    return {
        key: value
        for key, value in source.items()
        if key.upper() in _VERSION_PROBE_ENV_KEYS or key.upper().startswith("LC_")
    }


def _terminate_probe_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the isolated probe tree and reap its group leader.

    POSIX process groups remain addressable after their leader exits, so
    ``killpg`` is deliberately unconditional. Windows has no ``killpg``
    equivalent; ``taskkill /T`` is the supported process-tree operation, with
    leader termination as a fail-safe when that utility is unavailable.
    """

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_version_probe_env(dict(os.environ)),
                timeout=1.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("Windows probe process-tree termination was unavailable")
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _read_bounded_version_output(
    binary_path: str,
    *,
    timeout_s: float = _VERSION_PROBE_TIMEOUT_S,
) -> bytes:
    """Execute ``--version`` while retaining at most 257 stdout bytes."""

    process_kwargs: dict[str, object] = {}
    if os.name == "nt":
        process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            [binary_path, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_version_probe_env(dict(os.environ)),
            **process_kwargs,
        )
    except OSError:
        raise BinaryCompatibilityError(
            "copilot-language-server version probe failed"
        ) from None
    if process.stdout is None:  # pragma: no cover - guaranteed by PIPE
        _terminate_probe_process_group(process)
        raise BinaryCompatibilityError(
            "copilot-language-server version probe failed"
        )

    output = bytearray()
    reader_done = threading.Event()
    reader_failed = threading.Event()

    def _read_stdout() -> None:
        try:
            while len(output) <= _VERSION_OUTPUT_LIMIT_BYTES:
                chunk = os.read(
                    process.stdout.fileno(),
                    (_VERSION_OUTPUT_LIMIT_BYTES + 1) - len(output),
                )
                if not chunk:
                    break
                output.extend(chunk)
        except OSError:
            reader_failed.set()
        finally:
            reader_done.set()

    reader = threading.Thread(
        target=_read_stdout,
        name="copilot-version-probe-reader",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + timeout_s
    try:
        if not reader_done.wait(timeout_s):
            _terminate_probe_process_group(process)
            raise BinaryCompatibilityError(
                "copilot-language-server version probe timed out"
            )
        if len(output) > _VERSION_OUTPUT_LIMIT_BYTES:
            _terminate_probe_process_group(process)
            raise BinaryCompatibilityError(
                "copilot-language-server version output exceeded the safety limit"
            )
        if reader_failed.is_set():
            _terminate_probe_process_group(process)
            raise BinaryCompatibilityError(
                "copilot-language-server version probe failed"
            )
        try:
            return_code = process.wait(timeout=max(deadline - time.monotonic(), 0.0))
        except subprocess.TimeoutExpired:
            _terminate_probe_process_group(process)
            raise BinaryCompatibilityError(
                "copilot-language-server version probe timed out"
            ) from None
        if return_code != 0:
            raise BinaryCompatibilityError(
                "copilot-language-server version probe failed"
            )
        return bytes(output)
    finally:
        process.stdout.close()
        _terminate_probe_process_group(process)
        reader.join(timeout=1.0)


def _probe_binary_version(binary_path: str) -> tuple[int, int, int]:
    """Read a binary's version without retaining unsanitized process output."""

    raw_output = _read_bounded_version_output(binary_path)
    if len(raw_output) > _VERSION_OUTPUT_LIMIT_BYTES:  # defensive boundary
        raise BinaryCompatibilityError(
            "copilot-language-server version output exceeded the safety limit"
        )
    try:
        decoded = raw_output.decode("ascii")
    except UnicodeDecodeError:
        raise BinaryCompatibilityError(
            "copilot-language-server version must be canonical MAJOR.MINOR.PATCH"
        ) from None
    if decoded.endswith("\r\n"):
        decoded = decoded[:-2]
    elif decoded.endswith("\n"):
        decoded = decoded[:-1]
    return parse_copilot_language_server_version(decoded)


def _inspect_binary(binary_path: str) -> BinaryAdmission:
    """Canonicalize and admit one executable language-server candidate."""

    try:
        canonical_path = os.path.realpath(os.path.abspath(binary_path))
    except (TypeError, ValueError):
        raise BinaryCompatibilityError(
            "copilot-language-server binary path is invalid"
        ) from None
    if not os.path.isfile(canonical_path) or not os.access(canonical_path, os.X_OK):
        raise BinaryCompatibilityError(
            "copilot-language-server binary must be an existing executable file"
        )
    version = _probe_binary_version(canonical_path)
    if version < MIN_COPILOT_LANGUAGE_SERVER_VERSION:
        raise BinaryCompatibilityError(
            "copilot-language-server version "
            f"{_format_version(version)} is below required minimum "
            f"{_format_version(MIN_COPILOT_LANGUAGE_SERVER_VERSION)}"
        )
    return BinaryAdmission(path=canonical_path, version=version)


def admit_compatible_binary(binary_path: str) -> BinaryAdmission:
    """Admit an explicit/programmatic binary with retained version evidence."""

    return _inspect_binary(binary_path)


def require_compatible_binary(binary_path: str) -> str:
    """Admit an explicit/programmatic binary and return its canonical path."""

    return admit_compatible_binary(binary_path).path


def _select_best_binary(
    binary_paths: Iterable[str],
    *,
    fail_if_candidates_rejected: bool = False,
) -> str | None:
    """Select highest admitted version with a stable canonical-path tie-break."""

    canonical_paths: set[str] = set()
    for path in binary_paths:
        try:
            canonical_paths.add(os.path.realpath(os.path.abspath(path)))
        except (TypeError, ValueError):
            logger.warning("Rejected a language-server candidate with an invalid path")

    admitted: list[BinaryAdmission] = []
    rejected = 0
    rejection_reasons: set[str] = set()
    for path in sorted(canonical_paths):
        try:
            admitted.append(_inspect_binary(path))
        except BinaryCompatibilityError as exc:
            rejected += 1
            rejection_reasons.add(str(exc))
            logger.warning("Rejected language-server candidate: %s", exc)

    if rejected:
        logger.info("Rejected %d incompatible language-server candidate(s)", rejected)
    if not admitted:
        if canonical_paths and fail_if_candidates_rejected:
            reasons = "; ".join(sorted(rejection_reasons))
            raise BinaryCompatibilityError(
                "no auto-discovered copilot-language-server met admission "
                f"requirements: {reasons}"
            )
        return None
    selected = min(
        admitted,
        key=lambda candidate: (
            -candidate.version[0],
            -candidate.version[1],
            -candidate.version[2],
            candidate.path,
        ),
    )
    logger.info(
        "Selected copilot-language-server version %s",
        _format_version(selected.version),
    )
    return selected.path

def _platform_config() -> dict[str, str]:
    """Return platform-specific discovery configuration.

    The base is only an enumeration root. Product, release, plugin layout, and
    bundled architecture below it are not compatibility evidence.
    """
    system = platform.system()
    home = os.path.expanduser("~")

    if system == "Darwin":
        return {
            "base": os.path.join(home, "Library/Application Support/JetBrains"),
            "binary_name": "copilot-language-server",
        }
    if system == "Windows":
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        return {
            "base": os.path.join(appdata, "JetBrains"),
            "binary_name": "copilot-language-server.exe",
        }
    return {
        "base": os.path.join(home, ".local/share/JetBrains"),
        "binary_name": "copilot-language-server",
    }


def _has_language_server_name(binary_path: str) -> bool:
    """Return whether a process path names the platform language-server binary."""

    return os.path.basename(os.path.normpath(binary_path)) == _platform_config()[
        "binary_name"
    ]


def _candidate_paths_from_jetbrains() -> list[str]:
    """Collect every named executable below the platform JetBrains data root."""

    cfg = _platform_config()
    base = cfg["base"]
    binary_name = cfg["binary_name"]
    if not os.path.isdir(base):
        logger.debug("JetBrains discovery root does not exist")
        return []

    candidates: set[str] = set()

    def log_walk_error(error: OSError) -> None:
        logger.warning(
            "Could not inspect part of the JetBrains discovery root: %s", error
        )

    for root, _directories, filenames in os.walk(base, onerror=log_walk_error):
        if binary_name not in filenames:
            continue
        candidate = os.path.join(root, binary_name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            candidates.add(os.path.realpath(candidate))

    logger.debug("Found %d executable candidate(s) on disk", len(candidates))
    return sorted(candidates)


def find_binary_from_jetbrains() -> str | None:
    """Select the best admitted binary found below the JetBrains data root."""

    return _select_best_binary(
        _candidate_paths_from_jetbrains(),
    )


def _find_binary_from_processes_unix() -> str | None:
    """Find a compatible binary from running processes on Unix (macOS/Linux).

    Scans ``ps`` output for named candidates and admits them by reported
    language-server version.
    """
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "command"], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("Failed to run ps")
        return None

    return _select_best_binary(
        _collect_process_paths(out.splitlines(), separator=" --"),
    )


def _find_binary_from_processes_windows() -> str | None:
    """Find a compatible binary from running processes on Windows.

    Uses PowerShell to list processes named copilot-language-server and
    extract their executable paths. Falls back to wmic if PowerShell is
    unavailable.
    """
    binary_name = _platform_config()["binary_name"]
    # Strip .exe for the process name filter
    proc_name = binary_name.removesuffix(".exe")

    # Try PowerShell first — available on all modern Windows
    lines = _query_processes_powershell(proc_name)
    if lines is None:
        # Fall back to wmic (deprecated but widely available)
        lines = _query_processes_wmic(binary_name)
    if lines is None:
        return None

    return _select_best_binary(
        _collect_process_paths(lines, separator=None),
    )


def _query_processes_powershell(proc_name: str) -> list[str] | None:
    """Query running processes via PowerShell, returning executable paths."""
    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            f"Get-Process -Name '{proc_name}' -ErrorAction SilentlyContinue "
            f"| Select-Object -ExpandProperty Path"
        ),
    ]
    try:
        out = subprocess.check_output(
            cmd, text=True, stderr=subprocess.DEVNULL, timeout=10
        )
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if lines:
            logger.debug("PowerShell found %d process path(s)", len(lines))
            return lines
        return None
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("PowerShell process query failed: %s", e)
        return None


def _query_processes_wmic(binary_name: str) -> list[str] | None:
    """Query running processes via wmic, returning executable paths."""
    cmd = [
        "wmic",
        "process",
        "where",
        f"name='{binary_name}'",
        "get",
        "ExecutablePath",
        "/value",
    ]
    try:
        out = subprocess.check_output(
            cmd, text=True, stderr=subprocess.DEVNULL, timeout=10
        )
        lines = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("ExecutablePath="):
                path = line.split("=", 1)[1].strip()
                if path:
                    lines.append(path)
        if lines:
            logger.debug("wmic found %d process path(s)", len(lines))
            return lines
        return None
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("wmic process query failed: %s", e)
        return None


def _collect_process_paths(lines: list[str], separator: str | None) -> list[str]:
    """Collect paths whose executable name identifies a language-server candidate.

    Args:
        lines: Output lines from ps, PowerShell, or wmic.
        separator: If set, each line is split on this string and the first
            part is taken as the binary path (used for Unix ``ps`` output
            where flags follow the path). If None, the entire stripped line
            is treated as the path (used for Windows output).
    """
    cfg = _platform_config()
    binary_name = cfg["binary_name"]

    candidates: set[str] = set()
    rejected = 0

    for line in lines:
        if binary_name not in line and "copilot-language-server" not in line:
            continue
        if "grep" in line:
            continue

        if separator is not None:
            binary_path = line.split(separator)[0].strip()
        else:
            binary_path = line.strip()

        if not binary_path:
            continue

        if _has_language_server_name(binary_path):
            candidates.add(os.path.realpath(binary_path))
        else:
            rejected += 1

    if rejected:
        logger.warning(
            "Rejected %d process path(s) with an unexpected executable name",
            rejected,
        )
    return sorted(candidates)


def _filter_process_paths(lines: list[str], separator: str | None) -> str | None:
    """Select the best version-admitted binary in process output."""

    return _select_best_binary(
        _collect_process_paths(lines, separator),
    )


def _candidate_paths_from_processes() -> list[str]:
    """Collect named candidates from the platform process list."""

    if platform.system() == "Windows":
        binary_name = _platform_config()["binary_name"]
        proc_name = binary_name.removesuffix(".exe")
        lines = _query_processes_powershell(proc_name)
        if lines is None:
            lines = _query_processes_wmic(binary_name)
        if lines is None:
            return []
        return _collect_process_paths(lines, separator=None)

    try:
        out = subprocess.check_output(
            ["ps", "-eo", "command"], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("Failed to run ps")
        return []
    return _collect_process_paths(out.splitlines(), separator=" --")


def find_binary_from_processes() -> str | None:
    """Find a compatible binary from running processes.

    Dispatches to the platform-specific implementation:
    - Unix (macOS, Linux): scans ``ps`` output
    - Windows: uses PowerShell (preferred) or wmic (fallback)

    Process paths identify candidates; their reported language-server versions
    determine compatibility.
    """
    return _select_best_binary(
        _candidate_paths_from_processes(),
    )


def find_binary() -> str | None:
    """Locate the compatible copilot-language-server binary.

    Process and filesystem candidates are combined before selection so ambient
    enumeration order cannot choose an older language server.
    """
    candidates = [
        *_candidate_paths_from_processes(),
        *_candidate_paths_from_jetbrains(),
    ]
    return _select_best_binary(
        candidates,
        fail_if_candidates_rejected=True,
    )
