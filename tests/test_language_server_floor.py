"""Observable boundary tests for the installed language-server floor."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from acp_proxy import discovery
from acp_proxy.discovery import (
    BinaryCompatibilityError,
    _select_best_binary,
    require_compatible_binary,
)


def _executable(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("placeholder", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _version_probe(versions: dict[str, str]) -> Any:
    def read(binary_path: str, **_kwargs: object) -> bytes:
        return f"{versions[os.path.realpath(binary_path)]}\n".encode("ascii")

    return read


def test_installed_15183_is_the_supported_admission_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    below_floor = _executable(tmp_path / "below" / "copilot-language-server")
    supported = _executable(tmp_path / "supported" / "copilot-language-server")
    versions = {
        os.path.realpath(below_floor): "1.518.2",
        os.path.realpath(supported): "1.518.3",
    }
    monkeypatch.setattr(
        discovery,
        "_read_bounded_version_output",
        _version_probe(versions),
    )

    with pytest.raises(BinaryCompatibilityError, match="below required minimum"):
        require_compatible_binary(below_floor)
    assert require_compatible_binary(supported) == os.path.realpath(supported)


def test_newer_installed_server_is_preferred_over_15183(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported = _executable(tmp_path / "supported" / "copilot-language-server")
    preferred = _executable(tmp_path / "preferred" / "copilot-language-server")
    versions = {
        os.path.realpath(supported): "1.518.3",
        os.path.realpath(preferred): "1.523.3",
    }
    monkeypatch.setattr(
        discovery,
        "_read_bounded_version_output",
        _version_probe(versions),
    )

    assert _select_best_binary([supported, preferred]) == os.path.realpath(preferred)
