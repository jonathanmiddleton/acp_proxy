"""
Binary discovery for copilot-language-server.

The only supported binary is the one bundled with the GitHub Copilot plugin
for IntelliJ IDEA 2025.3 or 2026.1. Other versions (older IntelliJ, other JetBrains
IDEs, standalone installs, Homebrew, npm) are known to be incompatible with
the ACP protocol surface this proxy requires.

Supported platforms:
- macOS (Darwin): binary at ~/Library/Application Support/JetBrains/IntelliJIdea2025.3/plugins/...
- Windows: binary at %APPDATA%/JetBrains/IntelliJIdea2025.3/plugins/... (roaming profile)

This module is the single source of truth for binary resolution. Both the
CLI entry point and the test suite import from here.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess

logger = logging.getLogger(__name__)

# The IDE directory names that identify compatible IntelliJ versions.
_IDE_DIRS = ("IntelliJIdea2025.3", "IntelliJIdea2026.1", "PyCharm2025.3", "PyCharm2026.1")

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
    if os.path.basename(normalized) != cfg["binary_name"]:
        return False

    return True


def find_binary_from_jetbrains() -> str | None:
    """Find the compatible binary from the JetBrains plugin directory.

    Returns the path if it exists and is executable, or None.
    """
    for expected in _compatible_path_patterns():
        if os.path.isfile(expected) and os.access(expected, os.X_OK):
            logger.info("Found compatible binary on disk: %s", expected)
            return expected

    logger.debug("Binary not found at any expected paths")
    return None


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
    except Exception:
        logger.debug("Failed to run ps")
        return None

    return _filter_process_paths(out.splitlines(), separator=" --")


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

    return _filter_process_paths(lines, separator=None)


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
    except Exception as e:
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
    except Exception as e:
        logger.debug("wmic process query failed: %s", e)
        return None


def _filter_process_paths(lines: list[str], separator: str | None) -> str | None:
    """Filter process listing lines to find a compatible binary path.

    Args:
        lines: Output lines from ps, PowerShell, or wmic.
        separator: If set, each line is split on this string and the first
            part is taken as the binary path (used for Unix ``ps`` output
            where flags follow the path). If None, the entire stripped line
            is treated as the path (used for Windows output).
    """
    cfg = _platform_config()
    binary_name = cfg["binary_name"]

    candidates: list[str] = []
    rejected: list[str] = []

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
            candidates.append(binary_path)
        else:
            rejected.append(binary_path)

    if rejected:
        logger.warning(
            "Rejected incompatible copilot-language-server binaries: %s",
            rejected,
        )

    if candidates:
        # Deduplicate (same binary may appear in multiple lines)
        seen: set[str] = set()
        unique = []
        for c in candidates:
            normalized = os.path.normpath(c)
            if normalized not in seen:
                seen.add(normalized)
                unique.append(c)
        logger.info("Found compatible binary from processes: %s", unique[0])
        return unique[0]

    return None


def find_binary_from_processes() -> str | None:
    """Find a compatible binary from running processes.

    Dispatches to the platform-specific implementation:
    - Unix (macOS, Linux): scans ``ps`` output
    - Windows: uses PowerShell (preferred) or wmic (fallback)

    Only accepts binaries whose path is under the current user's home
    directory and matches the IntelliJ plugin structure.
    """
    if platform.system() == "Windows":
        return _find_binary_from_processes_windows()
    return _find_binary_from_processes_unix()


def find_binary() -> str | None:
    """Locate the compatible copilot-language-server binary.

    Discovery order:
    1. Running processes (filtered to compatible path pattern)
    2. JetBrains plugin directory on disk

    Returns None if no compatible binary is found.
    """
    binary = find_binary_from_processes()
    if binary:
        return binary

    logger.info("No compatible binary in running processes, checking disk...")
    return find_binary_from_jetbrains()
