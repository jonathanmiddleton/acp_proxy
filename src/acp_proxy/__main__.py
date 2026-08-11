"""
Entry point for the explicit ACP Proxy consumer modes.

Usage:
    acp-proxy [OPTIONS]

    Start the proxy from your project directory. The current working directory
    becomes the ACP workspace — the copilot-language-server scans it and scopes
    file operations to it.

    --binary PATH       Path to copilot-language-server binary.
                        Auto-discovers named JetBrains plugin binaries.
    --host HOST         Address to bind (default: 127.0.0.1).
    --port PORT         Port to listen on (default: 8765). Use 0 for ephemeral.
    --cwd PATH          Working directory for ACP sessions (default: current dir)
    --log-level LEVEL   Console logging level (default: DEBUG)
    --log-file PATH     Log file path (default: logs/proxy.log)
    --system-prompt     Path to system prompt file injected into each new session.
    --metadata-file     Write JSON metadata (port, pid, status) after startup.
    --context-files     Comma-separated context filenames, or 'none' to disable.
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

import uvicorn

from .application_policy import MIN_COPILOT_LANGUAGE_SERVER_VERSION
from .client import AcpClient, CallbackPolicy, ModelAcknowledgementError
from .config import (
    build_subprocess_env,
    compose_system_prompt,
    config_path,
    load_config,
)
from .copilot_auth import (
    CopilotOAuthCredentialError,
    inject_prior_copilot_oauth,
)
from .direct_protocol import DirectLimits
from .direct_server import create_direct_app
from .direct_service import DirectService
from .discovery import (
    BinaryAdmission,
    BinaryCompatibilityError,
    admit_compatible_binary,
    find_binary,
)
from .server import create_app

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 3
DIRECT_SECRET_ENV = "ACP_PROXY_MEADOW_SECRET"
CONTAINER_BOUNDARY_ENV = "ACP_PROXY_CONTAINER_BOUNDARY"
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


def _has_observable_container_boundary() -> bool:
    """Return true only when this process can observe a container runtime marker."""

    return any(os.path.exists(marker) for marker in CONTAINER_MARKERS)


def _direct_child_env(source: dict[str, str]) -> dict[str, str]:
    """Build the allowlisted environment for the admitted direct ACP child."""

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


def _configure_logging(console_level: str, log_file: str) -> None:
    """Set up dual logging: DEBUG to file (always), configurable to console."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler — respects --log-level
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, console_level))
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
    consumer_mode: str | None = None,
    protocol_major: int | None = None,
    continuity_generation_id: str | None = None,
) -> None:
    """Write a JSON metadata file with process info and readiness status.

    This file doubles as a readiness signal — its existence means the
    server is bound and accepting connections.

    Uses write-to-temp + rename for atomic creation so consumers never
    observe a partially-written file.
    """
    metadata = {
        "pid": os.getpid(),
        "port": port,
        "host": host,
        "status": "ready",
    }
    if consumer_mode is not None:
        metadata["consumer_mode"] = consumer_mode
    if protocol_major is not None:
        metadata["protocol_major"] = protocol_major
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


def _validate_run_mode(
    *,
    consumer_mode: str,
    host: str,
    launch_secret: str | None,
    execution_authority: str | None,
    system_prompt: str | None,
    direct_limits: DirectLimits | None,
) -> CallbackPolicy:
    """Validate every entry point before the owned ACP child can start."""

    if consumer_mode == "opencode-legacy":
        if execution_authority is not None or direct_limits is not None:
            raise ValueError(
                "direct execution options are invalid in opencode-legacy mode"
            )
        return CallbackPolicy.LEGACY_PERMISSIVE
    if consumer_mode != "meadow-direct":
        raise ValueError(f"unsupported consumer mode: {consumer_mode}")
    if system_prompt is not None:
        raise ValueError("meadow-direct mode rejects proxy-authored system prompts")
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
                "confined-container requires ACP_PROXY_CONTAINER_BOUNDARY=1 from "
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
    return CallbackPolicy.DIRECT_DENY


