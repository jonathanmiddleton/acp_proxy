"""Tests for binary discovery logic."""

from __future__ import annotations

import os
import platform
from unittest.mock import patch

from acp_proxy.discovery import (
    _compatible_path_patterns,
    _compatible_suffixes,
    _filter_process_paths,
    _is_compatible_path,
    _platform_config,
    _user_home,
    find_binary,
    find_binary_from_jetbrains,
    find_binary_from_processes,
)


def _make_path(*segments: str) -> str:
    """Build a path from segments, using the real home directory."""
    home = os.path.expanduser("~")
    return os.path.join(home, *segments)


def _with_unsupported_ide_version(path: str) -> str:
    """Replace the supported IDE component in a compatible path with an unsupported one."""
    parts = os.path.normpath(path).split(os.sep)
    for index, part in enumerate(parts):
        if part.startswith(("IntelliJIdea", "PyCharm")):
            parts[index] = "IntelliJIdea2025.2"
            return os.sep.join(parts)
    raise AssertionError(f"No IDE component found in path: {path}")


class TestIsCompatiblePath:
    """_is_compatible_path checks home, supported IDE version, and binary name."""

    def test_correct_path_matches(self):
        """The exact expected path is accepted."""
        expected = _compatible_path_patterns()

        assert all(_is_compatible_path(e) for e in expected)

    def test_alternative_directory_structure_accepted(self):
        """A path with different intermediate dirs but correct home/IDE/binary is accepted."""
        cfg = _platform_config()
        alt = _make_path(
            "some",
            "other",
            "layout",
            "IntelliJIdea2025.3",
            "whatever",
            "path",
            cfg["binary_name"],
        )
        assert _is_compatible_path(alt)


    def test_older_intellij_rejected(self):
        """An older IntelliJ version is not compatible."""
        cfg = _platform_config()
        bad = _make_path(
            "Library/Application Support/JetBrains",
            "IntelliJIdea2024.2",
            "plugins/github-copilot-intellij/copilot-agent/native",
            cfg["arch"],
            cfg["binary_name"],
        )
        assert not _is_compatible_path(bad)

    def test_homebrew_path_rejected(self):
        """A standalone/homebrew install is not compatible — no IntelliJIdea2025.3."""
        assert not _is_compatible_path("/usr/local/bin/copilot-language-server")

    def test_npm_global_path_rejected(self):
        """An npm global install is not compatible — no IntelliJIdea2025.3."""
        bad = _make_path(".npm-global/bin/copilot-language-server")
        assert not _is_compatible_path(bad)

    def test_empty_string_rejected(self):
        assert not _is_compatible_path("")

    def test_not_under_home_rejected(self):
        """A path with correct IDE dir and binary but outside home is rejected."""
        cfg = _platform_config()
        bad = os.path.join(
            "/opt",
            "JetBrains",
            "IntelliJIdea2025.3",
            "plugins",
            "github-copilot-intellij",
            cfg["binary_name"],
        )
        assert not _is_compatible_path(bad)

    def test_wrong_binary_name_rejected(self):
        """A path under home with correct IDE but wrong binary name is rejected."""
        bad = _make_path(
            "Library/Application Support/JetBrains",
            "IntelliJIdea2025.3",
            "plugins/something-else",
        )
        assert not _is_compatible_path(bad)

    def test_other_user_rejected(self):
        """A valid IntelliJ 2025.3 path under a different user's home is rejected."""
        cfg = _platform_config()
        other_home = (
            "/Users/otheruser"
            if platform.system() != "Windows"
            else "C:\\Users\\otheruser"
        )
        bad = os.path.join(
            other_home,
            "Library/Application Support/JetBrains",
            "IntelliJIdea2025.3",
            "plugins/github-copilot-intellij/copilot-agent/native",
            cfg["arch"],
            cfg["binary_name"],
        )
        assert not _is_compatible_path(bad)

    def test_home_directory_is_prefix_not_substring(self):
        """The home directory check uses a prefix match, not a substring match."""
        home = _user_home()
        for expected in _compatible_path_patterns():
            bad = expected.replace(home, home + "2")
            assert not _is_compatible_path(bad)

    def test_ide_dir_as_substring_rejected(self):
        """IntelliJIdea2025.3 must be a path component, not a substring."""
        cfg = _platform_config()
        bad = _make_path(
            "Library/Application Support/JetBrains",
            "NotIntelliJIdea2025.3Here",
            cfg["binary_name"],
        )
        assert not _is_compatible_path(bad)


