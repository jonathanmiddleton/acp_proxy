"""
Entry point for the ACP-to-OpenAI proxy.

Usage:
    acp-proxy [OPTIONS]

    Start the proxy from your project directory. The current working directory
    becomes the ACP workspace — the copilot-language-server scans it and scopes
    file operations to it.

    --binary PATH       Path to copilot-language-server binary.
                        Auto-discovered if omitted (IntelliJ 2025.3 plugin only).
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

from .client import AcpClient
from .config import (
    build_subprocess_env,
    compose_system_prompt,
    config_path,
    load_config,
)
from .discovery import find_binary
from .server import create_app

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
LOG_BACKUP_COUNT = 3


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
    path: str, port: int, host: str = "127.0.0.1"
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


async def run(
    binary: str,
    port: int,
    cwd: str,
    system_prompt: str | None = None,
    subprocess_env: dict[str, str] | None = None,
    metadata_file: str | None = None,
    host: str = "127.0.0.1",
) -> None:
    """Start the ACP client and HTTP server."""
    client = AcpClient(binary)
    await client.start(env=subprocess_env)

    # Create an initial session to discover models
    await client.create_session(cwd)

    logger.info("Available models: %s", [m.model_id for m in client.models])
    logger.info("Default model: %s", client.default_model)

    app = create_app(client, cwd, system_prompt=system_prompt)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",  # Suppress uvicorn's own logging; we route via root
    )
    server = uvicorn.Server(config)

    # Handle shutdown
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    if sys.platform == "win32":
        # asyncio.loop.add_signal_handler is not supported on Windows.
        # Use signal.signal for SIGINT (Ctrl+C); SIGTERM does not exist
        # on Windows.
        signal.signal(signal.SIGINT, lambda *_: _signal_handler())
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # --- Phase 1a: explicit startup to discover the actual bound port ---
    # Replicate the setup that uvicorn.Server._serve() performs before
    # calling startup().  This is an internal contract of uvicorn 0.44.0
    # (see Server._serve in uvicorn/server.py).  Pin uvicorn to ~=0.44.0
    # in pyproject.toml — this sequence may break on major version bumps.
    if not config.loaded:
        config.load()
    server.lifespan = config.lifespan_class(config)

    # NOTE: uvicorn calls sys.exit(1) inside startup() if the socket bind
    # fails (e.g. port already in use).  This bypasses our cleanup code.
    # There is no clean way to intercept this from outside uvicorn today.
    await server.startup()

    # Discover the actual port (critical for --port 0 / ephemeral assignment)
    if server.servers and server.servers[0].sockets:
        actual_port = server.servers[0].sockets[0].getsockname()[1]
    else:
        logger.error(
            "Server started but no listening sockets found. server.servers=%r",
            getattr(server, "servers", None),
        )
        await client.stop()
        raise RuntimeError("Server startup produced no listening sockets")

    try:
        # --- Phase 1b: write metadata file before main_loop (readiness signal) ---
        if metadata_file is not None:
            _write_metadata_file(metadata_file, actual_port, host=host)

        # Run the server main loop in a background task
        server_task = asyncio.create_task(server.main_loop())

        logger.info("Proxy listening on http://%s:%d", host, actual_port)
        logger.info("Models endpoint: http://%s:%d/v1/models", host, actual_port)
        logger.info(
            "Completions endpoint: http://%s:%d/v1/chat/completions",
            host,
            actual_port,
        )

        # Wait for shutdown signal or server to stop
        done, pending = await asyncio.wait(
            [server_task, asyncio.create_task(shutdown_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

        # Clean up
        if not server_task.done():
            server.should_exit = True
            await server_task
    finally:
        await server.shutdown()
        if metadata_file is not None:
            _remove_metadata_file(metadata_file)
        await client.stop()
        logger.info("Proxy stopped.")


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser without starting external services."""
    parser = argparse.ArgumentParser(
        description="ACP-to-OpenAI proxy for copilot-language-server"
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
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    _configure_logging(args.log_level, args.log_file)

    binary = args.binary
    if not binary:
        logger.info("Auto-discovering compatible copilot-language-server...")
        binary = find_binary()
    if not binary:
        logger.error(
            "Could not find a compatible copilot-language-server binary. "
            "Only the IntelliJ IDEA 2025.3 Copilot plugin binary is supported. "
            "Pass --binary /path/to/copilot-language-server to override."
        )
        sys.exit(1)

    explicit_prompt = None
    if args.system_prompt:
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
    logger.info("Config file: %s", config_path())

    # --- Phase 1c: CLI override for context files ---
    if args.context_files is not None:
        if args.context_files == "none":
            cfg["context_files"] = []
            logger.info("Context files disabled via --context-files none")
        else:
            cfg["context_files"] = [
                f.strip() for f in args.context_files.split(",") if f.strip()
            ]
            logger.info("Context files overridden via CLI: %s", cfg["context_files"])

    # Compose system prompt from explicit file + workspace context files
    system_prompt = compose_system_prompt(explicit_prompt, args.cwd, cfg)
    if system_prompt:
        logger.info("System prompt ready (%d chars)", len(system_prompt))
    else:
        logger.info(
            "No system prompt configured (no --system-prompt and no context files found)"
        )

    asyncio.run(
        run(
            binary,
            args.port,
            args.cwd,
            system_prompt=system_prompt,
            subprocess_env=subprocess_env,
            metadata_file=args.metadata_file,
            host=args.host,
        )
    )


if __name__ == "__main__":
    main()
