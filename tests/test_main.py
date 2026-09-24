"""CLI admission, native readiness, and owned service shutdown boundaries."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest
import uvicorn

from meadow_bridge import __main__ as cli
from meadow_bridge import discovery
from meadow_bridge.application_policy import MIN_COPILOT_LANGUAGE_SERVER_VERSION
from meadow_bridge.copilot_auth import CopilotOAuthCredentialError
from meadow_bridge.direct_protocol import DirectLimits
from meadow_bridge.discovery import BinaryAdmission, BinaryCompatibilityError
from meadow_bridge.json_types import json_object, parse_json
from meadow_bridge.native_types import NativeModel, NativeServerInfo
from meadow_bridge.native_transport import NativeProtocolError
from meadow_bridge.owned_commands import ShellSpec


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
        ([], "127.0.0.1"),
        (
            ["--host", "0.0.0.0"],
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
    assert metadata["protocol_major"] == 2
    assert "consumer_mode" not in metadata



def test_raw_capture_cli_is_explicit(tmp_path: Path) -> None:
    parser = cli._build_parser()
    base: list[str] = []
    assert parser.parse_args(base).raw_event_file is None
    path = str(tmp_path / "events.jsonl")
    assert parser.parse_args([*base, "--raw-event-file", path]).raw_event_file == path


@pytest.mark.parametrize("platform_name", ["win32", "darwin"])
def test_shell_selection_requires_one_supported_executable(
    monkeypatch: pytest.MonkeyPatch, platform_name: str,
) -> None:
    monkeypatch.setattr(sys, "platform", platform_name)
    parser = cli._build_parser()
    assert parser.parse_args([]).shell is None
    executable = "pwsh.exe" if platform_name == "win32" else "/bin/zsh"
    assert parser.parse_args(["--shell", executable]).shell == ShellSpec(executable)
    with pytest.raises(SystemExit) as error_info:
        parser.parse_args(["--shell", "unsupported-shell"])
    assert error_info.value.code == 2



def test_windows_shutdown_handles_ctrl_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A targeted Windows process group can shut down through CTRL+BREAK."""

    registered: dict[object, Callable[[object, object], None]] = {}
    shutdowns: list[str] = []

    def forbidden_posix_handler(*_args: object) -> None:
        raise AssertionError("Windows must use synchronous signal handlers")

    def register_handler(
        sig: object, callback: Callable[[object, object], None]
    ) -> None:
        registered[sig] = callback

    sigbreak = object()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(signal, "SIGBREAK", sigbreak, raising=False)
    monkeypatch.setattr(
        signal,
        "signal",
        register_handler,
    )

    loop = asyncio.new_event_loop()
    monkeypatch.setattr(loop, "add_signal_handler", forbidden_posix_handler)
    try:
        cli._install_shutdown_signal_handlers(
            loop, lambda: shutdowns.append("shutdown"),
        )
    finally:
        loop.close()

    assert set(registered) == {signal.SIGINT, sigbreak}
    registered[sigbreak](sigbreak, None)
    assert shutdowns == ["shutdown"]



