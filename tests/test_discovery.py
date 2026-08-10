"""Tests for version-driven language-server discovery."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any

import pytest

from acp_proxy import discovery
from acp_proxy.application_policy import MIN_COPILOT_LANGUAGE_SERVER_VERSION
from acp_proxy.discovery import (
    BinaryCompatibilityError,
    _candidate_paths_from_jetbrains,
    _candidate_paths_from_processes,
    _collect_process_paths,
    _platform_config,
    find_binary,
    find_binary_from_jetbrains,
    find_binary_from_processes,
)


def _executable(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _version_probe(versions: dict[str, str]) -> Any:
    def read(binary_path: str, **_kwargs: Any) -> bytes:
        version = versions[os.path.realpath(binary_path)]
        return f"{version}\n".encode("ascii")

    return read


def _platform_binary(tmp_path: Path, *parents: str) -> str:
    return _executable(tmp_path.joinpath(*parents, _platform_config()["binary_name"]))


class TestPlatformConfig:
    """Platform configuration only identifies an enumeration root and filename."""

    def test_config_has_only_discovery_keys(self) -> None:
        assert set(_platform_config()) == {"base", "binary_name"}

    def test_binary_name_is_platform_appropriate(self) -> None:
        binary_name = _platform_config()["binary_name"]
        assert binary_name.endswith(".exe") is (platform.system() == "Windows")


class TestCollectProcessPaths:
    """Process parsing identifies candidates without assigning compatibility."""

    def test_named_binary_is_collected_independent_of_parent_layout(
        self, tmp_path: Path
    ) -> None:
        candidate = _platform_binary(
            tmp_path,
            "arbitrary-product",
            "rolling-release",
            "unexpected-layout",
        )

        assert _collect_process_paths(
            [f"{candidate} --acp --stdio"], separator=" --"
        ) == [os.path.realpath(candidate)]

    def test_unexpected_executable_name_is_rejected(self, tmp_path: Path) -> None:
        candidate = tmp_path / "copilot-language-server-helper"

        assert _collect_process_paths([str(candidate)], separator=None) == []

    def test_duplicates_are_deduplicated(self, tmp_path: Path) -> None:
        candidate = _platform_binary(tmp_path)
        lines = [
            f"{candidate} --acp --stdio",
            f"{candidate} --acp --stdio --other",
        ]

        assert _collect_process_paths(lines, separator=" --") == [
            os.path.realpath(candidate)
        ]

    def test_headers_empty_lines_and_grep_are_ignored(self) -> None:
        assert _collect_process_paths(
            ["COMMAND", "", "grep copilot-language-server"], separator=" --"
        ) == []


class TestFindBinaryFromProcesses:
    """A process candidate's reported version is its compatibility evidence."""

    def test_compatible_binary_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = _platform_binary(tmp_path, "unlisted-ide-release")
        version = _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
        monkeypatch.setattr(
            discovery,
            "_candidate_paths_from_processes",
            lambda: [candidate],
        )
        monkeypatch.setattr(
            discovery,
            "_read_bounded_version_output",
            _version_probe({os.path.realpath(candidate): version}),
        )

        assert find_binary_from_processes() == os.path.realpath(candidate)

    def test_same_path_is_rejected_when_reported_version_is_below_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = _platform_binary(tmp_path, "unlisted-ide-release")
        monkeypatch.setattr(
            discovery,
            "_candidate_paths_from_processes",
            lambda: [candidate],
        )
        monkeypatch.setattr(
            discovery,
            "_read_bounded_version_output",
            _version_probe({os.path.realpath(candidate): "0.0.0"}),
        )

        assert find_binary_from_processes() is None

    def test_no_process_candidates_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(discovery, "_candidate_paths_from_processes", list)

        assert find_binary_from_processes() is None