class TestCompatibleSuffix:
    """_compatible_suffix returns the IDE-and-below portion of the path."""


    def test_suffix_ends_with_binary_name(self):
        cfg = _platform_config()
        suffixes = _compatible_suffixes()
        assert all(suffix.endswith(cfg["binary_name"]) for suffix in suffixes)


class TestPlatformConfig:
    """_platform_config returns sane values for the current platform."""

    def test_config_has_required_keys(self):
        cfg = _platform_config()
        for key in ("base", "arch", "binary_name", "home"):
            assert key in cfg

    def test_binary_name_platform_appropriate(self):
        cfg = _platform_config()
        if platform.system() == "Windows":
            assert cfg["binary_name"].endswith(".exe")
        else:
            assert not cfg["binary_name"].endswith(".exe")

    def test_home_matches_expanduser(self):
        cfg = _platform_config()
        assert cfg["home"] == os.path.expanduser("~")


class TestFilterProcessPaths:
    """_filter_process_paths applies compatibility filtering to process output."""

    def test_no_separator_uses_full_line(self):
        """When separator is None, the full line is used as the path (Windows style)."""
        for expected in _compatible_path_patterns():
            lines = [f"  {expected}  "]
            result = _filter_process_paths(lines, separator=None)
            assert result == expected

    def test_grep_lines_filtered(self):
        for expected in _compatible_path_patterns():
            lines = [
                "grep copilot-language-server",
                f"{expected} --acp --stdio",
            ]
            result = _filter_process_paths(lines, separator=" --")
            assert result == expected

    def test_duplicates_deduplicated(self):
        for expected in _compatible_path_patterns():
            lines = [
                f"{expected} --acp --stdio",
                f"{expected} --acp --stdio --other",
            ]
            result = _filter_process_paths(lines, separator=" --")
            assert result == expected

    def test_empty_lines_ignored(self):
        result = _filter_process_paths(["", "  ", "\n"], separator=" --")
        assert result is None

    def test_mixed_compatible_and_incompatible(self):
        for expected in _compatible_path_patterns():
            bad = _with_unsupported_ide_version(expected)
            lines = [
                f"{bad} --acp --stdio",
                f"{expected} --acp --stdio",
            ]
            result = _filter_process_paths(lines, separator=" --")
            assert result == expected


class TestFindBinaryFromProcesses:
    """Process-based discovery dispatches to the correct platform implementation."""

    def _expected_paths(self) -> list[str]:
        return _compatible_path_patterns()

    def test_compatible_binary_found(self):
        """A running process with the compatible path is returned."""
        for expected in self._expected_paths():
            ps_output = f"COMMAND\n{expected} --acp --stdio\n/usr/bin/python3 script.py\n"
            with patch("subprocess.check_output", return_value=ps_output):
                result = find_binary_from_processes()
            assert result == expected

    def test_incompatible_binary_rejected(self):
        """A running copilot-language-server from an unsupported IDE version is rejected."""
        for expected in self._expected_paths():
            bad_path = _with_unsupported_ide_version(expected)
            ps_output = f"COMMAND\n{bad_path} --acp --stdio\n"
            with patch("subprocess.check_output", return_value=ps_output):
                result = find_binary_from_processes()
            assert result is None

    def test_mixed_compatible_and_incompatible(self):
        """Only the compatible binary is returned when both are running."""
        for expected in self._expected_paths():
            bad_path = _with_unsupported_ide_version(expected)
            ps_output = f"COMMAND\n{bad_path} --acp --stdio\n{expected} --acp --stdio\n"
            with patch("subprocess.check_output", return_value=ps_output):
                result = find_binary_from_processes()
            assert result == expected

    def test_duplicate_processes_deduplicated(self):
        """Same binary appearing multiple times in ps is deduplicated."""
        for expected in self._expected_paths():
            ps_output = (
                "COMMAND\n"
                f"{expected} --acp --stdio\n"
                f"{expected} --acp --stdio --some-other-flag\n"
            )
            with patch("subprocess.check_output", return_value=ps_output):
                result = find_binary_from_processes()
            assert result == expected

    def test_no_copilot_processes(self):
        """No copilot-language-server in ps output returns None."""
        ps_output = "COMMAND\n/usr/bin/python3\n/usr/bin/vim\n"
        with patch("subprocess.check_output", return_value=ps_output):
            result = find_binary_from_processes()
        assert result is None

    def test_ps_failure_returns_none(self):
        """If ps fails, return None instead of crashing."""
        with patch("subprocess.check_output", side_effect=OSError("ps not found")):
            result = find_binary_from_processes()
        assert result is None

    def test_grep_lines_filtered(self):
        """Lines containing 'grep' are excluded (standard ps filtering)."""
        for expected in self._expected_paths():
            ps_output = f"COMMAND\ngrep copilot-language-server\n{expected} --acp --stdio\n"
            with patch("subprocess.check_output", return_value=ps_output):
                result = find_binary_from_processes()
            assert result == expected


