"""Tests for the prior GitHub Copilot OAuth credential bridge."""

from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path

import pytest

from acp_proxy.copilot_auth import (
    CopilotOAuthCredentialError,
    copilot_oauth_path,
    inject_prior_copilot_oauth,
    load_copilot_oauth_token,
)

_TOKEN_A = "oauth-canary-a-never-log"
_TOKEN_B = "oauth-canary-b-never-log"


def _oauth_document(*tokens: str) -> dict[str, object]:
    return {
        "https://company-managed-oauth.example": [
            {
                "id": f"synthetic-account-{index}",
                "accessToken": token,
                "account": {
                    "id": f"synthetic-account-{index}",
                    "label": f"Synthetic account {index}",
                },
                "scopes": ["synthetic-scope"],
            }
            for index, token in enumerate(tokens)
        ]
    }


def test_windows_oauth_path_uses_local_app_data() -> None:
    env = {
        "LOCALAPPDATA": r"C:\synthetic\local",
        "USERPROFILE": r"C:\synthetic\profile",
    }

    assert copilot_oauth_path(env, os_name="nt") == ntpath.join(
        env["LOCALAPPDATA"], "github-copilot", "oauth.json"
    )


def test_windows_oauth_path_falls_back_to_user_profile() -> None:
    env = {"USERPROFILE": r"C:\synthetic\profile"}

    assert copilot_oauth_path(env, os_name="nt") == ntpath.join(
        env["USERPROFILE"],
        "AppData",
        "Local",
        "github-copilot",
        "oauth.json",
    )


def test_non_windows_file_discovery_requires_explicit_token() -> None:
    with pytest.raises(CopilotOAuthCredentialError, match="only on Windows"):
        copilot_oauth_path({}, os_name="posix")


def test_load_oauth_token_preserves_the_exact_value_and_authority(
    tmp_path: Path,
) -> None:
    oauth_path = tmp_path / "oauth.json"
    oauth_path.write_text(json.dumps(_oauth_document(_TOKEN_A)), encoding="utf-8")

    assert load_copilot_oauth_token(oauth_path) == _TOKEN_A


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"authority": {}},
        {"authority": ["not-an-account"]},
        {"authority": [{}]},
        {"authority": [{"accessToken": None}]},
        {"authority": [{"accessToken": ""}]},
        {},
        _oauth_document(_TOKEN_A, _TOKEN_B),
    ],
)
def test_load_oauth_token_rejects_missing_or_ambiguous_credentials(
    tmp_path: Path,
    document: object,
) -> None:
    oauth_path = tmp_path / "oauth.json"
    oauth_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CopilotOAuthCredentialError):
        load_copilot_oauth_token(oauth_path)


@pytest.mark.parametrize("payload", [b"not json", b"\xff\xfe\x00"])
def test_load_oauth_token_rejects_unreadable_content(
    tmp_path: Path,
    payload: bytes,
) -> None:
    oauth_path = tmp_path / "oauth.json"
    oauth_path.write_bytes(payload)

    with pytest.raises(CopilotOAuthCredentialError):
        load_copilot_oauth_token(oauth_path)


@pytest.mark.parametrize(
    "explicit_env",
    [
        {"GH_COPILOT_TOKEN": _TOKEN_A},
        {"GITHUB_COPILOT_TOKEN": _TOKEN_A},
        {
            "GH_COPILOT_TOKEN": _TOKEN_A,
            "GITHUB_COPILOT_TOKEN": _TOKEN_B,
        },
    ],
)
def test_explicit_tokens_are_authoritative_and_unchanged(
    explicit_env: dict[str, str],
) -> None:
    child_env = inject_prior_copilot_oauth(
        explicit_env,
        oauth_path="path-that-must-not-be-read",
        os_name="posix",
    )

    assert child_env == explicit_env
    assert child_env is not explicit_env


def test_prior_oauth_is_injected_only_into_the_child_environment(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    oauth_path = tmp_path / "oauth.json"
    oauth_path.write_text(json.dumps(_oauth_document(_TOKEN_A)), encoding="utf-8")
    env = {
        "PATH": "synthetic-path",
        "GITHUB_COPILOT_API_URL": "https://company-managed-endpoint.example",
    }
    ambient_before = dict(os.environ)
    caplog.set_level("DEBUG", logger="acp_proxy.copilot_auth")

    child_env = inject_prior_copilot_oauth(
        env,
        oauth_path=oauth_path,
        os_name="posix",
    )

    assert child_env == {
        **env,
        "GITHUB_COPILOT_TOKEN": _TOKEN_A,
    }
    assert env == {
        "PATH": "synthetic-path",
        "GITHUB_COPILOT_API_URL": "https://company-managed-endpoint.example",
    }
    assert dict(os.environ) == ambient_before
    assert _TOKEN_A not in caplog.text


def test_missing_prior_oauth_fails_without_adding_a_token(tmp_path: Path) -> None:
    env: dict[str, str] = {}

    with pytest.raises(CopilotOAuthCredentialError, match="Could not read"):
        inject_prior_copilot_oauth(
            env,
            oauth_path=tmp_path / "missing-oauth.json",
            os_name="posix",
        )

    assert env == {}


def test_windows_explicit_token_name_matching_is_case_insensitive() -> None:
    env = {"github_copilot_token": _TOKEN_A}

    child_env = inject_prior_copilot_oauth(
        env,
        oauth_path="path-that-must-not-be-read",
        os_name="nt",
    )

    assert child_env == env