class TestProcessEnumeration:
    """OS-specific process formats produce the same candidate paths."""

    def test_macos_process_output_keeps_path_with_arguments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = _executable(tmp_path / "copilot-language-server")
        output = f"COMMAND\n{candidate} --acp --stdio\n/usr/bin/python script.py\n"
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(
            subprocess,
            "check_output",
            lambda *_args, **_kwargs: output,
        )

        assert _candidate_paths_from_processes() == [os.path.realpath(candidate)]

    def test_windows_process_output_is_one_path_per_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = _executable(tmp_path / "copilot-language-server.exe")
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            discovery,
            "_query_processes_powershell",
            lambda _name: [candidate],
        )

        assert _candidate_paths_from_processes() == [os.path.realpath(candidate)]

    def test_windows_falls_back_to_wmic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = _executable(tmp_path / "copilot-language-server.exe")
        monkeypatch.setattr(platform, "system", lambda: "Windows")
        monkeypatch.setattr(
            discovery,
            "_query_processes_powershell",
            lambda _name: None,
        )
        monkeypatch.setattr(
            discovery,
            "_query_processes_wmic",
            lambda _name: [candidate],
        )

        assert _candidate_paths_from_processes() == [os.path.realpath(candidate)]

    def test_process_query_failure_returns_no_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform, "system", lambda: "Linux")

        def fail(*_args: object, **_kwargs: object) -> str:
            raise OSError("process query unavailable")

        monkeypatch.setattr(subprocess, "check_output", fail)

        assert _candidate_paths_from_processes() == []


class TestFindBinaryFromJetBrains:
    """Filesystem discovery enumerates releases and layouts instead of encoding them."""

    def test_recursive_discovery_admits_reported_version(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary_name = _platform_config()["binary_name"]
        candidate = _executable(
            tmp_path
            / "future-product"
            / "rolling-release"
            / "plugins"
            / "different-architecture"
            / binary_name
        )
        version = _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
        monkeypatch.setattr(
            discovery,
            "_platform_config",
            lambda: {"base": str(tmp_path), "binary_name": binary_name},
        )
        monkeypatch.setattr(
            discovery,
            "_read_bounded_version_output",
            _version_probe({os.path.realpath(candidate): version}),
        )

        assert _candidate_paths_from_jetbrains() == [os.path.realpath(candidate)]
        assert find_binary_from_jetbrains() == os.path.realpath(candidate)

    def test_wrong_filename_is_not_a_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary_name = _platform_config()["binary_name"]
        _executable(tmp_path / f"{binary_name}-helper")
        monkeypatch.setattr(
            discovery,
            "_platform_config",
            lambda: {"base": str(tmp_path), "binary_name": binary_name},
        )

        assert _candidate_paths_from_jetbrains() == []

    def test_missing_discovery_root_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            discovery,
            "_platform_config",
            lambda: {
                "base": str(tmp_path / "missing"),
                "binary_name": "copilot-language-server",
            },
        )

        assert find_binary_from_jetbrains() is None


class TestFindBinarySelection:
    """Combined discovery selects by reported version, independent of source."""

    def test_higher_filesystem_version_beats_running_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        process_path = _platform_binary(tmp_path, "process")
        disk_path = _platform_binary(tmp_path, "disk")
        minimum = MIN_COPILOT_LANGUAGE_SERVER_VERSION
        newer = minimum[0], minimum[1], minimum[2] + 1
        monkeypatch.setattr(
            discovery,
            "_candidate_paths_from_processes",
            lambda: [process_path],
        )
        monkeypatch.setattr(
            discovery,
            "_candidate_paths_from_jetbrains",
            lambda: [disk_path],
        )
        monkeypatch.setattr(
            discovery,
            "_read_bounded_version_output",
            _version_probe(
                {
                    os.path.realpath(process_path): _version_text(minimum),
                    os.path.realpath(disk_path): _version_text(newer),
                }
            ),
        )

        assert find_binary() == os.path.realpath(disk_path)

    def test_empty_union_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(discovery, "_candidate_paths_from_processes", list)
        monkeypatch.setattr(discovery, "_candidate_paths_from_jetbrains", list)

        assert find_binary() is None

    def test_old_only_union_reports_observed_and_required_versions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = _platform_binary(tmp_path)
        required = _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
        monkeypatch.setattr(
            discovery,
            "_candidate_paths_from_processes",
            lambda: [candidate],
        )
        monkeypatch.setattr(discovery, "_candidate_paths_from_jetbrains", list)
        monkeypatch.setattr(
            discovery,
            "_read_bounded_version_output",
            _version_probe({os.path.realpath(candidate): "0.0.0"}),
        )

        with pytest.raises(BinaryCompatibilityError) as exc_info:
            find_binary()
        assert "0.0.0" in str(exc_info.value)
        assert required in str(exc_info.value)
