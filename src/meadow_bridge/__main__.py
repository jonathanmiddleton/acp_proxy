"""
Entry point for the authenticated native Copilot IDE service.

Usage:
    meadow-bridge [OPTIONS]

    Start the proxy from your project directory. The current working directory
    becomes the native workspace — the copilot-language-server scans it and scopes
    file operations to it.

    --binary PATH       Path to copilot-language-server binary.
                        Auto-discovers named JetBrains plugin binaries.
    --host HOST         Address to bind (default: 127.0.0.1).
    --port PORT         Port to listen on (default: 8765). Use 0 for ephemeral.
    --cwd PATH          Working directory for native sessions (default: current dir)
    --log-level LEVEL   Console logging level (default: WARNING)
    --log-file PATH     Log file path (default: logs/meadow-bridge.log)
    --metadata-file     Write JSON metadata (port, pid, status) after startup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import platform
import signal
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import uvicorn

from .application_policy import MIN_COPILOT_LANGUAGE_SERVER_VERSION
from .config import (
    build_subprocess_env,
    config_path,
    load_config,
)
from .copilot_auth import (
    CopilotOAuthCredentialError,
    inject_prior_copilot_oauth,
)
from .direct_protocol import DIRECT_PROTOCOL_MAJOR, DirectLimits
from .direct_server import create_direct_app
from .direct_service import DirectService
from .discovery import (
    BinaryAdmission,
    BinaryCompatibilityError,
    admit_compatible_binary,
    find_binary,
)
from .native_client import NativeClient
from .native_transport import NativeRpcError, NativeTransportError
from .owned_commands import ShellSpec
from .raw_events import RawEventCaptureError

logger = logging.getLogger(__name__)

LOG_LEVEL_ENV = "MEADOW_BRIDGE_LOG_LEVEL"
LOG_LEVEL_CHOICES = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 3
DIRECT_SECRET_ENV = "MEADOW_BRIDGE_MEADOW_SECRET"
CONTAINER_BOUNDARY_ENV = "MEADOW_BRIDGE_CONTAINER_BOUNDARY"
CONTAINER_MARKERS = ("/run/.containerenv", "/.dockerenv")
DIRECT_CHILD_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "GITHUB_COPILOT_ENTERPRISE_URI",
    }
)
DIRECT_CHILD_ENV_PREFIXES = ("LC_", "GH", "GITHUB")

def _default_log_level() -> str:
    return os.environ.get(LOG_LEVEL_ENV, "WARNING").strip().upper() or "WARNING"

def _install_shutdown_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    handler: Callable[[], None],
) -> None:
    """Install the platform signals used for graceful owned shutdown."""

    def _windows_handler(*_: object) -> None:
        handler()

    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP disables targeted CTRL+C delivery. Its
        # group-addressable graceful signal is CTRL+BREAK / SIGBREAK.
        for sig in (signal.SIGINT, signal.SIGBREAK):
            signal.signal(sig, _windows_handler)
        return

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handler)


def _has_observable_container_boundary() -> bool:
    """Return true only when this process can observe a container runtime marker."""

    return any(os.path.exists(marker) for marker in CONTAINER_MARKERS)


def _direct_child_env(source: dict[str, str]) -> dict[str, str]:
    """Build the allowlisted environment for the admitted language server."""

    return {
        key: value
        for key, value in source.items()
        if key.upper() in DIRECT_CHILD_ENV_KEYS
        or key.upper().startswith(DIRECT_CHILD_ENV_PREFIXES)
    }


def _direct_binary_capability_error(
    admission: BinaryAdmission,
    capability: str,
) -> BinaryCompatibilityError:
    """Build a versioned, model-text-safe direct compatibility diagnostic."""

    observed = ".".join(str(part) for part in admission.version)
    required = ".".join(
        str(part) for part in MIN_COPILOT_LANGUAGE_SERVER_VERSION
    )
    return BinaryCompatibilityError(
        f"copilot-language-server version {observed} meets required minimum "
        f"{required} but failed required direct capability: {capability}"
    )


def _configure_logging(console_level: str | None, log_file: str) -> None:
    """Set up dual logging: DEBUG to file (always), configurable to console."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    log_level = console_level or _default_log_level()
    # Console handler — respects --log-level
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, log_level))
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)

    # File handler — always DEBUG, with rotation
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)

    # Route uvicorn access and error logs through the same handlers
    for uv_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uv_logger_name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True


