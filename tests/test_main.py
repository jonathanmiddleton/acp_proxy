"""Tests for the acp-proxy command-line entry point and server wiring."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from acp_proxy import __main__ as cli
from acp_proxy.direct_protocol import CreateSessionRequest, PromptRequest


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
    assert metadata["host"] == "0.0.0.0"


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


def test_consumer_mode_is_mandatory() -> None:
    """ADI-12: the proxy never guesses direct versus legacy caller semantics."""
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args([])


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
def test_direct_child_environment_is_an_exact_least_credential_allowlist(
    authority: str,
) -> None:
    """ADI-09/15: host and container children receive no unrelated credentials."""
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
        cli.DIRECT_SECRET_ENV: canary,
        cli.CONTAINER_BOUNDARY_ENV: "1",
        "MEADOW_OPENAI_API_KEY": canary,
        "OPENAI_API_KEY": canary,
        "MOONSHOT_API_KEY": canary,
        "GITHUB_TOKEN": canary,
        "GH_TOKEN": canary,
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
    }
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
