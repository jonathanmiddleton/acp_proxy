"""
Binary discovery for copilot-language-server.

Supported binaries are bundled with the GitHub Copilot plugin for IntelliJ IDEA
or PyCharm 2025.3/2026.1. Other versions/products, standalone installs,
Homebrew, and npm are outside the validated ACP surface.

Supported platforms:
- macOS (Darwin): binary under a supported JetBrains IDE plugin directory.
- Windows: binary under a supported `%APPDATA%/JetBrains` plugin directory.

This module is the single source of truth for binary resolution. Both the
CLI entry point and the test suite import from here.
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

logger = logging.getLogger(__name__)

MIN_COPILOT_LANGUAGE_SERVER_VERSION = (1, 523, 3)
"""Oldest copilot-language-server admitted by every production entry point."""

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
        "SystemRoot",
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
        if key in _VERSION_PROBE_ENV_KEYS or key.startswith("LC_")
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


def _inspect_binary(
    binary_path: str,
    *,
    require_supported_path: bool,
) -> BinaryAdmission:
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
    if require_supported_path and not _is_compatible_path(canonical_path):
        raise BinaryCompatibilityError(
            "copilot-language-server binary is outside supported JetBrains paths"
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

    return _inspect_binary(binary_path, require_supported_path=False)


def require_compatible_binary(binary_path: str) -> str:
    """Admit an explicit/programmatic binary and return its canonical path."""

    return admit_compatible_binary(binary_path).path


def _select_best_binary(
    binary_paths: Iterable[str],
    *,
    require_supported_path: bool,
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
            admitted.append(
                _inspect_binary(path, require_supported_path=require_supported_path)
            )
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

# The IDE directory names that identify compatible IntelliJ versions.
_IDE_DIRS = (
    "IntelliJIdea2025.3",
    "IntelliJIdea2026.1",
    "PyCharm2025.3",
    "PyCharm2026.1",
)

_PLUGIN_SUFFIX_PARTS = (
    "plugins",
    "github-copilot-intellij",
    "copilot-agent",
    "native",
    "{arch}",
    "{binary_name}",
)


def _platform_config() -> dict[str, str]:
    """Return platform-specific discovery configuration.

    Returns a dict with keys: base, arch, binary_name, home.
    """
    system = platform.system()
    home = os.path.expanduser("~")

    if system == "Darwin":
        return {
            "home": home,
            "base": os.path.join(home, "Library/Application Support/JetBrains"),
            "arch": "darwin-arm64" if platform.machine() == "arm64" else "darwin-x64",
            "binary_name": "copilot-language-server",
        }
    elif system == "Windows":
        # Windows uses %APPDATA% (roaming profile) for JetBrains config.
        # The binary is an .exe on Windows.
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        return {
            "home": home,
            "base": os.path.join(appdata, "JetBrains"),
            "arch": "win32-x64",
            "binary_name": "copilot-language-server.exe",
        }
    else:
        # Linux — included for completeness but not a current target
        return {
            "home": home,
            "base": os.path.join(home, ".local/share/JetBrains"),
            "arch": "linux-x64",
            "binary_name": "copilot-language-server",
        }


def _compatible_path_patterns() -> list[str]:
    """Return the expected full paths for the compatible binaries on this platform."""
    cfg = _platform_config()
    suffix_parts = [
        p.format(arch=cfg["arch"], binary_name=cfg["binary_name"])
        for p in _PLUGIN_SUFFIX_PARTS
    ]
    return [os.path.join(cfg["base"], ide_dir, *suffix_parts) for ide_dir in _IDE_DIRS]


def _compatible_suffixes() -> list[str]:
    """Return the path suffixes from the IDE directories onward.

    This is the portion that identifies a compatible binary regardless of
    where the user's home directory is located. Used together with a home
    directory check to validate paths from process listings.
    """
    cfg = _platform_config()
    suffix_parts = [
        p.format(arch=cfg["arch"], binary_name=cfg["binary_name"])
        for p in _PLUGIN_SUFFIX_PARTS
    ]
    return [os.path.join(ide_dir, *suffix_parts) for ide_dir in _IDE_DIRS]


def _user_home() -> str:
    """Return the current user's home directory, normalized."""
    return os.path.normpath(os.path.expanduser("~"))


def _is_compatible_path(binary_path: str) -> bool:
    """Check whether a binary path is a compatible IntelliJ binary.

    Three conditions must hold:
    1. The path is under the current user's home directory.
    2. The path contains one of the supported IDE dirs as a path component.
    3. The binary filename is the expected platform-specific name
       (``copilot-language-server`` or ``copilot-language-server.exe``).

    We do not assume anything else about the intermediate directory
    structure — deployment paths vary across environments, and the
    plugin layout may differ between IDE versions or install methods.
    """
    normalized = os.path.normpath(binary_path)
    home = _user_home()
    cfg = _platform_config()

    # Must be under the current user's home
    if not normalized.startswith(home + os.sep):
        return False

    # Must contain one of the IDE_DIRS as a path component
    parts = normalized.split(os.sep)
    if not any(ide_dir in parts for ide_dir in _IDE_DIRS):
        return False

    # Must end with the correct binary name
    return os.path.basename(normalized) == cfg["binary_name"]


def _candidate_paths_from_jetbrains() -> list[str]:
    """Collect every executable in the supported JetBrains plugin paths."""

    candidates = [
        expected
        for expected in _compatible_path_patterns()
        if os.path.isfile(expected) and os.access(expected, os.X_OK)
    ]
    logger.debug("Found %d executable candidate(s) on disk", len(candidates))
    return candidates


def find_binary_from_jetbrains() -> str | None:
    """Select the best admitted binary from supported plugin directories."""

    return _select_best_binary(
        _candidate_paths_from_jetbrains(),
        require_supported_path=True,
    )


def _find_binary_from_processes_unix() -> str | None:
    """Find a compatible binary from running processes on Unix (macOS/Linux).

    Scans ``ps`` output for copilot-language-server processes, but only
    accepts those whose resolved path matches the IntelliJ plugin
    location under the current user's home.
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
        require_supported_path=True,
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
        require_supported_path=True,
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
    """Collect path-compatible binaries from a process listing.

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

        if _is_compatible_path(binary_path):
            candidates.add(os.path.realpath(binary_path))
        else:
            rejected += 1

    if rejected:
        logger.warning(
            "Rejected %d process path(s) outside supported JetBrains locations",
            rejected,
        )
    return sorted(candidates)


def _filter_process_paths(lines: list[str], separator: str | None) -> str | None:
    """Select the best version-admitted binary in process output."""

    return _select_best_binary(
        _collect_process_paths(lines, separator),
        require_supported_path=True,
    )


def _candidate_paths_from_processes() -> list[str]:
    """Collect all path-compatible candidates from the platform process list."""

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

    Only accepts binaries whose path is under the current user's home
    directory and matches the IntelliJ plugin structure.
    """
    return _select_best_binary(
        _candidate_paths_from_processes(),
        require_supported_path=True,
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
        require_supported_path=True,
        fail_if_candidates_rejected=True,
    )
