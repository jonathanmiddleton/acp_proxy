"""Tests for user configuration loading, subprocess environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from acp_proxy.config import (
    build_subprocess_env,
    config_path,
    ensure_default_config,
    load_config,
)


class TestLoadConfig:
    """load_config reads ~/.acp_proxy/config.json."""

    def test_creates_default_when_no_file(self, tmp_path: Path) -> None:
        """When no config exists, load_config creates a default and returns it."""
        cfg_file = tmp_path / ".acp_proxy" / "config.json"
        with (
            patch("acp_proxy.config.config_path", return_value=str(cfg_file)),
            patch("acp_proxy.config.config_dir", return_value=str(cfg_file.parent)),
        ):
            cfg = load_config()
        assert cfg_file.exists()
        assert "https_proxy" in cfg

    def test_loads_valid_json(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"https_proxy": "http://proxy:8080"}))
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            cfg = load_config()
        assert cfg == {"https_proxy": "http://proxy:8080"}

    def test_returns_empty_on_malformed_json(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json {{{")
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            assert load_config() == {}

    def test_returns_empty_when_not_object(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(["a", "list"]))
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            assert load_config() == {}

    def test_preserves_all_keys(self, tmp_path: Path) -> None:
        data = {
            "https_proxy": "http://proxy:8080",
            "no_proxy": "localhost",
            "custom_key": "value",
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(data))
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            assert load_config() == data


class TestConfigPath:
    """config_path returns a path under the user's home."""

    def test_under_home_directory(self) -> None:
        path = config_path()
        home = os.path.expanduser("~")
        assert path.startswith(home)
        assert ".acp_proxy" in path
        assert path.endswith("config.json")


class TestEnsureDefaultConfig:
    """ensure_default_config creates the config file on first run."""

    def test_creates_file_when_missing(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / ".acp_proxy" / "config.json"
        with (
            patch("acp_proxy.config.config_path", return_value=str(cfg_file)),
            patch("acp_proxy.config.config_dir", return_value=str(cfg_file.parent)),
        ):
            ensure_default_config()
        assert cfg_file.exists()
        data = json.loads(cfg_file.read_text())
        assert "https_proxy" in data

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"custom": "value"}))
        with patch("acp_proxy.config.config_path", return_value=str(cfg_file)):
            ensure_default_config()
        data = json.loads(cfg_file.read_text())
        assert data == {"custom": "value"}


class TestBuildSubprocessEnv:
    """build_subprocess_env merges config proxy settings into the environment."""

    def test_applies_proxy_from_config(self) -> None:
        cfg = {"https_proxy": "http://proxy:8080"}
        with patch.dict(os.environ, {}, clear=True):
            # Preserve PATH so the env is usable
            env = build_subprocess_env(cfg)
        assert env["HTTPS_PROXY"] == "http://proxy:8080"
        assert env["https_proxy"] == "http://proxy:8080"

    def test_env_var_takes_precedence_over_config(self) -> None:
        cfg = {"https_proxy": "http://from-config:8080"}
        with patch.dict(
            os.environ, {"HTTPS_PROXY": "http://from-env:9090"}, clear=True
        ):
            env = build_subprocess_env(cfg)
        assert env["HTTPS_PROXY"] == "http://from-env:9090"

    def test_no_config_returns_current_env(self) -> None:
        env = build_subprocess_env({})
        # Should be a copy of os.environ, not os.environ itself
        assert env is not os.environ
        assert env.get("PATH") == os.environ.get("PATH")

    def test_none_config_loads_from_disk(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"http_proxy": "http://disk-proxy:3128"}))
        with (
            patch("acp_proxy.config.config_path", return_value=str(cfg_file)),
            patch.dict(os.environ, {}, clear=True),
        ):
            env = build_subprocess_env(None)
        assert env["HTTP_PROXY"] == "http://disk-proxy:3128"
        assert env["http_proxy"] == "http://disk-proxy:3128"

    def test_all_proxy_vars_applied(self) -> None:
        cfg = {
            "http_proxy": "http://proxy:3128",
            "https_proxy": "http://proxy:3129",
            "no_proxy": "localhost,127.0.0.1",
        }
        with patch.dict(os.environ, {}, clear=True):
            env = build_subprocess_env(cfg)
        assert env["HTTP_PROXY"] == "http://proxy:3128"
        assert env["http_proxy"] == "http://proxy:3128"
        assert env["HTTPS_PROXY"] == "http://proxy:3129"
        assert env["https_proxy"] == "http://proxy:3129"
        assert env["NO_PROXY"] == "localhost,127.0.0.1"
        assert env["no_proxy"] == "localhost,127.0.0.1"

    def test_non_string_values_in_config_ignored(self) -> None:
        cfg = {"https_proxy": 12345, "http_proxy": "http://proxy:3128"}
        with patch.dict(os.environ, {}, clear=True):
            env = build_subprocess_env(cfg)
        assert "HTTPS_PROXY" not in env
        assert env["HTTP_PROXY"] == "http://proxy:3128"

    def test_case_insensitive_config_keys(self) -> None:
        cfg = {"HTTPS_PROXY": "http://proxy:8080"}
        with patch.dict(os.environ, {}, clear=True):
            env = build_subprocess_env(cfg)
        assert env["HTTPS_PROXY"] == "http://proxy:8080"
        assert env["https_proxy"] == "http://proxy:8080"

    def test_does_not_modify_os_environ(self) -> None:
        cfg = {"https_proxy": "http://proxy:8080"}
        original_env = dict(os.environ)
        build_subprocess_env(cfg)
        assert dict(os.environ) == original_env
