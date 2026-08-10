"""Production admission tests for the owned copilot-language-server binary."""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from acp_proxy import discovery
from acp_proxy.application_policy import MIN_COPILOT_LANGUAGE_SERVER_VERSION
from acp_proxy.discovery import (
    BinaryCompatibilityError,
    _probe_binary_version,
    _read_bounded_version_output,
    _select_best_binary,
    _version_probe_env,
    parse_copilot_language_server_version,
    require_compatible_binary,
)


def _executable(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _version_probe(
    versions: dict[str, str],
) -> Any:
    def read(binary_path: str, **_kwargs: Any) -> bytes:
        canonical = os.path.realpath(binary_path)
        return (versions[canonical] + "\n").encode("ascii")

    return read


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _version_before(version: tuple[int, int, int]) -> tuple[int, int, int]:
    parts = list(version)
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] > 0:
            parts[index] -= 1
            parts[index + 1 :] = [999_999] * (len(parts) - index - 1)
            return parts[0], parts[1], parts[2]
    raise AssertionError("the configured minimum must have a predecessor")


def _version_after(version: tuple[int, int, int]) -> tuple[int, int, int]:
    major, minor, patch = version
    return major, minor, patch + 1


def _script(path: Path, *, unix: str, windows: str) -> str:
    path = path.with_suffix(".cmd") if os.name == "nt" else path
    path.write_text(windows if os.name == "nt" else unix, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_configured_minimum_is_a_canonical_semantic_version() -> None:
    configured = MIN_COPILOT_LANGUAGE_SERVER_VERSION

    assert len(configured) == 3
    assert all(isinstance(part, int) and part >= 0 for part in configured)
    assert (
        parse_copilot_language_server_version(_version_text(configured)) == configured
    )
    _version_before(configured)


@pytest.mark.parametrize(
    "raw",
    [
        True,
        False,
        1,
        7.89,
        None,
        "",
        "v7.8.9",
        "7.8",
        "7.8.9.10",
        "7.08.9",
        "7.8.-9",
        "7.8.9 extra",
    ],
)
def test_version_parser_rejects_noncanonical_values(raw: object) -> None:
    with pytest.raises(BinaryCompatibilityError, match="MAJOR.MINOR.PATCH"):
        parse_copilot_language_server_version(raw)


def test_version_probe_env_matches_windows_names_case_insensitively() -> None:
    source = {
        "SYSTEMROOT": r"C:\Windows",
        "appdata": r"C:\Users\example\AppData\Roaming",
        "ACP_PROXY_MEADOW_SECRET": "must-not-pass",
    }

    assert _version_probe_env(source) == {
        "SYSTEMROOT": r"C:\Windows",
        "appdata": r"C:\Users\example\AppData\Roaming",
    }


@pytest.mark.parametrize(
    "observed_version",
    [
        "0.0.0",
        _version_text(_version_before(MIN_COPILOT_LANGUAGE_SERVER_VERSION)),
    ],
)
def test_explicit_binary_rejects_below_floor_with_sanitized_versions(
    observed_version: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _executable(tmp_path / "copilot-language-server")
    canonical = os.path.realpath(binary)
    monkeypatch.setattr(
        discovery,
        "_read_bounded_version_output",
        _version_probe({canonical: observed_version}),
    )

    with pytest.raises(BinaryCompatibilityError) as exc_info:
        require_compatible_binary(binary)

    message = str(exc_info.value)
    assert observed_version in message
    assert _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION) in message
    assert canonical not in message
    assert "placeholder" not in message


@pytest.mark.parametrize(
    "observed_version",
    [
        _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION),
        _version_text(_version_after(MIN_COPILOT_LANGUAGE_SERVER_VERSION)),
    ],
)
def test_explicit_binary_accepts_floor_or_newer_and_returns_canonical_path(
    observed_version: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = _executable(tmp_path / "copilot-language-server")
    canonical = os.path.realpath(binary)
    monkeypatch.setattr(
        discovery,
        "_read_bounded_version_output",
        _version_probe({canonical: observed_version}),
    )

    assert require_compatible_binary(binary) == canonical


@given(
    order=st.permutations(("a", "b", "c")),
    version_minor_offsets=st.tuples(
        st.integers(min_value=0, max_value=200),
        st.integers(min_value=0, max_value=200),
        st.integers(min_value=0, max_value=200),
    ),
    version_patch_offsets=st.tuples(
        st.integers(min_value=0, max_value=20),
        st.integers(min_value=0, max_value=20),
        st.integers(min_value=0, max_value=20),
    ),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_selection_is_highest_version_then_canonical_path_for_generated_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
    version_minor_offsets: tuple[int, int, int],
    version_patch_offsets: tuple[int, int, int],
) -> None:
    paths = {
        name: _executable(tmp_path / name / "copilot-language-server")
        for name in ("a", "b", "c")
    }
    minimum_major, minimum_minor, minimum_patch = (
        MIN_COPILOT_LANGUAGE_SERVER_VERSION
    )
    semantic_versions = {
        name: (
            minimum_major,
            minimum_minor + minor_offset,
            minimum_patch + patch_offset,
        )
        for name, minor_offset, patch_offset in zip(
            ("a", "b", "c"),
            version_minor_offsets,
            version_patch_offsets,
            strict=True,
        )
    }
    versions = {
        os.path.realpath(paths[name]): ".".join(
            str(part) for part in semantic_versions[name]
        )
        for name in paths
    }
    monkeypatch.setattr(
        discovery,
        "_read_bounded_version_output",
        _version_probe(versions),
    )

    expected_name = min(
        ("a", "b", "c"),
        key=lambda name: (
            tuple(-part for part in semantic_versions[name]),
            os.path.realpath(paths[name]),
        ),
    )
    selected = _select_best_binary(
        [paths[name] for name in order],
    )
    assert selected == os.path.realpath(paths[expected_name])


def test_selection_ignores_malformed_and_below_floor_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = _executable(tmp_path / "malformed")
    old = _executable(tmp_path / "old")
    admitted = _executable(tmp_path / "admitted")
    versions = {
        os.path.realpath(malformed): "unexpected output with 9.9.9",
        os.path.realpath(old): _version_text(
            _version_before(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
        ),
        os.path.realpath(admitted): _version_text(
            MIN_COPILOT_LANGUAGE_SERVER_VERSION
        ),
    }
    monkeypatch.setattr(
        discovery,
        "_read_bounded_version_output",
        _version_probe(versions),
    )

    assert _select_best_binary(
        [malformed, old, admitted],
    ) == os.path.realpath(admitted)


def test_version_probe_excludes_ambient_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_version = _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
    binary = _script(
        tmp_path / "copilot-language-server",
        unix=(
            "#!/bin/sh\n"
            "if [ -n \"$MEADOW_OPENAI_API_KEY\" ] || "
            "[ -n \"$ACP_PROXY_MEADOW_SECRET\" ]; then\n"
            "  printf '9.9.9\\n'\n"
            "else\n"
            f"  printf '{configured_version}\\n'\n"
            "fi\n"
        ),
        windows=(
            "@if defined MEADOW_OPENAI_API_KEY (echo 9.9.9) else "
            "if defined ACP_PROXY_MEADOW_SECRET (echo 9.9.9) else "
            f"echo {configured_version}\r\n"
        ),
    )

    monkeypatch.setenv("PATH", "/safe/path")
    monkeypatch.setenv("MEADOW_OPENAI_API_KEY", "provider-canary")
    monkeypatch.setenv("ACP_PROXY_MEADOW_SECRET", "launch-canary")

    assert _probe_binary_version(binary) == MIN_COPILOT_LANGUAGE_SERVER_VERSION


def test_version_probe_rejects_output_flood_without_retaining_it(
    tmp_path: Path,
) -> None:
    binary = _script(
        tmp_path / "copilot-language-server",
        unix=(
            "#!/bin/sh\n"
            "i=0\n"
            "while [ \"$i\" -lt 100000 ]; do\n"
            "  printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'\n"
            "  i=$((i + 1))\n"
            "done\n"
            "sleep 10\n"
        ),
        windows=(
            "@for /L %%i in (1,1,100000) do @<nul set /p "
            "=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\r\n"
            "@ping -n 11 127.0.0.1 >nul\r\n"
        ),
    )

    started = time.monotonic()
    with pytest.raises(BinaryCompatibilityError) as exc_info:
        _probe_binary_version(binary)
    elapsed = time.monotonic() - started
    assert "safety limit" in str(exc_info.value)
    assert "x" * 20 not in str(exc_info.value)


def test_version_probe_nonzero_exit_is_sanitized(tmp_path: Path) -> None:
    canary = "NONZERO_OUTPUT_CANARY_MUST_NOT_SURFACE"
    binary = _script(
        tmp_path / "copilot-language-server",
        unix=(
            "#!/bin/sh\n"
            f"printf '{canary}\\n'\n"
            f"printf '{canary}\\n' >&2\n"
            "exit 17\n"
        ),
        windows=(
            f"@echo {canary}\r\n"
            f"@echo {canary} 1>&2\r\n"
            "@exit /b 17\r\n"
        ),
    )

    with pytest.raises(BinaryCompatibilityError) as exc_info:
        _probe_binary_version(binary)

    assert "version probe failed" in str(exc_info.value)
    assert canary not in str(exc_info.value)


def test_version_probe_timeout_is_sanitized_and_kills_posix_descendants(
    tmp_path: Path,
) -> None:
    canary = "TIMEOUT_OUTPUT_CANARY_MUST_NOT_SURFACE"
    marker = tmp_path / "descendant-survived"
    binary = _script(
        tmp_path / "copilot-language-server",
        unix=(
            "#!/bin/sh\n"
            f"printf '{canary}\\n' >&2\n"
            f"(sleep 0.3; printf survived > {shlex.quote(str(marker))}) &\n"
            "exit 0\n"
        ),
        windows=(
            f"@echo {canary} 1>&2\r\n"
            "@ping -n 11 127.0.0.1 >nul\r\n"
        ),
    )

    started = time.monotonic()
    with pytest.raises(BinaryCompatibilityError) as exc_info:
        _read_bounded_version_output(binary, timeout_s=0.05)
    elapsed = time.monotonic() - started

    assert "timed out" in str(exc_info.value)
    assert canary not in str(exc_info.value)
    if os.name != "nt":
        time.sleep(0.4)
        assert not marker.exists()