class TestWindowsProcessDiscovery:
    """Windows-specific process discovery using PowerShell and wmic.

    These tests patch platform.system to "Windows" which changes the
    platform config (arch, binary name, base path).  The expected path
    must be constructed under the patched config to match correctly.
    """

    def _windows_expected(self) -> str:
        """Construct the expected path as _compatible_path_pattern would on Windows."""
        home = os.path.expanduser("~")
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        return os.path.join(
            appdata,
            "JetBrains",
            "IntelliJIdea2025.3",
            "plugins",
            "github-copilot-intellij",
            "copilot-agent",
            "native",
            "win32-x64",
            "copilot-language-server.exe",
        )

    def test_powershell_discovery(self):
        """PowerShell output is parsed and filtered correctly."""
        expected = self._windows_expected()
        with (
            patch("platform.system", return_value="Windows"),
            patch(
                "acp_proxy.discovery._query_processes_powershell",
                return_value=[expected],
            ),
        ):
            result = find_binary_from_processes()
        assert result == expected

    def test_powershell_fails_falls_back_to_wmic(self):
        """When PowerShell fails, wmic is tried."""
        expected = self._windows_expected()
        with (
            patch("platform.system", return_value="Windows"),
            patch(
                "acp_proxy.discovery._query_processes_powershell",
                return_value=None,
            ),
            patch(
                "acp_proxy.discovery._query_processes_wmic",
                return_value=[expected],
            ),
        ):
            result = find_binary_from_processes()
        assert result == expected

    def test_both_methods_fail_returns_none(self):
        """When both PowerShell and wmic fail, returns None."""
        with (
            patch("platform.system", return_value="Windows"),
            patch(
                "acp_proxy.discovery._query_processes_powershell",
                return_value=None,
            ),
            patch(
                "acp_proxy.discovery._query_processes_wmic",
                return_value=None,
            ),
        ):
            result = find_binary_from_processes()
        assert result is None

    def test_incompatible_path_rejected_on_windows(self):
        """A PyCharm binary from Windows process listing is rejected."""
        bad = self._windows_expected().replace("IntelliJIdea2025.3", "PyCharm2025.1")
        with (
            patch("platform.system", return_value="Windows"),
            patch(
                "acp_proxy.discovery._query_processes_powershell",
                return_value=[bad],
            ),
        ):
            result = find_binary_from_processes()
        assert result is None

class TestFindBinaryFromJetbrains:
    """Filesystem-based discovery (ADR-006): checks expected path on disk."""

    def test_binary_exists_and_executable(self, tmp_path):
        """Returns path when binary exists and is executable."""
        with (
            patch(
                "acp_proxy.discovery._compatible_path_patterns",
                return_value=[str(tmp_path / "binary")],
            ),
            patch("os.path.isfile", return_value=True),
            patch("os.access", return_value=True),
        ):
            result = find_binary_from_jetbrains()
        assert result == str(tmp_path / "binary")

    def test_binary_not_found_returns_none(self):
        """Returns None when binary doesn't exist on disk."""
        with (
            patch("os.path.isfile", return_value=False),
        ):
            result = find_binary_from_jetbrains()
        assert result is None


class TestFindBinaryFallback:
    """find_binary tries processes first, then filesystem (ADR-006)."""

    def test_processes_checked_first(self):
        """If processes find a binary, filesystem is not checked."""
        with (
            patch(
                "acp_proxy.discovery.find_binary_from_processes",
                return_value="/found/from/ps",
            ) as mock_ps,
            patch("acp_proxy.discovery.find_binary_from_jetbrains") as mock_jb,
        ):
            result = find_binary()
        assert result == "/found/from/ps"
        mock_ps.assert_called_once()
        mock_jb.assert_not_called()

    def test_falls_back_to_filesystem(self):
        """If processes find nothing, filesystem is checked."""
        with (
            patch("acp_proxy.discovery.find_binary_from_processes", return_value=None),
            patch(
                "acp_proxy.discovery.find_binary_from_jetbrains",
                return_value="/found/on/disk",
            ) as mock_jb,
        ):
            result = find_binary()
        assert result == "/found/on/disk"
        mock_jb.assert_called_once()

    def test_both_fail_returns_none(self):
        """If neither source finds a binary, returns None."""
        with (
            patch("acp_proxy.discovery.find_binary_from_processes", return_value=None),
            patch("acp_proxy.discovery.find_binary_from_jetbrains", return_value=None),
        ):
            result = find_binary()
        assert result is None
