"""Tests for the acp-proxy command-line entry point and server wiring."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from acp_proxy import __main__ as cli
from acp_proxy import discovery
from acp_proxy.application_policy import MIN_COPILOT_LANGUAGE_SERVER_VERSION
from acp_proxy.client import ModelAcknowledgementError
from acp_proxy.copilot_auth import CopilotOAuthCredentialError
from acp_proxy.direct_protocol import CreateSessionRequest, DirectLimits, PromptRequest
from acp_proxy.discovery import BinaryAdmission, BinaryCompatibilityError


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


@pytest.fixture(autouse=True)
def _admit_unit_test_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep server-wiring tests isolated from the real executable boundary."""

    monkeypatch.setattr(
        cli,
        "admit_compatible_binary",
        lambda path: BinaryAdmission(
            path=path,
            version=MIN_COPILOT_LANGUAGE_SERVER_VERSION,
        ),
    )


def _old_version_executable(tmp_path: Path) -> str:
    """Create a real executable whose only behavior is an old version report."""

    old_version = _version_text(
        _version_before(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
    )
    if os.name == "nt":
        path = tmp_path / "copilot-language-server.cmd"
        path.write_text(f"@echo {old_version}\r\n", encoding="utf-8")
    else:
        path = tmp_path / "copilot-language-server"
        path.write_text(
            f"#!/bin/sh\nprintf '{old_version}\\n'\n",
            encoding="utf-8",
        )
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize(
    ("argv", "expected_host"),
    [
        (["--consumer-mode", "opencode-legacy"], "127.0.0.1"),
        (
            ["--consumer-mode", "opencode-legacy", "--host", "0.0.0.0"],
            "0.0.0.0",
        ),
    ],
)
def test_bind_host_cli_contract(argv: list[str], expected_host: str) -> None:
    """The CLI remains loopback-only by default and accepts an explicit bind."""
    args = cli._build_parser().parse_args(argv)

    assert args.host == expected_host


def test_metadata_records_requested_bind_host(tmp_path: Path) -> None:
    """Readiness metadata reports the address on which Uvicorn was configured."""
    metadata_path = tmp_path / "proxy.meta.json"

    cli._write_metadata_file(str(metadata_path), 8765, host="0.0.0.0")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["pid"] == os.getpid()
    assert metadata["host"] == "0.0.0.0"


def test_windows_shutdown_handles_ctrl_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A targeted Windows process group can shut down through CTRL+BREAK."""

    registered: dict[object, Any] = {}
    shutdowns: list[str] = []

    class NoPosixSignalLoop:
        def add_signal_handler(self, *_args: Any) -> None:
            raise AssertionError("Windows must use synchronous signal handlers")

    sigbreak = object()
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.signal, "SIGBREAK", sigbreak, raising=False)
    monkeypatch.setattr(
        cli.signal,
        "signal",
        lambda sig, callback: registered.setdefault(sig, callback),
    )

    cli._install_shutdown_signal_handlers(
        NoPosixSignalLoop(),
        lambda: shutdowns.append("shutdown"),
    )

    assert set(registered) == {cli.signal.SIGINT, sigbreak}
    registered[sigbreak](sigbreak, None)
    assert shutdowns == ["shutdown"]


@pytest.mark.asyncio
async def test_run_passes_requested_bind_host_to_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The requested CLI bind address reaches the HTTP server boundary."""
    observed: dict[str, Any] = {}

    class FakeAcpClient:
        def __init__(self, binary: str, **_kwargs: Any) -> None:
            self.binary = binary
            self.models = [SimpleNamespace(model_id="test-model")]
            self.default_model = "test-model"
            self.stopped = False
            self._close_handler: Any = None
            observed["client"] = self

        def on_transport_closed(self, handler: Any) -> None:
            self._close_handler = handler

        async def start(self, env: dict[str, str] | None = None) -> None:
            observed["subprocess_env"] = env

        async def create_session(self, cwd: str) -> None:
            observed["cwd"] = cwd

        async def stop(self) -> None:
            self.stopped = True

    class FakeSocket:
        def getsockname(self) -> tuple[str, int]:
            return ("0.0.0.0", 8765)

    class FakeUvicornServer:
        def __init__(self, config: Any) -> None:
            observed["config"] = config
            self.servers: list[Any] = []
            self.should_exit = False

        async def startup(self) -> None:
            self.servers = [SimpleNamespace(sockets=[FakeSocket()])]

        async def main_loop(self) -> None:
            return None

        async def shutdown(self) -> None:
            observed["shutdown"] = True

    class FakeSignalLoop:
        def add_signal_handler(self, *_args: Any) -> None:
            return None

    async def fake_app(
        _scope: dict[str, Any], _receive: Any, _send: Any
    ) -> None:
        return None

    monkeypatch.setattr(cli, "AcpClient", FakeAcpClient)
    monkeypatch.setattr(cli, "create_app", lambda *_args, **_kwargs: fake_app)
    monkeypatch.setattr(cli.uvicorn, "Server", FakeUvicornServer)
    monkeypatch.setattr(cli.asyncio, "get_event_loop", lambda: FakeSignalLoop())

    await cli.run(
        "/fake/copilot-language-server",
        8765,
        str(tmp_path),
        host="0.0.0.0",
        consumer_mode="opencode-legacy",
    )

    assert observed["config"].host == "0.0.0.0"
    assert observed["client"].stopped is True
    assert observed["shutdown"] is True


@pytest.mark.asyncio
async def test_programmatic_run_rejects_old_binary_before_client_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The programmatic entry point cannot bypass production admission."""

    class ForbiddenClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("old binary reached AcpClient construction")

    old_binary = _old_version_executable(tmp_path)
    monkeypatch.setattr(cli, "admit_compatible_binary", discovery.admit_compatible_binary)
    monkeypatch.setattr(cli, "AcpClient", ForbiddenClient)

    old_version = _version_text(
        _version_before(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
    )
    with pytest.raises(BinaryCompatibilityError, match=re.escape(old_version)):
        await cli.run(
            old_binary,
            8765,
            "/workspace",
            consumer_mode="meadow-direct",
            launch_secret="s" * 48,
            execution_authority="trusted-host",
        )


def test_cli_explicit_old_binary_fails_before_client_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI ``--binary`` path crosses the same real version boundary."""

    class ForbiddenClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("old binary reached AcpClient construction")

    old_binary = _old_version_executable(tmp_path)
    monkeypatch.setattr(cli, "admit_compatible_binary", discovery.admit_compatible_binary)
    monkeypatch.setattr(cli, "AcpClient", ForbiddenClient)
    monkeypatch.setattr(cli, "_configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "load_config", dict)
    monkeypatch.setattr(
        cli,
        "build_subprocess_env",
        lambda _cfg: {"GITHUB_COPILOT_TOKEN": "synthetic-token"},
    )
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acp-proxy",
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "trusted-host",
            "--binary",
            old_binary,
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1


def test_cli_direct_injects_prior_oauth_into_child_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct CLI startup augments only its local subprocess mapping."""

    observed: dict[str, Any] = {}
    token = "oauth-main-canary-never-log"
    launch_secret = "s" * 48

    def fake_inject(env: dict[str, str]) -> dict[str, str]:
        child_env = dict(env)
        child_env["GITHUB_COPILOT_TOKEN"] = token
        return child_env

    async def fake_run(*_args: Any, **kwargs: Any) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(cli, "_configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "load_config", dict)
    monkeypatch.setattr(
        cli,
        "build_subprocess_env",
        lambda _cfg: {
            "PATH": "synthetic-path",
            cli.DIRECT_SECRET_ENV: launch_secret,
        },
    )
    monkeypatch.setattr(cli, "inject_prior_copilot_oauth", fake_inject)
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, launch_secret)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acp-proxy",
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "trusted-host",
            "--binary",
            "/synthetic/copilot-language-server",
        ],
    )

    cli.main()

    subprocess_env = observed["subprocess_env"]
    assert subprocess_env["GITHUB_COPILOT_TOKEN"] == token
    assert subprocess_env["PATH"] == "synthetic-path"
    assert cli.DIRECT_SECRET_ENV not in subprocess_env


def test_cli_direct_oauth_error_stops_before_child_start(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential discovery failures produce a clean, secret-free CLI exit."""

    token = "oauth-error-canary-never-log"

    async def forbidden_run(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("credential failure reached child startup")

    def fail_injection(_env: dict[str, str]) -> dict[str, str]:
        raise CopilotOAuthCredentialError("synthetic credential failure")

    monkeypatch.setattr(cli, "_configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "load_config", dict)
    monkeypatch.setattr(cli, "build_subprocess_env", lambda _cfg: {"CANARY": token})
    monkeypatch.setattr(cli, "inject_prior_copilot_oauth", fail_injection)
    monkeypatch.setattr(cli, "run", forbidden_run)
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acp-proxy",
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "trusted-host",
            "--binary",
            "/synthetic/copilot-language-server",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 1
    assert "synthetic credential failure" in caplog.text
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_legacy_mode_shares_the_global_binary_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deprecation does not create a weaker language-server admission path."""

    old_binary = _old_version_executable(tmp_path)
    monkeypatch.setattr(cli, "admit_compatible_binary", discovery.admit_compatible_binary)

    required_version = _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
    with pytest.raises(
        BinaryCompatibilityError,
        match=re.escape(required_version),
    ):
        await cli.run(
            old_binary,
            8765,
            "/workspace",
            consumer_mode="opencode-legacy",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("catalog_model_ids", "catalog_default"),
    [
        (["model"], None),
        ([], "model"),
        (["other-model"], "model"),
    ],
)
async def test_direct_readiness_requires_usable_catalog_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog_model_ids: list[str],
    catalog_default: str | None,
) -> None:
    """A catalog session without a usable current model cannot become ready."""

    observed: dict[str, Any] = {"server_constructed": False}

    class IncompleteCatalogClient:
        def __init__(self, _binary: str, **_kwargs: Any) -> None:
            self.models = [
                SimpleNamespace(model_id=model_id) for model_id in catalog_model_ids
            ]
            self.default_model = catalog_default
            self.stopped = False
            observed["client"] = self

        def on_transport_closed(self, _handler: Any) -> None:
            return None

        async def start(self, env: dict[str, str] | None = None) -> None:
            return None

        async def create_session(self, cwd: str) -> str:
            observed["catalog_cwd"] = cwd
            return "catalog-session"

        async def stop(self) -> None:
            self.stopped = True

    class ForbiddenServer:
        def __init__(self, _config: Any) -> None:
            observed["server_constructed"] = True

    metadata = tmp_path / "ready.json"
    monkeypatch.setattr(cli, "AcpClient", IncompleteCatalogClient)
    monkeypatch.setattr(cli.uvicorn, "Server", ForbiddenServer)

    with pytest.raises(BinaryCompatibilityError) as exc_info:
        await cli.run(
            "/fake/copilot-language-server",
            8765,
            str(tmp_path),
            consumer_mode="meadow-direct",
            launch_secret="s" * 48,
            execution_authority="trusted-host",
            metadata_file=str(metadata),
        )

    message = str(exc_info.value)
    assert _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION) in message
    assert "advertised usable default model" in message
    assert "gpt-" not in message
    assert observed["server_constructed"] is False
    assert not metadata.exists()
    assert observed["client"].stopped is True


@pytest.mark.asyncio
async def test_direct_readiness_follows_strategy_negotiation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The startup binding strategy settles before HTTP can become ready."""

    order: list[str] = []
    metadata = tmp_path / "ready.json"

    class ProvenCatalogClient:
        def __init__(self, _binary: str, **kwargs: Any) -> None:
            self.callback_policy = kwargs["callback_policy"]
            self.models = [SimpleNamespace(model_id="catalog-model")]
            self.default_model = "catalog-model"
            self.protocol_version = 1
            self.agent_info = {"name": "fake", "version": "1"}
            self.agent_capabilities: dict[str, Any] = {}

        def on_transport_closed(self, _handler: Any) -> None:
            return None

        async def start(self, env: dict[str, str] | None = None) -> None:
            order.append("child-start")

        async def create_session(self, cwd: str) -> str:
            order.append("catalog-session")
            return "catalog-session"

        async def negotiate_direct_model_binding(
            self, session_id: str, model_id: str
        ) -> None:
            assert session_id == "catalog-session"
            assert model_id == "catalog-model"
            order.append("strategy-negotiated")

        async def stop(self) -> None:
            order.append("child-stop")

    class FakeSocket:
        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 8765)

    class OrderedServer:
        def __init__(self, _config: Any) -> None:
            order.append("http-constructed")
            self.servers = [SimpleNamespace(sockets=[FakeSocket()])]
            self.should_exit = False

        async def startup(self) -> None:
            order.append("http-startup")

        async def main_loop(self) -> None:
            assert metadata.exists()
            order.append("ready")

        async def shutdown(self) -> None:
            order.append("http-shutdown")

    class FakeSignalLoop:
        def add_signal_handler(self, *_args: Any) -> None:
            return None

    async def fake_app(
        _scope: dict[str, Any], _receive: Any, _send: Any
    ) -> None:
        return None

    monkeypatch.setattr(cli, "AcpClient", ProvenCatalogClient)
    monkeypatch.setattr(cli, "create_direct_app", lambda _service: fake_app)
    monkeypatch.setattr(cli.uvicorn, "Server", OrderedServer)
    monkeypatch.setattr(cli.asyncio, "get_event_loop", lambda: FakeSignalLoop())

    await cli.run(
        "/fake/copilot-language-server",
        8765,
        str(tmp_path),
        consumer_mode="meadow-direct",
        launch_secret="s" * 48,
        execution_authority="trusted-host",
        metadata_file=str(metadata),
    )

    assert order.index("catalog-session") < order.index("http-constructed")
    assert order.index("catalog-session") < order.index("strategy-negotiated")
    assert order.index("strategy-negotiated") < order.index("http-constructed")
    assert order.index("http-startup") < order.index("ready")
    assert "session/prompt" not in order


@pytest.mark.asyncio
async def test_direct_strategy_negotiation_failure_prevents_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No usable binding strategy means no HTTP server or readiness metadata."""

    observed: dict[str, Any] = {"server_constructed": False}

    class FailingStrategyClient:
        def __init__(self, _binary: str, **_kwargs: Any) -> None:
            self.models = [SimpleNamespace(model_id="catalog-model")]
            self.default_model = "catalog-model"
            self.stopped = False
            observed["client"] = self

        def on_transport_closed(self, _handler: Any) -> None:
            return None

        async def start(self, env: dict[str, str] | None = None) -> None:
            return None

        async def create_session(self, cwd: str) -> str:
            return "catalog-session"

        async def negotiate_direct_model_binding(
            self, session_id: str, model_id: str
        ) -> None:
            raise ModelAcknowledgementError("private child selector detail")

        async def stop(self) -> None:
            self.stopped = True

    class ForbiddenServer:
        def __init__(self, _config: Any) -> None:
            observed["server_constructed"] = True

    metadata = tmp_path / "ready.json"
    monkeypatch.setattr(cli, "AcpClient", FailingStrategyClient)
    monkeypatch.setattr(cli.uvicorn, "Server", ForbiddenServer)

    with pytest.raises(BinaryCompatibilityError) as exc_info:
        await cli.run(
            "/fake/copilot-language-server",
            8765,
            str(tmp_path),
            consumer_mode="meadow-direct",
            launch_secret="s" * 48,
            execution_authority="trusted-host",
            metadata_file=str(metadata),
        )

    message = str(exc_info.value)
    assert "supported session model binding strategy" in message
    assert "private child selector detail" not in message
    assert observed["server_constructed"] is False
    assert observed["client"].stopped is True
    assert not metadata.exists()


@pytest.mark.asyncio
async def test_direct_strategy_negotiation_timeout_prevents_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-responsive selector is bounded before HTTP readiness."""

    observed: dict[str, Any] = {"server_constructed": False}

    class HangingStrategyClient:
        def __init__(self, _binary: str, **_kwargs: Any) -> None:
            self.models = [SimpleNamespace(model_id="catalog-model")]
            self.default_model = "catalog-model"
            self.stopped = False
            observed["client"] = self

        def on_transport_closed(self, _handler: Any) -> None:
            return None

        async def start(self, env: dict[str, str] | None = None) -> None:
            return None

        async def create_session(self, cwd: str) -> str:
            return "catalog-session"

        async def negotiate_direct_model_binding(
            self, session_id: str, model_id: str
        ) -> None:
            await asyncio.Event().wait()

        async def stop(self) -> None:
            self.stopped = True

    class ForbiddenServer:
        def __init__(self, _config: Any) -> None:
            observed["server_constructed"] = True

    metadata = tmp_path / "ready.json"
    monkeypatch.setattr(cli, "AcpClient", HangingStrategyClient)
    monkeypatch.setattr(cli.uvicorn, "Server", ForbiddenServer)

    with pytest.raises(BinaryCompatibilityError, match="bounded startup"):
        await cli.run(
            "/fake/copilot-language-server",
            8765,
            str(tmp_path),
            consumer_mode="meadow-direct",
            launch_secret="s" * 48,
            execution_authority="trusted-host",
            metadata_file=str(metadata),
            direct_limits=DirectLimits(session_creation_timeout_s=0.01),
        )

    assert observed["server_constructed"] is False
    assert observed["client"].stopped is True
    assert not metadata.exists()


def test_consumer_mode_is_mandatory() -> None:
    """ADI-12: the proxy never guesses direct versus legacy caller semantics."""
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args([])


def test_cli_invalid_direct_config_never_probes_auto_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid authority fails before any candidate executes ``--version``."""

    def forbidden_discovery() -> str | None:
        raise AssertionError("invalid configuration reached version discovery")

    monkeypatch.delenv(cli.DIRECT_SECRET_ENV, raising=False)
    monkeypatch.setattr(cli, "_configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "find_binary", forbidden_discovery)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acp-proxy",
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "trusted-host",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 2


def test_cli_auto_discovery_reports_old_only_environment_without_traceback(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-discovery converts typed old-only evidence into a clean CLI exit."""

    observed_version = _version_text(
        _version_before(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
    )
    required_version = _version_text(MIN_COPILOT_LANGUAGE_SERVER_VERSION)

    def reject_old_only() -> str | None:
        raise BinaryCompatibilityError(
            "no auto-discovered copilot-language-server met admission requirements: "
            f"version {observed_version} is below required minimum {required_version}"
        )

    monkeypatch.setattr(cli, "_configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "find_binary", reject_old_only)
    monkeypatch.setattr(
        sys,
        "argv",
        ["acp-proxy", "--consumer-mode", "opencode-legacy"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert observed_version in caplog.text
    assert required_version in caplog.text


@pytest.mark.asyncio
async def test_programmatic_run_requires_explicit_consumer_mode() -> None:
    """ADI-12: non-CLI callers cannot inherit a compatibility mode default."""
    with pytest.raises(TypeError, match="consumer_mode"):
        await cli.run("/fake/copilot-language-server", 8765, "/workspace")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_kwargs", "message"),
    [
        (
            {
                "consumer_mode": "meadow-direct",
                "host": "0.0.0.0",
                "launch_secret": "s" * 48,
                "execution_authority": "trusted-host",
            },
            "loopback",
        ),
        (
            {
                "consumer_mode": "meadow-direct",
                "launch_secret": "short",
                "execution_authority": "trusted-host",
            },
            "32 bytes",
        ),
        (
            {
                "consumer_mode": "meadow-direct",
                "launch_secret": "s" * 48,
                "execution_authority": "trusted-host",
                "system_prompt": "proxy text",
            },
            "proxy-authored",
        ),
    ],
)
async def test_programmatic_invalid_direct_startup_fails_before_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_kwargs: dict[str, Any],
    message: str,
) -> None:
    """ADI-02/09/12/15: shared startup enforces direct policy pre-process."""
    constructed = False

    class ForbiddenClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            nonlocal constructed
            constructed = True

    monkeypatch.setattr(cli, "AcpClient", ForbiddenClient)
    monkeypatch.setattr(
        cli,
        "admit_compatible_binary",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("invalid configuration reached version admission")
        ),
    )

    with pytest.raises(ValueError, match=message):
        await cli.run(
            "/fake/copilot-language-server",
            8765,
            str(tmp_path),
            **run_kwargs,
        )
    assert constructed is False


@pytest.mark.asyncio
async def test_post_start_catalog_failure_still_stops_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADI-13: every failure after child start crosses the owned cleanup boundary."""
    observed: dict[str, Any] = {}

    class FailingCatalogClient:
        def __init__(self, _binary: str, **_kwargs: Any) -> None:
            observed["client"] = self
            self.stopped = False

        def on_transport_closed(self, _handler: Any) -> None:
            return None

        async def start(self, env: dict[str, str] | None = None) -> None:
            return None

        async def create_session(self, cwd: str) -> str:
            raise RuntimeError("catalog probe failed")

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(cli, "AcpClient", FailingCatalogClient)

    with pytest.raises(RuntimeError, match="catalog probe failed"):
        await cli.run(
            "/fake/copilot-language-server",
            8765,
            str(tmp_path),
            consumer_mode="opencode-legacy",
        )

    assert observed["client"].stopped is True


@pytest.mark.asyncio
async def test_child_loss_during_startup_invalidates_direct_service_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADI-10/13: a dead ACP child cannot leave direct readiness or an orphan."""
    observed: dict[str, Any] = {}

    class ChildLossClient:
        def __init__(self, _binary: str, **kwargs: Any) -> None:
            self.callback_policy = kwargs["callback_policy"]
            self.models = [SimpleNamespace(model_id="gpt-5.3-codex")]
            self.default_model = "gpt-5.3-codex"
            self.protocol_version = 1
            self.agent_info = {"name": "fake", "version": "1"}
            self.agent_capabilities = {}
            self.is_alive = True
            self.stopped = False
            self.close_handler: Any = None
            observed["client"] = self

        def on_transport_closed(self, handler: Any) -> None:
            self.close_handler = handler

        async def start(self, env: dict[str, str] | None = None) -> None:
            return None

        async def create_session(self, cwd: str) -> str:
            return "catalog-session"

        async def negotiate_direct_model_binding(
            self, session_id: str, model_id: str
        ) -> None:
            return None

        async def stop(self) -> None:
            self.stopped = True

    class FakeServer:
        def __init__(self, _config: Any) -> None:
            self.servers = [
                SimpleNamespace(
                    sockets=[SimpleNamespace(getsockname=lambda: ("127.0.0.1", 8765))]
                )
            ]
            self.should_exit = False
            self.shutdown_called = False

        async def startup(self) -> None:
            observed["client"].is_alive = False
            observed["client"].close_handler()

        async def shutdown(self) -> None:
            self.shutdown_called = True
            observed["server"] = self

    class FakeSignalLoop:
        def add_signal_handler(self, *_args: Any) -> None:
            return None

    async def fake_app(
        _scope: dict[str, Any], _receive: Any, _send: Any
    ) -> None:
        return None

    def capture_service(service: Any) -> Any:
        observed["service"] = service
        return fake_app

    monkeypatch.setattr(cli, "AcpClient", ChildLossClient)
    monkeypatch.setattr(cli, "create_direct_app", capture_service)
    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(cli.asyncio, "get_event_loop", lambda: FakeSignalLoop())

    with pytest.raises(ConnectionError, match="HTTP startup"):
        await cli.run(
            "/fake/copilot-language-server",
            8765,
            str(tmp_path),
            consumer_mode="meadow-direct",
            launch_secret="s" * 48,
            execution_authority="trusted-host",
        )

    with pytest.raises(Exception, match="managed restart is required"):
        _ = observed["service"].capabilities
    assert observed["client"].stopped is True
    assert observed["server"].shutdown_called is True


@pytest.mark.asyncio
async def test_graceful_owner_shutdown_quarantines_active_direct_work_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADI-10/13: normal shutdown marks active work in_doubt before child stop."""
    observed: dict[str, Any] = {}

    class ActiveClient:
        def __init__(self, _binary: str, **kwargs: Any) -> None:
            self.callback_policy = kwargs["callback_policy"]
            self.models = [SimpleNamespace(model_id="gpt-5.3-codex")]
            self.default_model = "gpt-5.3-codex"
            self.protocol_version = 1
            self.agent_info = {"name": "fake", "version": "1"}
            self.agent_capabilities = {}
            self.is_alive = True
            self.prompt_started = asyncio.Event()
            self.prompt_release = asyncio.Event()
            self.stopped = False

        def on_transport_closed(self, _handler: Any) -> None:
            return None

        async def start(self, env: dict[str, str] | None = None) -> None:
            return None

        async def create_session(self, cwd: str) -> str:
            return "catalog"

        async def negotiate_direct_model_binding(
            self, session_id: str, model_id: str
        ) -> None:
            return None

        async def create_session_exact(self, cwd: str, model_id: str) -> Any:
            return SimpleNamespace(session_id="backend", model_id=model_id)

        async def prompt_blocks(
            self,
            session_id: str,
            blocks: list[dict[str, str]],
            *,
            timeout_s: float,
            event_byte_limit: int,
            event_count_limit: int,
        ) -> Any:
            self.prompt_started.set()
            await self.prompt_release.wait()
            yield {"done": True, "stopReason": "end_turn"}

        async def cancel_session(self, session_id: str) -> None:
            return None

        async def stop(self) -> None:
            self.stopped = True
            self.prompt_release.set()

    class FakeServer:
        def __init__(self, _config: Any) -> None:
            self.servers = [
                SimpleNamespace(
                    sockets=[SimpleNamespace(getsockname=lambda: ("127.0.0.1", 8765))]
                )
            ]
            self.should_exit = False

        async def startup(self) -> None:
            return None

        async def main_loop(self) -> None:
            service = observed["service"]
            stable_digest = hashlib.sha256(b"").hexdigest()
            contract_digest = hashlib.sha256(b"contract").hexdigest()
            create_record, _ = await service.admit_create(
                CreateSessionRequest(
                    protocol_major=1,
                    continuity_generation_id=service.continuity_generation_id,
                    operation_id="create",
                    logical_session_id="session",
                    expected_canonical_workspace=service.canonical_workspace,
                    actor_ref="actor",
                    title="Actor",
                    model_id="gpt-5.3-codex",
                    stable_instruction_digest=stable_digest,
                )
            )
            await service.wait_for_operation(create_record)
            prompt_record, _ = await service.admit_prompt(
                "session",
                PromptRequest(
                    protocol_major=1,
                    continuity_generation_id=service.continuity_generation_id,
                    operation_id="prompt",
                    invocation_id="invocation",
                    phase="initial",
                    stable_instruction_digest=stable_digest,
                    output_contract_digest=contract_digest,
                    execution_timeout_s=30,
                    stable_instructions="",
                    prompt="prompt with routes",
                    output_contract="contract",
                ),
            )
            observed["prompt_record"] = prompt_record
            observed["old_session"] = service._generation.sessions["session"]
            await observed["client"].prompt_started.wait()

        async def shutdown(self) -> None:
            observed["shutdown"] = True

    class FakeSignalLoop:
        def add_signal_handler(self, *_args: Any) -> None:
            return None

    async def fake_app(
        _scope: dict[str, Any], _receive: Any, _send: Any
    ) -> None:
        return None

    def make_client(*args: Any, **kwargs: Any) -> ActiveClient:
        client = ActiveClient(*args, **kwargs)
        observed["client"] = client
        return client

    def capture_service(service: Any) -> Any:
        observed["service"] = service
        return fake_app

    monkeypatch.setattr(cli, "AcpClient", make_client)
    monkeypatch.setattr(cli, "create_direct_app", capture_service)
    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(cli.asyncio, "get_event_loop", lambda: FakeSignalLoop())

    await cli.run(
        "/fake/copilot-language-server",
        8765,
        str(tmp_path),
        consumer_mode="meadow-direct",
        launch_secret="s" * 48,
        execution_authority="trusted-host",
    )

    assert observed["prompt_record"].state.value == "in_doubt"
    assert observed["old_session"].state.value == "lost"
    assert observed["client"].stopped is True
    assert observed["shutdown"] is True


def test_trusted_host_direct_rejects_non_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADI-02/15: host direct mode cannot expose authenticated HTTP publicly."""
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    args = cli._build_parser().parse_args(
        [
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "trusted-host",
            "--host",
            "0.0.0.0",
        ]
    )
    with pytest.raises(ValueError, match="loopback"):
        cli._validate_mode_options(args)


def test_confined_container_direct_accepts_declared_private_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADI-02/09/13/15: managed container mode may bind inside its namespace."""
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    monkeypatch.setenv(cli.CONTAINER_BOUNDARY_ENV, "1")
    monkeypatch.setattr(cli, "_has_observable_container_boundary", lambda: True)
    args = cli._build_parser().parse_args(
        [
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "confined-container",
            "--host",
            "0.0.0.0",
        ]
    )
    assert cli._validate_mode_options(args) == "s" * 48


def test_container_env_claim_without_runtime_boundary_fails_pre_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADI-02/09/15: a caller-controlled environment bit is not confinement proof."""
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    monkeypatch.setenv(cli.CONTAINER_BOUNDARY_ENV, "1")
    monkeypatch.setattr(cli, "_has_observable_container_boundary", lambda: False)
    args = cli._build_parser().parse_args(
        [
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "confined-container",
            "--host",
            "0.0.0.0",
        ]
    )
    with pytest.raises(ValueError, match="observable container runtime"):
        cli._validate_mode_options(args)


@pytest.mark.parametrize("authority", ["trusted-host", "confined-container"])
def test_direct_child_environment_allows_runtime_and_github_namespaces(
    authority: str,
) -> None:
    """ADI-09/15: children receive GitHub but not unrelated credentials."""
    canary = "canary-secret-must-not-cross"
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/worker",
        "XDG_CONFIG_HOME": "/config",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "UTF-8",
        "HTTPS_PROXY": "http://required-proxy",
        "SSL_CERT_FILE": "/cert.pem",
        "GITHUB_COPILOT_ENTERPRISE_URI": "https://github.example",
        "GH_COPILOT_TOKEN": "gh-copilot-credential",
        "GITHUB_COPILOT_TOKEN": "github-copilot-credential",
        "GH_TOKEN": "gh-general-credential",
        "GITHUB_TOKEN": "github-general-credential",
        "github_actions": "true",
        "ghost_setting": "literal-gh-prefix",
        "SYSTEMROOT": r"C:\Windows",
        "appdata": r"C:\Users\worker\AppData\Roaming",
        cli.DIRECT_SECRET_ENV: canary,
        cli.CONTAINER_BOUNDARY_ENV: "1",
        "MEADOW_OPENAI_API_KEY": canary,
        "OPENAI_API_KEY": canary,
        "MOONSHOT_API_KEY": canary,
        "XGITHUB_TOKEN": canary,
        "UNRELATED_TOKEN": canary,
        "UNRELATED_SECRET": canary,
    }

    child = cli._direct_child_env(source)

    assert set(child) == {
        "PATH",
        "HOME",
        "XDG_CONFIG_HOME",
        "LANG",
        "LC_CTYPE",
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
        "GITHUB_COPILOT_ENTERPRISE_URI",
        "GH_COPILOT_TOKEN",
        "GITHUB_COPILOT_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "github_actions",
        "ghost_setting",
        "SYSTEMROOT",
        "appdata",
    }
    assert child["GH_COPILOT_TOKEN"] == "gh-copilot-credential"
    assert child["GITHUB_COPILOT_TOKEN"] == "github-copilot-credential"
    assert child["GH_TOKEN"] == "gh-general-credential"
    assert child["GITHUB_TOKEN"] == "github-general-credential"
    assert canary not in child.values()
    assert authority in {"trusted-host", "confined-container"}


def test_direct_mode_rejects_proxy_authored_prompt_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADI-06/12: legacy prompt injection cannot enter Meadow direct mode."""
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    args = cli._build_parser().parse_args(
        [
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "trusted-host",
            "--system-prompt",
            "/tmp/legacy.md",
        ]
    )
    with pytest.raises(ValueError, match="rejects"):
        cli._validate_mode_options(args)
