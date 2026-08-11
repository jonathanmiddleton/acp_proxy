"""Bridge a prior JetBrains Copilot OAuth login into the direct child."""

from __future__ import annotations

import json
import logging
import ntpath
import os
import platform
import posixpath
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

COPILOT_TOKEN_ENV_NAMES = frozenset(
    {"GH_COPILOT_TOKEN", "GITHUB_COPILOT_TOKEN"}
)
_COPILOT_CHILD_TOKEN_ENV = "GITHUB_COPILOT_TOKEN"


class CopilotOAuthCredentialError(RuntimeError):
    """A prior Copilot OAuth credential cannot be loaded unambiguously."""


def copilot_oauth_path(
    environ: Mapping[str, str] | None = None,
    *,
    os_name: str | None = None,
    system_name: str | None = None,
) -> str:
    """Return the verified platform location of Copilot's ``oauth.json``."""

    source = os.environ if environ is None else environ
    platform_name = os.name if os_name is None else os_name
    if platform_name != "nt":
        host_system = platform.system() if system_name is None else system_name
        if platform_name != "posix" or host_system != "Darwin":
            raise CopilotOAuthCredentialError(
                "Automatic prior OAuth discovery is supported only on Windows and "
                "macOS; set GH_COPILOT_TOKEN or GITHUB_COPILOT_TOKEN explicitly"
            )

        config_home = source.get("XDG_CONFIG_HOME")
        if not config_home or not posixpath.isabs(config_home):
            home = source.get("HOME")
            if not home:
                raise CopilotOAuthCredentialError(
                    "Cannot locate prior Copilot OAuth on macOS: HOME is unset and "
                    "XDG_CONFIG_HOME is not an absolute path"
                )
            config_home = posixpath.join(home, ".config")
        return posixpath.join(config_home, "github-copilot", "oauth.json")

    config_home = source.get("LOCALAPPDATA")
    if not config_home:
        user_profile = source.get("USERPROFILE")
        if not user_profile:
            raise CopilotOAuthCredentialError(
                "Cannot locate prior Copilot OAuth: LOCALAPPDATA and USERPROFILE "
                "are both unset"
            )
        config_home = ntpath.join(user_profile, "AppData", "Local")
    return ntpath.join(config_home, "github-copilot", "oauth.json")


def _extract_oauth_token(document: Any) -> str:
    if not isinstance(document, dict):
        raise CopilotOAuthCredentialError(
            "Expected Copilot oauth.json to contain an object of account arrays"
        )

    tokens: list[str] = []
    for accounts in document.values():
        if not isinstance(accounts, list):
            raise CopilotOAuthCredentialError(
                "Expected each Copilot OAuth authority to contain an account array"
            )
        for account in accounts:
            if not isinstance(account, dict):
                raise CopilotOAuthCredentialError(
                    "Expected each Copilot OAuth account to contain an object"
                )
            token = account.get("accessToken")
            if not isinstance(token, str) or not token:
                raise CopilotOAuthCredentialError(
                    "Expected each Copilot OAuth account to contain a non-empty "
                    "string accessToken"
                )
            tokens.append(token)

    if len(tokens) != 1:
        raise CopilotOAuthCredentialError(
            "Expected Copilot oauth.json to contain exactly one OAuth account; "
            "set GH_COPILOT_TOKEN or GITHUB_COPILOT_TOKEN explicitly to choose"
        )
    return tokens[0]


def load_copilot_oauth_token(path: str | os.PathLike[str]) -> str:
    """Load the single ``accessToken`` from a Copilot ``oauth.json`` file."""

    try:
        with open(path, encoding="utf-8-sig") as oauth_file:
            document = json.load(oauth_file)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise CopilotOAuthCredentialError(
            f"Could not read github-copilot/oauth.json: {reason}"
        ) from None
    except (UnicodeError, json.JSONDecodeError):
        raise CopilotOAuthCredentialError(
            "Could not parse github-copilot/oauth.json as UTF-8 JSON"
        ) from None

    return _extract_oauth_token(document)


def inject_prior_copilot_oauth(
    env: Mapping[str, str],
    *,
    oauth_path: str | os.PathLike[str] | None = None,
    os_name: str | None = None,
    system_name: str | None = None,
) -> dict[str, str]:
    """Return a child environment with prior OAuth when no token is explicit."""

    platform_name = os.name if os_name is None else os_name
    child_env = dict(env)
    explicit_values = [
        value
        for key, value in child_env.items()
        if (
            key.upper() in COPILOT_TOKEN_ENV_NAMES
            if platform_name == "nt"
            else key in COPILOT_TOKEN_ENV_NAMES
        )
    ]
    if any(explicit_values):
        return child_env

    path = (
        copilot_oauth_path(
            child_env,
            os_name=platform_name,
            system_name=system_name,
        )
        if oauth_path is None
        else oauth_path
    )
    child_env[_COPILOT_CHILD_TOKEN_ENV] = load_copilot_oauth_token(path)
    logger.info("Loaded prior GitHub Copilot OAuth for the direct child")
    return child_env