@pytest.mark.asyncio
async def test_programmatic_run_rejects_old_binary_before_client_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The programmatic entry point cannot bypass production admission."""

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("old binary reached NativeClient construction")

    old_binary = _old_version_executable(tmp_path)
    monkeypatch.setattr(cli, "admit_compatible_binary", discovery.admit_compatible_binary)
    monkeypatch.setattr(cli, "NativeClient", ForbiddenClient)

    old_version = _version_text(
        _version_before(MIN_COPILOT_LANGUAGE_SERVER_VERSION)
    )
    with pytest.raises(BinaryCompatibilityError, match=re.escape(old_version)):
        await cli.run(
            old_binary,
            8765,
            "/workspace",
            launch_secret="s" * 48,
            execution_authority="trusted-host",
        )



def test_cli_explicit_old_binary_fails_before_client_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI ``--binary`` path crosses the same real version boundary."""

    class ForbiddenClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("old binary reached NativeClient construction")

    old_binary = _old_version_executable(tmp_path)
    monkeypatch.setattr(cli, "admit_compatible_binary", discovery.admit_compatible_binary)
    monkeypatch.setattr(cli, "NativeClient", ForbiddenClient)
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
            "meadow-bridge",
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
    """Discovered OAuth reaches the server without becoming a command credential."""

    observed: dict[str, object] = {}
    token = "oauth-main-canary-never-log"
    launch_secret = "s" * 48

    def fake_inject(env: dict[str, str]) -> dict[str, str]:
        child_env = dict(env)
        child_env["GITHUB_COPILOT_TOKEN"] = token
        return child_env

    async def fake_run(*_args: object, **kwargs: object) -> None:
        observed.update(kwargs)

    monkeypatch.setattr(cli, "_configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "load_config", dict)
    monkeypatch.setattr(
        cli,
        "build_subprocess_env",
        lambda _cfg: {
            "PATH": "synthetic-path",
            "GH_TOKEN": "explicit-inherited-token",
            "HTTPS_PROXY": "http://configured-proxy",
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
            "meadow-bridge",
            "--execution-authority",
            "trusted-host",
            "--binary",
            "/synthetic/copilot-language-server",
        ],
    )

    cli.main()

    subprocess_env = observed["subprocess_env"]
    assert isinstance(subprocess_env, dict)
    assert subprocess_env["GITHUB_COPILOT_TOKEN"] == token
    assert subprocess_env["PATH"] == "synthetic-path"
    assert cli.DIRECT_SECRET_ENV not in subprocess_env
    command_env = observed["command_env"]
    assert isinstance(command_env, dict)
    assert "GITHUB_COPILOT_TOKEN" not in command_env
    assert command_env["GH_TOKEN"] == "explicit-inherited-token"
    assert command_env["HTTPS_PROXY"] == "http://configured-proxy"
    assert command_env["PATH"] == "synthetic-path"
    assert cli.DIRECT_SECRET_ENV not in command_env



def test_cli_direct_oauth_error_stops_before_child_start(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential discovery failures produce a clean, secret-free CLI exit."""

    token = "oauth-error-canary-never-log"

    async def forbidden_run(*_args: object, **_kwargs: object) -> None:
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
            "meadow-bridge",
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
            "meadow-bridge",
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

    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    monkeypatch.setattr(cli, "_configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "find_binary", reject_old_only)
    monkeypatch.setattr(
        sys,
        "argv",
        ["meadow-bridge", "--execution-authority", "trusted-host"],
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1
    assert observed_version in caplog.text
    assert required_version in caplog.text



def test_trusted_host_direct_rejects_non_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADI-02/15: host direct mode cannot expose authenticated HTTP publicly."""
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    args = cli._build_parser().parse_args(
        [
            "--execution-authority",
            "trusted-host",
            "--host",
            "0.0.0.0",
        ]
    )
    with pytest.raises(ValueError, match="loopback"):
        cli._validate_options(args)



def test_confined_container_direct_accepts_declared_private_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADI-02/09/13/15: managed container mode may bind inside its namespace."""
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    monkeypatch.setenv(cli.CONTAINER_BOUNDARY_ENV, "1")
    monkeypatch.setattr(cli, "_has_observable_container_boundary", lambda: True)
    args = cli._build_parser().parse_args(
        [
            "--execution-authority",
            "confined-container",
            "--host",
            "0.0.0.0",
        ]
    )
    assert cli._validate_options(args) == "s" * 48



def test_container_env_claim_without_runtime_boundary_fails_pre_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADI-02/09/15: a caller-controlled environment bit is not confinement proof."""
    monkeypatch.setenv(cli.DIRECT_SECRET_ENV, "s" * 48)
    monkeypatch.setenv(cli.CONTAINER_BOUNDARY_ENV, "1")
    monkeypatch.setattr(cli, "_has_observable_container_boundary", lambda: False)
    args = cli._build_parser().parse_args(
        [
            "--execution-authority",
            "confined-container",
            "--host",
            "0.0.0.0",
        ]
    )
    with pytest.raises(ValueError, match="observable container runtime"):
        cli._validate_options(args)



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


class _NativeStub:
    """Controlled native I/O boundary; no conversation methods are available."""

    def __init__(self) -> None:
        self.models: tuple[NativeModel, ...] = (
            NativeModel("fixture-model", "Fixture Model"),
        )
        self.server_info = NativeServerInfo("Fixture Language Server", "1.545.1")
        self.is_alive = True
        self.started = False
        self.stopped = False
        self.start_blocked = False
        self.start_failure: Exception | None = None
        self.stop_blocked = False
        self.stop_started = asyncio.Event()
        self.stop_release = asyncio.Event()
        self.close_handler: Callable[[str], None] | None = None
        self.child_env: dict[str, str] = {}

    def on_transport_closed(self, handler: Callable[[str], None]) -> None:
        self.close_handler = handler

    async def start(self, env: Mapping[str, str] | None = None) -> None:
        self.child_env = dict(env or {})
        self.started = True
        if self.start_failure is not None:
            raise self.start_failure
        if self.start_blocked:
            await asyncio.Event().wait()

    async def stop(self) -> None:
        self.stop_started.set()
        if self.stop_blocked:
            await self.stop_release.wait()
        self.stopped = True
        self.is_alive = False

    async def abort(self) -> None:
        await self.stop()

    def lose_child(self) -> None:
        self.is_alive = False
        assert self.close_handler is not None
        self.close_handler("private provider detail must not become a public error")


class _Bootstrap:
    """Capture only external lifecycle controls while using the real HTTP server."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.client = _NativeStub()
        self.metadata_path = tmp_path / "ready.json"
        self.shutdown: Callable[[], None] | None = None
        self.raw_event_file: str | None = None
        self.workspace: Path | None = None
        self.shell: ShellSpec | None = None
        self.command_env: dict[str, str] | None = None

        def make_client(
            _binary: str,
            *,
            cwd: str | Path,
            raw_event_file: str | None = None,
            shell: ShellSpec | None = None,
            command_env: Mapping[str, str] | None = None,
        ) -> _NativeStub:
            self.workspace = Path(cwd)
            self.raw_event_file = raw_event_file
            self.shell = shell
            self.command_env = None if command_env is None else dict(command_env)
            return self.client

        def install_signals(
            _loop: asyncio.AbstractEventLoop, handler: Callable[[], None]
        ) -> None:
            self.shutdown = handler

        monkeypatch.setattr(cli, "NativeClient", make_client)
        monkeypatch.setattr(cli, "_install_shutdown_signal_handlers", install_signals)

    async def ready(self, task: asyncio.Task[None]) -> dict[str, object]:
        async with asyncio.timeout(5):
            while not self.metadata_path.exists():
                if task.done():
                    await task
                    raise AssertionError("service completed before readiness")
                await asyncio.sleep(0.001)
        value = json_object(parse_json(self.metadata_path.read_text(encoding="utf-8")))
        return dict(value)

    def request_shutdown(self) -> None:
        assert self.shutdown is not None
        self.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty-catalog", "timeout", "protocol"])
async def test_native_startup_admission_is_bounded_before_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bootstrap = _Bootstrap(monkeypatch, tmp_path)
    if failure == "empty-catalog":
        bootstrap.client.models = ()
    elif failure == "timeout":
        bootstrap.client.start_blocked = True
    else:
        bootstrap.client.start_failure = NativeProtocolError("private provider detail")

    def forbidden_server(_config: uvicorn.Config) -> uvicorn.Server:
        raise AssertionError("unadmitted native client reached HTTP startup")

    monkeypatch.setattr(uvicorn, "Server", forbidden_server)
    with pytest.raises(BinaryCompatibilityError, match="native") as error_info:
        await cli.run(
            "/fixture/copilot-language-server", 0, str(tmp_path),
            launch_secret="s" * 48, execution_authority="trusted-host",
            metadata_file=str(bootstrap.metadata_path),
            direct_limits=DirectLimits(session_creation_timeout_s=0.01),
        )
    assert bootstrap.client.started
    assert "private provider detail" not in str(error_info.value)
    assert bootstrap.client.stopped
    assert not bootstrap.metadata_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["signal", "child-loss", "task-cancel"])
async def test_native_readiness_and_shutdown_use_owned_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    bootstrap = _Bootstrap(monkeypatch, tmp_path)
    capture_path = str(tmp_path / "native-events.jsonl")
    shell = ShellSpec("powershell.exe" if sys.platform == "win32" else "/bin/sh")
    task = asyncio.create_task(cli.run(
        "/fixture/copilot-language-server", 0, str(tmp_path),
        launch_secret="s" * 48, execution_authority="trusted-host",
        metadata_file=str(bootstrap.metadata_path), raw_event_file=capture_path,
        shell=shell,
        command_env={"PATH": "explicit-command-path"},
        subprocess_env={
            "PATH": os.defpath,
            "GITHUB_COPILOT_ACP_USE_CLI": "1",
            "GITHUB_COPILOT_TOKEN": "fixture-token",
            cli.DIRECT_SECRET_ENV: "must-not-cross",
        },
    ))
    try:
        metadata = await bootstrap.ready(task)
        assert metadata["protocol_major"] == 2
        assert "consumer_mode" not in metadata
        port = metadata["port"]
        assert isinstance(port, int) and port > 0
        assert bootstrap.workspace == tmp_path
        assert bootstrap.raw_event_file == capture_path
        assert bootstrap.shell == shell
        assert bootstrap.command_env == {"PATH": "explicit-command-path"}
        assert bootstrap.client.child_env["GITHUB_COPILOT_ACP_USE_CLI"] == "0"
        assert bootstrap.client.child_env["GITHUB_COPILOT_TOKEN"] == "fixture-token"
        assert cli.DIRECT_SECRET_ENV not in bootstrap.client.child_env
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{port}/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "protocol_major": 2}
        if finish == "signal":
            bootstrap.request_shutdown()
        elif finish == "child-loss":
            bootstrap.client.lose_child()
        else:
            task.cancel()
        if finish == "task-cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bootstrap.client.stopped
    assert not bootstrap.metadata_path.exists()


@pytest.mark.asyncio
async def test_shutdown_does_not_return_until_native_resources_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = _Bootstrap(monkeypatch, tmp_path)
    bootstrap.client.stop_blocked = True
    task = asyncio.create_task(cli.run(
        "/fixture/copilot-language-server", 0, str(tmp_path),
        launch_secret="s" * 48, execution_authority="trusted-host",
        metadata_file=str(bootstrap.metadata_path),
    ))
    try:
        await bootstrap.ready(task)
        bootstrap.request_shutdown()
        await asyncio.wait_for(bootstrap.client.stop_started.wait(), 5)
        assert not task.done()
        bootstrap.client.stop_release.set()
        await asyncio.wait_for(task, 5)
    finally:
        bootstrap.client.stop_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert bootstrap.client.stopped
    assert not bootstrap.metadata_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "secret", "message"),
    [("0.0.0.0", "s" * 48, "loopback"), ("127.0.0.1", "short", "32 bytes")],
)
async def test_invalid_programmatic_startup_never_constructs_native_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    host: str, secret: str, message: str,
) -> None:
    def forbidden_client(*_args: object, **_kwargs: object) -> _NativeStub:
        raise AssertionError("invalid startup reached native construction")

    monkeypatch.setattr(cli, "NativeClient", forbidden_client)
    with pytest.raises(ValueError, match=message):
        await cli.run(
            "/fixture/copilot-language-server", 0, str(tmp_path), host=host,
            launch_secret=secret, execution_authority="trusted-host",
        )