def _write_metadata_file(
    path: str,
    port: int,
    host: str = "127.0.0.1",
    *,
    protocol_major: int = DIRECT_PROTOCOL_MAJOR,
    continuity_generation_id: str | None = None,
) -> None:
    """Write a JSON metadata file with process info and readiness status.

    This file doubles as a readiness signal — its existence means the
    server is bound and accepting connections.

    Uses write-to-temp + rename for atomic creation so consumers never
    observe a partially-written file.
    """
    metadata: dict[str, int | str] = {
        "pid": os.getpid(),
        "port": port,
        "host": host,
        "status": "ready",
        "protocol_major": protocol_major,
    }
    if continuity_generation_id is not None:
        metadata["continuity_generation_id"] = continuity_generation_id
    metadata_dir = os.path.dirname(path) or "."
    os.makedirs(metadata_dir, exist_ok=True)

    tmp_fd, tmp_path = tempfile.mkstemp(dir=metadata_dir, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(metadata, f)
        os.rename(tmp_path, path)
    except BaseException:
        os.unlink(tmp_path)
        raise
    logger.info("Wrote metadata file: %s", path)


def _remove_metadata_file(path: str) -> None:
    """Remove the metadata file if it exists. Log on failure but do not raise."""
    try:
        os.remove(path)
        logger.debug("Removed metadata file: %s", path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Failed to remove metadata file %s: %s", path, e)


def _validate_startup(
    *,
    host: str,
    launch_secret: str | None,
    execution_authority: str | None,
) -> None:
    """Validate every entry point before the owned native child can start."""

    if launch_secret is None or len(launch_secret.encode("utf-8")) < 32:
        raise ValueError("meadow-direct mode requires a launch secret of 32 bytes")
    if execution_authority not in {"trusted-host", "confined-container"}:
        raise ValueError("meadow-direct mode requires execution authority")
    loopback_hosts = {"127.0.0.1", "::1", "localhost"}
    if execution_authority == "trusted-host" and host not in loopback_hosts:
        raise ValueError("trusted-host direct mode may bind only to loopback")
    if execution_authority == "confined-container":
        if os.environ.get(CONTAINER_BOUNDARY_ENV) != "1":
            raise ValueError(
                "confined-container requires MEADOW_BRIDGE_CONTAINER_BOUNDARY=1 from "
                "the managed container launcher"
            )
        if not _has_observable_container_boundary():
            raise ValueError(
                "confined-container requires an observable container runtime boundary"
            )
        if host not in loopback_hosts | {"0.0.0.0", "::"}:
            raise ValueError(
                "confined-container direct mode supports only container-local binds"
            )


async def run(
    binary: str,
    port: int,
    cwd: str,
    *,
    subprocess_env: dict[str, str] | None = None,
    command_env: dict[str, str] | None = None,
    metadata_file: str | None = None,
    host: str = "127.0.0.1",
    launch_secret: str | None = None,
    execution_authority: str | None = None,
    direct_limits: DirectLimits | None = None,
    raw_event_file: str | None = None,
    shell: ShellSpec | None = None,
) -> None:
    """Admit native capabilities before HTTP readiness and settle every owner."""
    _validate_startup(
        host=host,
        launch_secret=launch_secret,
        execution_authority=execution_authority,
    )
    limits = direct_limits or DirectLimits()
    admission = await asyncio.to_thread(admit_compatible_binary, binary)
    client = NativeClient(
        admission.path, cwd=Path(cwd), raw_event_file=raw_event_file, shell=shell,
        command_env=command_env,
    )
    child_lost_event = asyncio.Event()
    server: uvicorn.Server | None = None
    server_start_attempted = False
    server_task: asyncio.Task[None] | None = None
    watchers: list[asyncio.Task[bool]] = []
    metadata_written = False
    direct_service: DirectService | None = None
    generation_loss_task: asyncio.Task[None] | None = None
    shutting_down = False

    def _owned_child_closed(_reason: str) -> None:
        nonlocal generation_loss_task
        if shutting_down:
            return
        child_lost_event.set()
        if direct_service is not None and generation_loss_task is None:
            generation_loss_task = asyncio.create_task(
                direct_service.mark_generation_lost(
                    "owned native child transport closed"
                )
            )

    client.on_transport_closed(_owned_child_closed)
    try:
        child_env = _direct_child_env(
            dict(subprocess_env) if subprocess_env is not None else dict(os.environ)
        )
        child_env["GITHUB_COPILOT_ACP_USE_CLI"] = "0"
        try:
            async with asyncio.timeout(limits.session_creation_timeout_s):
                await client.start(env=child_env)
        except TimeoutError:
            raise _direct_binary_capability_error(
                admission, "bounded native initialization and model catalog"
            ) from None
        except (NativeRpcError, NativeTransportError):
            raise _direct_binary_capability_error(
                admission, "native initialization and model catalog"
            ) from None
        if not client.models:
            raise _direct_binary_capability_error(
                admission, "nonempty native model catalog"
            )
        if child_lost_event.is_set() or not client.is_alive:
            raise ConnectionError("Native child closed during startup")
        logger.info("Available models: %s", [model.id for model in client.models])

        direct_service = DirectService(
            client,
            cwd=cwd,
            launch_secret=launch_secret or "",
            execution_authority=execution_authority or "",
            limits=limits,
        )
        app = create_direct_app(direct_service)
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            logger.info("Shutdown signal received")
            shutdown_event.set()

        _install_shutdown_signal_handlers(asyncio.get_running_loop(), _signal_handler)

        # Uvicorn 0.44 binds sockets during startup; publish the actual port only
        # after native admission and HTTP binding have both completed.
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)
        server_start_attempted = True
        await server.startup()
        if child_lost_event.is_set() or not client.is_alive:
            raise ConnectionError("Native child closed during HTTP startup")
        if not server.servers or not server.servers[0].sockets:
            raise RuntimeError("Server startup produced no listening sockets")
        socket_address: object = server.servers[0].sockets[0].getsockname()
        if (
            not isinstance(socket_address, tuple)
            or len(socket_address) < 2
            or not isinstance(socket_address[1], int)
            or isinstance(socket_address[1], bool)
            or not 0 < socket_address[1] < 65536
        ):
            raise RuntimeError("Server startup produced no usable TCP port")
        actual_port = socket_address[1]
        if metadata_file is not None:
            _write_metadata_file(
                metadata_file,
                actual_port,
                host=host,
                continuity_generation_id=direct_service.continuity_generation_id,
            )
            metadata_written = True

        server_task = asyncio.create_task(server.main_loop())
        logger.info("Meadow Bridge listening on http://%s:%d", host, actual_port)
        logger.info(
            "Direct capabilities endpoint: http://%s:%d/meadow/v%d/capabilities",
            host, actual_port, DIRECT_PROTOCOL_MAJOR,
        )
        signal_task = asyncio.create_task(shutdown_event.wait())
        child_task = asyncio.create_task(child_lost_event.wait())
        watchers.extend((signal_task, child_task))
        done, _pending = await asyncio.wait(
            [server_task, signal_task, child_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if child_task in done and child_lost_event.is_set():
            logger.error("Owned native child transport closed; stopping Meadow Bridge")
            if generation_loss_task is not None:
                await generation_loss_task
        server.should_exit = True
        await server_task
    finally:
        shutting_down = True
        for watcher in watchers:
            if not watcher.done():
                watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
        if server_task is not None and not server_task.done():
            server_task.cancel()
            await asyncio.gather(server_task, return_exceptions=True)
        cleanup_errors: list[Exception] = []
        if generation_loss_task is not None:
            try:
                await generation_loss_task
            except Exception as error:
                cleanup_errors.append(error)
        if direct_service is not None:
            try:
                await direct_service.mark_generation_lost(
                    "owned Meadow Bridge is shutting down", expected_shutdown=True
                )
            except Exception as error:
                cleanup_errors.append(error)
        if server is not None and server_start_attempted:
            try:
                await server.shutdown()
            except Exception as error:
                cleanup_errors.append(error)
        if metadata_file is not None and metadata_written:
            _remove_metadata_file(metadata_file)
        try:
            await client.stop()
        except Exception as error:
            cleanup_errors.append(error)
        if len(cleanup_errors) == 1:
            raise cleanup_errors[0]
        if cleanup_errors:
            raise ExceptionGroup("Meadow Bridge cleanup did not settle", cleanup_errors)
        logger.info("Meadow Bridge stopped.")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser without starting external services."""
    parser = argparse.ArgumentParser(
        description="Authenticated Meadow integration with the native Copilot IDE interface"
    )
    parser.add_argument(
        "--binary",
        help="Path to copilot-language-server binary (auto-discovered if omitted)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Address to bind (default: 127.0.0.1; "
            "0.0.0.0 exposes all IPv4 interfaces)"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to listen on (default: 8765). Use 0 for ephemeral port assignment.",
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Working directory for native sessions (default: current dir)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging level (default: WARNING, or MEADOW_BRIDGE_LOG_LEVEL). "
        "File always logs DEBUG.",
    )
    parser.add_argument(
        "--shell",
        type=ShellSpec,
        help=(
            "Command shell executable (default: Windows PowerShell 5.1 or /bin/sh). "
            "Select pwsh explicitly for PowerShell 7."
        ),
    )
    parser.add_argument(
        "--log-file",
        default="logs/meadow-bridge.log",
        help="Log file path (default: logs/meadow-bridge.log). DEBUG level always.",
    )
    parser.add_argument(
        "--raw-event-file",
        help="Opt-in NDJSON capture of full native protocol events and turn boundaries.",
    )
    parser.add_argument(
        "--metadata-file",
        help="Write a JSON metadata file at this path after startup (port, pid, status).",
    )
    parser.add_argument(
        "--execution-authority",
        choices=["trusted-host", "confined-container"],
        help="Truthfully describes native and Bridge tool authority",
    )
    return parser


def _validate_options(args: argparse.Namespace) -> str | None:
    """Fail before process startup when mode, bind, or prompt options conflict."""

    secret = os.environ.get(DIRECT_SECRET_ENV)
    _validate_startup(
        host=args.host,
        launch_secret=secret,
        execution_authority=args.execution_authority,
    )
    return secret


def main() -> None:
    args = _build_parser().parse_args()

    _configure_logging(args.log_level, args.log_file)

    try:
        launch_secret = _validate_options(args)
    except ValueError as exc:
        logger.error("Invalid startup configuration: %s", exc)
        sys.exit(2)

    binary = args.binary
    if not binary:
        logger.info("Auto-discovering compatible copilot-language-server...")
        try:
            binary = find_binary()
        except BinaryCompatibilityError as exc:
            logger.error("Incompatible copilot-language-server: %s", exc)
            sys.exit(1)
    if not binary:
        logger.error(
            "Could not find a compatible copilot-language-server binary. "
            "Named candidates below the JetBrains data root and the minimum "
            "reported language-server version are defined by discovery.py. "
            "Pass --binary only to select another version-admitted executable."
        )
        sys.exit(1)

    logger.info("Using binary: %s", binary)
    logger.info("Working directory (cwd): %s", args.cwd)
    logger.info("Platform: %s", platform.system())

    # Load user config and build subprocess environment with proxy settings
    cfg = load_config()
    subprocess_env = build_subprocess_env(cfg)
    # The proxy launch credential authenticates inbound Meadow traffic only.
    # Never expose it to the separately controlled language-server subprocess.
    subprocess_env.pop(DIRECT_SECRET_ENV, None)
    # Copilot authentication discovered for the language server is not an
    # inherited command credential. Preserve the configured environment first.
    command_env = dict(subprocess_env)
    try:
        subprocess_env = inject_prior_copilot_oauth(subprocess_env)
    except CopilotOAuthCredentialError as exc:
        logger.error("Direct Copilot authentication setup failed: %s", exc)
        sys.exit(1)
    logger.info("Config file: %s", config_path())

    try:
        asyncio.run(
            run(
                binary,
                args.port,
                args.cwd,
                subprocess_env=subprocess_env,
                command_env=command_env,
                metadata_file=args.metadata_file,
                host=args.host,
                launch_secret=launch_secret,
                execution_authority=args.execution_authority,
                raw_event_file=args.raw_event_file,
                shell=args.shell,
            )
        )
    except BinaryCompatibilityError as exc:
        logger.error("Incompatible copilot-language-server: %s", exc)
        sys.exit(1)
    except RawEventCaptureError as exc:
        logger.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