async def run(
    binary: str,
    port: int,
    cwd: str,
    *,
    consumer_mode: str,
    system_prompt: str | None = None,
    subprocess_env: dict[str, str] | None = None,
    metadata_file: str | None = None,
    host: str = "127.0.0.1",
    launch_secret: str | None = None,
    execution_authority: str | None = None,
    direct_limits: DirectLimits | None = None,
) -> None:
    """Start the ACP client and HTTP server."""
    callback_policy = _validate_run_mode(
        consumer_mode=consumer_mode,
        host=host,
        launch_secret=launch_secret,
        execution_authority=execution_authority,
        system_prompt=system_prompt,
        direct_limits=direct_limits,
    )
    effective_direct_limits = (
        direct_limits or DirectLimits()
        if consumer_mode == "meadow-direct"
        else None
    )
    admission = await asyncio.to_thread(admit_compatible_binary, binary)
    binary = admission.path
    if consumer_mode == "opencode-legacy":
        logger.warning(
            "opencode-legacy mode is deprecated and will be removed in acp-proxy 0.3.0"
        )

    client = AcpClient(binary, callback_policy=callback_policy)
    child_lost_event = asyncio.Event()
    server: uvicorn.Server | None = None
    server_start_attempted = False
    metadata_written = False
    direct_service: DirectService | None = None
    generation_loss_task: asyncio.Task[None] | None = None

    def _owned_child_closed() -> None:
        nonlocal generation_loss_task
        child_lost_event.set()
        if direct_service is not None and generation_loss_task is None:
            generation_loss_task = asyncio.create_task(
                direct_service.mark_generation_lost(
                    "owned ACP child transport closed"
                )
            )

    client.on_transport_closed(_owned_child_closed)
    try:
        if consumer_mode == "meadow-direct":
            child_env = _direct_child_env(
                dict(subprocess_env) if subprocess_env is not None else dict(os.environ)
            )
        else:
            child_env = dict(subprocess_env) if subprocess_env is not None else None
        await client.start(env=child_env)

        # ACP initialize has no model catalog. Create exactly one non-prompted,
        # non-Meadow catalog-probe session at startup. HTTP capability requests
        # are read-only and never create additional ACP sessions.
        if consumer_mode == "meadow-direct":
            assert effective_direct_limits is not None
            try:
                async with asyncio.timeout(
                    effective_direct_limits.session_creation_timeout_s
                ):
                    catalog_session_id = await client.create_session(cwd)
                    default_model = client.default_model
                    advertised_models = {model.model_id for model in client.models}
                    if (
                        not isinstance(default_model, str)
                        or not default_model
                        or default_model not in advertised_models
                    ):
                        raise _direct_binary_capability_error(
                            admission,
                            "startup model catalog with an advertised usable default model",
                        )
                    await client.negotiate_direct_model_binding(
                        catalog_session_id,
                        default_model,
                    )
            except TimeoutError:
                raise _direct_binary_capability_error(
                    admission,
                    "bounded startup model catalog and binding negotiation",
                ) from None
            except ModelAcknowledgementError:
                raise _direct_binary_capability_error(
                    admission,
                    "a supported session model binding strategy",
                ) from None
            logger.info(
                "Created and model-bound non-prompted catalog-probe ACP session; "
                "backend close is unsupported"
            )
        else:
            catalog_session_id = await client.create_session(cwd)
            logger.info(
                "Created non-prompted catalog-probe ACP session %s; "
                "backend close is %s",
                catalog_session_id,
                "unsupported",
            )
        if child_lost_event.is_set():
            raise ConnectionError("ACP child closed during startup")

        logger.info("Available models: %s", [m.model_id for m in client.models])
        logger.info("Default model: %s", client.default_model)

        if consumer_mode == "meadow-direct":
            direct_service = DirectService(
                client,
                cwd=cwd,
                launch_secret=launch_secret or "",
                execution_authority=execution_authority or "",
                limits=effective_direct_limits,
            )
            app = create_direct_app(direct_service)
        else:
            app = create_app(client, cwd, system_prompt=system_prompt)

        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        loop = asyncio.get_event_loop()
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            logger.info("Shutdown signal received")
            shutdown_event.set()

        if sys.platform == "win32":
            signal.signal(signal.SIGINT, lambda *_: _signal_handler())
        else:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _signal_handler)

        # This reproduces uvicorn 0.44's internal setup so port 0 can be
        # reported atomically in the readiness metadata.
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)
        server_start_attempted = True
        await server.startup()
        if child_lost_event.is_set():
            raise ConnectionError("ACP child closed during HTTP startup")

        if server.servers and server.servers[0].sockets:
            actual_port = server.servers[0].sockets[0].getsockname()[1]
        else:
            logger.error(
                "Server started but no listening sockets found. server.servers=%r",
                getattr(server, "servers", None),
            )
            raise RuntimeError("Server startup produced no listening sockets")

        # --- Phase 1b: write metadata file before main_loop (readiness signal) ---
        if metadata_file is not None:
            _write_metadata_file(
                metadata_file,
                actual_port,
                host=host,
                consumer_mode=consumer_mode,
                protocol_major=(1 if direct_service is not None else None),
                continuity_generation_id=(
                    direct_service.continuity_generation_id
                    if direct_service is not None
                    else None
                ),
            )
            metadata_written = True

        # Run the server main loop in a background task
        server_task = asyncio.create_task(server.main_loop())

        logger.info("Proxy listening on http://%s:%d", host, actual_port)
        if consumer_mode == "meadow-direct":
            logger.info(
                "Direct capabilities endpoint: http://%s:%d/meadow/v1/capabilities",
                host,
                actual_port,
            )
        else:
            logger.info("Models endpoint: http://%s:%d/v1/models", host, actual_port)
            logger.info(
                "Completions endpoint: http://%s:%d/v1/chat/completions",
                host,
                actual_port,
            )

        # Wait for shutdown signal or server to stop
        signal_task = asyncio.create_task(shutdown_event.wait())
        child_task = asyncio.create_task(child_lost_event.wait())
        done, _pending = await asyncio.wait(
            [server_task, signal_task, child_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for watcher in (signal_task, child_task):
            if not watcher.done():
                watcher.cancel()
        await asyncio.gather(signal_task, child_task, return_exceptions=True)

        if child_task in done and child_lost_event.is_set():
            logger.error("Owned ACP child transport closed; stopping proxy")
            if generation_loss_task is not None:
                await generation_loss_task
            elif direct_service is not None:
                await direct_service.mark_generation_lost(
                    "owned ACP child transport closed"
                )

        if server_task not in done:
            server.should_exit = True
            await server_task
        else:
            await server_task
    finally:
        if generation_loss_task is not None and not generation_loss_task.done():
            await generation_loss_task
        if direct_service is not None:
            await direct_service.mark_generation_lost("owned proxy is shutting down")
        if server is not None and server_start_attempted:
            try:
                await server.shutdown()
            except Exception:
                logger.exception("HTTP server cleanup failed")
        if metadata_file is not None and metadata_written:
            _remove_metadata_file(metadata_file)
        try:
            await client.stop()
        except Exception:
            logger.exception("ACP child cleanup failed")
        logger.info("Proxy stopped.")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser without starting external services."""
    parser = argparse.ArgumentParser(
        description="Explicit Meadow-direct or deprecated OpenCode ACP proxy"
    )
    parser.add_argument(
        "--consumer-mode",
        required=True,
        choices=["meadow-direct", "opencode-legacy"],
        help="Required inbound contract; modes are mutually exclusive",
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
        help="Working directory for ACP sessions (default: current dir)",
    )
    parser.add_argument(
        "--log-level",
        default="DEBUG",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console logging level (default: DEBUG during development). "
        "File always logs DEBUG.",
    )
    parser.add_argument(
        "--log-file",
        default="logs/proxy.log",
        help="Log file path (default: logs/proxy.log). DEBUG level always.",
    )
    parser.add_argument(
        "--system-prompt",
        help="Path to a file containing a system prompt to inject into each new session.",
    )
    parser.add_argument(
        "--metadata-file",
        help="Write a JSON metadata file at this path after startup (port, pid, status).",
    )
    parser.add_argument(
        "--context-files",
        help="Comma-separated list of context filenames to inject, or 'none' to disable. "
        "Default: AGENTS.md,CLAUDE.md,COPILOT-INSTRUCTIONS.md",
    )
    parser.add_argument(
        "--execution-authority",
        choices=["trusted-host", "confined-container"],
        help="Required in meadow-direct mode; truthfully describes ACP tool authority",
    )
    return parser


def _validate_mode_options(args: argparse.Namespace) -> str | None:
    """Fail before process startup when mode, bind, or prompt options conflict."""

    if args.consumer_mode == "meadow-direct" and args.context_files is not None:
        raise ValueError(
            "meadow-direct mode rejects --context-files; "
            "Meadow owns all prompt layers"
        )
    secret = (
        os.environ.get(DIRECT_SECRET_ENV)
        if args.consumer_mode == "meadow-direct"
        else None
    )
    _validate_run_mode(
        consumer_mode=args.consumer_mode,
        host=args.host,
        launch_secret=secret,
        execution_authority=args.execution_authority,
        system_prompt=args.system_prompt,
        direct_limits=None,
    )
    return secret


def main() -> None:
    args = _build_parser().parse_args()

    _configure_logging(args.log_level, args.log_file)

    try:
        launch_secret = _validate_mode_options(args)
    except ValueError as exc:
        logger.error("Invalid consumer-mode configuration: %s", exc)
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

    explicit_prompt = None
    if args.consumer_mode == "opencode-legacy" and args.system_prompt:
        with open(args.system_prompt) as f:
            explicit_prompt = f.read().strip()
        logger.info(
            "Loaded explicit system prompt from %s (%d chars)",
            args.system_prompt,
            len(explicit_prompt),
        )

    logger.info("Using binary: %s", binary)
    logger.info("Working directory (cwd): %s", args.cwd)
    logger.info("Platform: %s", platform.system())

    # Load user config and build subprocess environment with proxy settings
    cfg = load_config()
    subprocess_env = build_subprocess_env(cfg)
    # The proxy launch credential authenticates inbound Meadow traffic only.
    # Never expose it to the separately controlled language-server subprocess.
    subprocess_env.pop(DIRECT_SECRET_ENV, None)
    if args.consumer_mode == "meadow-direct":
        try:
            subprocess_env = inject_prior_copilot_oauth(subprocess_env)
        except CopilotOAuthCredentialError as exc:
            logger.error("Direct Copilot authentication setup failed: %s", exc)
            sys.exit(1)
    logger.info("Config file: %s", config_path())

    # --- Phase 1c: CLI override for context files ---
    if args.consumer_mode == "opencode-legacy" and args.context_files is not None:
        if args.context_files == "none":
            cfg["context_files"] = []
            logger.info("Context files disabled via --context-files none")
        else:
            cfg["context_files"] = [
                f.strip() for f in args.context_files.split(",") if f.strip()
            ]
            logger.info("Context files overridden via CLI: %s", cfg["context_files"])

    # Compose system prompt from explicit file + workspace context files
    system_prompt = None
    if args.consumer_mode == "opencode-legacy":
        system_prompt = compose_system_prompt(explicit_prompt, args.cwd, cfg)
        if system_prompt:
            logger.info("Legacy system prompt ready (%d chars)", len(system_prompt))
        else:
            logger.info("No legacy system prompt configured")

    try:
        asyncio.run(
            run(
                binary,
                args.port,
                args.cwd,
                system_prompt=system_prompt,
                subprocess_env=subprocess_env,
                metadata_file=args.metadata_file,
                host=args.host,
                consumer_mode=args.consumer_mode,
                launch_secret=launch_secret,
                execution_authority=args.execution_authority,
            )
        )
    except BinaryCompatibilityError as exc:
        logger.error("Incompatible copilot-language-server: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
