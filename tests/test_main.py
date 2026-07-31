"""Tests for the acp-proxy command-line entry point and server wiring."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from acp_proxy import __main__ as cli


@pytest.mark.parametrize(
    ("argv", "expected_host"),
    [
        ([], "127.0.0.1"),
        (["--host", "0.0.0.0"], "0.0.0.0"),
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
        def __init__(self, binary: str) -> None:
            self.binary = binary
            self.models = [SimpleNamespace(model_id="test-model")]
            self.default_model = "test-model"
            self.stopped = False
            observed["client"] = self

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
    )

    assert observed["config"].host == "0.0.0.0"
    assert observed["client"].stopped is True
    assert observed["shutdown"] is True
