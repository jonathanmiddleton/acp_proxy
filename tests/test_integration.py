"""Black-box integration tests for the proxy's public process contracts.

The tests launch the real CLI and observe only process exit, readiness
metadata, and TCP HTTP.  Meadow startup owns credential setup, binary
admission, ACP startup, model negotiation, service construction, and wiring;
the deprecated legacy contract receives its documented explicit child token.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import httpx
import pytest

from acp_proxy.copilot_auth import inject_prior_copilot_oauth
from acp_proxy.discovery import BinaryCompatibilityError, find_binary

REQUIRED_LIVE_MODEL = "gpt-5.3-codex"
UNADVERTISED_LIVE_MODEL = "acp-proxy-negative-control-model"
_DIRECT_SECRET_ENV = "ACP_PROXY_MEADOW_SECRET"
_COPILOT_TOKEN_ENV_NAMES = frozenset(
    {"GH_COPILOT_TOKEN", "GITHUB_COPILOT_TOKEN"}
)
_PROXY_START_TIMEOUT_S = 90.0
_PROXY_STOP_TIMEOUT_S = 20.0
_CONSOLE_TAIL_BYTES = 256 * 1024
_HTTP_TIMEOUT = httpx.Timeout(180.0, connect=5.0)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
_PATH_SAFE_GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,255}$")
_AUTH_GATE_RUNTIME_ENV_NAMES = frozenset(
    {
        "COMSPEC",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "LANG",
        "LD_LIBRARY_PATH",
        "PATH",
        "PATHEXT",
        "PYTHONHOME",
        "PYTHONIOENCODING",
        "PYTHONNOUSERSITE",
        "PYTHONUTF8",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)


@dataclass(frozen=True)
class LiveProxy:
    """One ready proxy process exposed only through its public boundary."""

    base_url: str
    metadata: dict[str, Any]
    debug_log_path: Path
    launch_secret: str | None = field(default=None, repr=False)

    @property
    def authorization_headers(self) -> dict[str, str]:
        if self.launch_secret is None:
            raise RuntimeError("this proxy mode has no inbound bearer credential")
        return {"Authorization": f"Bearer {self.launch_secret}"}


def _source_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Make the current checkout importable by the spawned Python process."""

    env = dict(source)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{_SOURCE_ROOT}{os.pathsep}{existing}" if existing else str(_SOURCE_ROOT)
    )
    return env


def _minimal_runtime_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Retain only interpreter/runtime variables for the no-credential probe."""

    return {
        key: value
        for key, value in source.items()
        if key.upper() in _AUTH_GATE_RUNTIME_ENV_NAMES
        or key.upper().startswith("LC_")
    }


def _copilot_credential_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value
            for key, value in environment.items()
            if key.upper() in _COPILOT_TOKEN_ENV_NAMES and value
        )
    )


def _redact(text: str, sensitive_values: tuple[str, ...]) -> str:
    redacted = text
    for value in sensitive_values:
        redacted = redacted.replace(value, "<redacted>")
    return redacted


def _files_contain_sensitive_value(
    log_path: Path, sensitive_values: tuple[str, ...]
) -> bool:
    """Scan a bounded rotating-log set without loading it into memory."""

    needles = tuple(value.encode("utf-8") for value in sensitive_values if value)
    if not needles:
        return False
    overlap_size = max(len(needle) for needle in needles) - 1
    paths = tuple(log_path.parent.glob(f"{log_path.name}*"))
    for candidate in paths:
        overlap = b""
        with candidate.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                scan = overlap + chunk
                if any(needle in scan for needle in needles):
                    return True
                overlap = scan[-overlap_size:] if overlap_size else b""
    return False


class _ConsoleCapture:
    """Continuously drain a child pipe while retaining only a bounded tail."""

    def __init__(self, sensitive_values: tuple[str, ...]) -> None:
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()
        self._needles = tuple(
            value.encode("utf-8") for value in sensitive_values if value
        )
        self._overlap = b""
        self._sensitive_value_seen = False
        self._drain_error: str | None = None

    def drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(8192):
                self._append(chunk)
        except OSError as exc:
            with self._lock:
                self._drain_error = str(exc)
        finally:
            stream.close()

    def _append(self, chunk: bytes) -> None:
        with self._lock:
            if self._needles:
                scan = self._overlap + chunk
                if any(needle in scan for needle in self._needles):
                    self._sensitive_value_seen = True
                overlap_size = max(len(needle) for needle in self._needles) - 1
                self._overlap = scan[-overlap_size:] if overlap_size else b""

            if len(chunk) >= _CONSOLE_TAIL_BYTES:
                self._chunks.clear()
                chunk = chunk[-_CONSOLE_TAIL_BYTES:]
                self._size = 0
            self._chunks.append(chunk)
            self._size += len(chunk)
            while self._size > _CONSOLE_TAIL_BYTES:
                removed = self._chunks.popleft()
                self._size -= len(removed)

    def text(self) -> str:
        with self._lock:
            return b"".join(self._chunks).decode("utf-8", errors="replace")

    @property
    def sensitive_value_seen(self) -> bool:
        with self._lock:
            return self._sensitive_value_seen

    @property
    def drain_error(self) -> str | None:
        with self._lock:
            return self._drain_error


def _process_diagnostic(
    process: subprocess.Popen[bytes],
    console: _ConsoleCapture,
    debug_log_path: Path,
    sensitive_values: tuple[str, ...],
) -> str:
    return (
        f"proxy return code: {process.poll()}\n"
        f"proxy DEBUG log: {debug_log_path}\n"
        "bounded proxy console tail:\n"
        f"{_redact(console.text(), sensitive_values)}"
    )


def _posix_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_posix_group_exit(group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _posix_group_exists(group_id):
            return True
        time.sleep(0.05)
    return not _posix_group_exists(group_id)


def _signal_posix_group(group_id: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(group_id, sig)
    except ProcessLookupError:
        return False
    return True


def _terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    graceful: bool,
    process_group_id: int,
) -> int:
    """Stop the owned proxy tree without leaving its ACP child behind."""

    if os.name == "nt":
        if process.poll() is None and graceful:
            process.send_signal(getattr(signal, "CTRL_C_EVENT"))
            try:
                return process.wait(timeout=_PROXY_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
            )
            return process.wait(timeout=5.0)
        return process.wait()

    return_code = process.poll()
    if return_code is None and graceful:
        process.send_signal(signal.SIGTERM)
        try:
            return_code = process.wait(timeout=_PROXY_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            pass
    elif return_code is None:
        _signal_posix_group(process_group_id, signal.SIGTERM)
        try:
            return_code = process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass

    if return_code is None:
        _signal_posix_group(process_group_id, signal.SIGKILL)
        return_code = process.wait(timeout=5.0)

    if _posix_group_exists(process_group_id):
        _signal_posix_group(process_group_id, signal.SIGTERM)
        if not _wait_for_posix_group_exit(process_group_id, 5.0):
            _signal_posix_group(process_group_id, signal.SIGKILL)
            if not _wait_for_posix_group_exit(process_group_id, 5.0):
                raise RuntimeError(
                    f"proxy process group {process_group_id} survived SIGKILL"
                )
    return return_code


def _wait_for_readiness(
    process: subprocess.Popen[bytes],
    metadata_path: Path,
    console: _ConsoleCapture,
    debug_log_path: Path,
    sensitive_values: tuple[str, ...],
) -> dict[str, Any]:
    deadline = time.monotonic() + _PROXY_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                "proxy exited before readiness\n"
                + _process_diagnostic(
                    process, console, debug_log_path, sensitive_values
                )
            )
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AssertionError(
                    f"proxy readiness metadata is unreadable: {exc}"
                ) from exc
            if not isinstance(metadata, dict):
                raise AssertionError("proxy readiness metadata is not an object")
            return metadata
        time.sleep(0.05)

    raise AssertionError(
        f"proxy did not become ready within {_PROXY_START_TIMEOUT_S:.0f}s\n"
        + _process_diagnostic(process, console, debug_log_path, sensitive_values)
    )


def _wait_for_health(base_url: str, expected_mode: str) -> None:
    deadline = time.monotonic() + 5.0
    last_error: httpx.HTTPError | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(base_url=base_url, timeout=2.0, trust_env=False) as http:
                response = http.get("/health")
        except httpx.HTTPError as exc:
            last_error = exc
            time.sleep(0.05)
            continue

        assert response.status_code == 200, response.text
        health = response.json()
        assert health["status"] == "ok"
        assert health["consumer_mode"] == expected_mode
        if expected_mode == "meadow-direct":
            assert health["protocol_major"] == 1
        else:
            assert health["deprecated"] is True
        return
    raise AssertionError(f"proxy health endpoint did not become ready: {last_error}")


@contextmanager
def _running_proxy(
    *,
    binary: str,
    consumer_mode: str,
    environment: Mapping[str, str],
    runtime_dir: Path,
    launch_secret: str | None = None,
) -> Iterator[LiveProxy]:
    """Launch the real CLI and yield only after its HTTP socket is ready."""

    metadata_path = runtime_dir / "ready.json"
    debug_log_path = runtime_dir / "proxy.log"
    command = [
        sys.executable,
        "-m",
        "acp_proxy",
        "--consumer-mode",
        consumer_mode,
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--cwd",
        str(_REPOSITORY_ROOT),
        "--binary",
        binary,
        "--metadata-file",
        str(metadata_path),
        "--log-file",
        str(debug_log_path),
        "--log-level",
        "INFO",
    ]
    if consumer_mode == "meadow-direct":
        command.extend(["--execution-authority", "trusted-host"])

    process_env = _source_environment(environment)
    if launch_secret is not None:
        process_env[_DIRECT_SECRET_ENV] = launch_secret
    sensitive_values = tuple(
        dict.fromkeys(
            (*_copilot_credential_values(process_env), launch_secret or "")
        )
    )
    sensitive_values = tuple(value for value in sensitive_values if value)

    process_options: dict[str, Any] = {}
    if os.name == "nt":
        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        process_options["start_new_session"] = True

    runtime_dir.mkdir(parents=True, exist_ok=True)
    console = _ConsoleCapture(sensitive_values)
    process = subprocess.Popen(
        command,
        cwd=_REPOSITORY_ROOT,
        env=process_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **process_options,
    )
    assert process.stdout is not None
    process_group_id = process.pid
    console_thread = threading.Thread(
        target=console.drain,
        args=(process.stdout,),
        name=f"proxy-console-{process.pid}",
        daemon=True,
    )
    try:
        console_thread.start()
    except BaseException:
        _terminate_process_tree(
            process,
            graceful=False,
            process_group_id=process_group_id,
        )
        raise

    ready = False
    try:
        metadata = _wait_for_readiness(
            process,
            metadata_path,
            console,
            debug_log_path,
            sensitive_values,
        )
        assert type(metadata["pid"]) is int and metadata["pid"] == process.pid
        assert metadata["status"] == "ready"
        assert metadata["host"] == "127.0.0.1"
        assert metadata["consumer_mode"] == consumer_mode
        port = metadata["port"]
        assert type(port) is int and 1 <= port <= 65535
        if consumer_mode == "meadow-direct":
            protocol_major = metadata["protocol_major"]
            assert type(protocol_major) is int and protocol_major == 1
            generation_id = metadata["continuity_generation_id"]
            assert isinstance(generation_id, str)
            assert _PATH_SAFE_GENERATION.fullmatch(generation_id) is not None
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(base_url, consumer_mode)
        ready = True
        yield LiveProxy(
            base_url=base_url,
            metadata=metadata,
            launch_secret=launch_secret,
            debug_log_path=debug_log_path,
        )
    finally:
        active_exception = sys.exc_info()[1]
        cleanup_errors: list[str] = []
        pre_stop_return_code = process.poll()
        try:
            return_code = _terminate_process_tree(
                process,
                graceful=ready and pre_stop_return_code is None,
                process_group_id=process_group_id,
            )
            if ready and pre_stop_return_code is not None:
                cleanup_errors.append(
                    "ready proxy exited before fixture teardown\n"
                    + _process_diagnostic(
                        process, console, debug_log_path, sensitive_values
                    )
                )
            elif ready and return_code != 0:
                cleanup_errors.append(
                    f"ready proxy exited with status {return_code}\n"
                    + _process_diagnostic(
                        process, console, debug_log_path, sensitive_values
                    )
                )
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup_errors.append(f"could not stop proxy process tree: {exc}")

        console_thread.join(timeout=5.0)
        if console_thread.is_alive():
            cleanup_errors.append("proxy console drain thread did not stop")
        if console.drain_error is not None:
            cleanup_errors.append(
                f"proxy console drain failed: {console.drain_error}"
            )
        if ready and metadata_path.exists():
            cleanup_errors.append("ready proxy did not remove readiness metadata")
        if console.sensitive_value_seen or _files_contain_sensitive_value(
            debug_log_path, sensitive_values
        ):
            cleanup_errors.append("proxy diagnostics exposed a launch credential")

        if cleanup_errors:
            cleanup_message = "\n".join(cleanup_errors)
            if active_exception is not None:
                active_exception.add_note(
                    f"Additional proxy cleanup failure:\n{cleanup_message}"
                )
            else:
                raise AssertionError(cleanup_message)


@pytest.fixture(scope="module")
def binary() -> str:
    """Resolve a real, compatible JetBrains Copilot language server."""

    try:
        result = find_binary()
    except BinaryCompatibilityError as exc:
        pytest.fail(f"Incompatible copilot-language-server: {exc}")
    assert result is not None, (
        "No compatible copilot-language-server binary found. "
        "The environment must provide the supported JetBrains Copilot plugin."
    )
    assert os.path.isfile(result), f"Discovered binary path does not exist: {result}"
    assert os.access(result, os.X_OK), f"Discovered binary is not executable: {result}"
    return result


@pytest.fixture
def meadow_proxy(binary: str, tmp_path: Path) -> Iterator[LiveProxy]:
    """Run the production Meadow-direct process with its real OAuth setup."""

    launch_secret = secrets.token_urlsafe(32)
    with _running_proxy(
        binary=binary,
        consumer_mode="meadow-direct",
        environment=os.environ,
        runtime_dir=tmp_path / "meadow-direct",
        launch_secret=launch_secret,
    ) as proxy:
        yield proxy


@pytest.fixture
def legacy_proxy(binary: str, tmp_path: Path) -> Iterator[LiveProxy]:
    """Run the deprecated production adapter with an explicit child credential."""

    legacy_environment = inject_prior_copilot_oauth(dict(os.environ))
    assert _copilot_credential_values(legacy_environment), (
        "legacy credential provisioning did not produce a Copilot child token"
    )
    with _running_proxy(
        binary=binary,
        consumer_mode="opencode-legacy",
        environment=legacy_environment,
        runtime_dir=tmp_path / "opencode-legacy",
    ) as proxy:
        yield proxy


def test_meadow_direct_cli_rejects_missing_oauth_before_child_start(
    tmp_path: Path,
) -> None:
    """Missing prior OAuth fails before binary admission or child startup."""

    isolated_environment = _source_environment(
        _minimal_runtime_environment(os.environ)
    )

    isolated_roots = {
        "HOME": tmp_path / "home",
        "XDG_CONFIG_HOME": tmp_path / "xdg-config",
        "XDG_DATA_HOME": tmp_path / "xdg-data",
        "XDG_STATE_HOME": tmp_path / "xdg-state",
        "XDG_CACHE_HOME": tmp_path / "xdg-cache",
        "LOCALAPPDATA": tmp_path / "local-app-data",
        "APPDATA": tmp_path / "app-data",
        "USERPROFILE": tmp_path / "user-profile",
    }
    for name, root in isolated_roots.items():
        root.mkdir(parents=True)
        isolated_environment[name] = str(root)

    launch_secret = "black-box-auth-gate-secret-0000000000000000"
    isolated_environment[_DIRECT_SECRET_ENV] = launch_secret
    metadata_path = tmp_path / "must-not-be-ready.json"
    log_path = tmp_path / "auth-failure.log"
    invalid_binary_path = tmp_path / "must-not-start-copilot-language-server"
    assert not invalid_binary_path.exists()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "acp_proxy",
            "--consumer-mode",
            "meadow-direct",
            "--execution-authority",
            "trusted-host",
            "--port",
            "0",
            "--binary",
            str(invalid_binary_path),
            "--metadata-file",
            str(metadata_path),
            "--log-file",
            str(log_path),
            "--log-level",
            "INFO",
        ],
        cwd=_REPOSITORY_ROOT,
        env=isolated_environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 1, output
    assert "Direct Copilot authentication setup failed:" in output
    assert "Could not read github-copilot/oauth.json" in output
    assert "Starting copilot-language-server:" not in output
    assert "Incompatible copilot-language-server:" not in output
    assert "Traceback (most recent call last):" not in output
    if launch_secret in output or _files_contain_sensitive_value(
        log_path, (launch_secret,)
    ):
        raise AssertionError("launch credential leaked into proxy diagnostics")
    assert not metadata_path.exists()


def test_meadow_direct_proxy_model_binding_and_continuity(
    meadow_proxy: LiveProxy,
) -> None:
    """The real direct proxy binds the requested model and preserves continuity."""

    with httpx.Client(
        base_url=meadow_proxy.base_url,
        timeout=_HTTP_TIMEOUT,
        trust_env=False,
    ) as http:
        unauthorized = http.get("/meadow/v1/capabilities")
        assert unauthorized.status_code == 401, unauthorized.text
        assert unauthorized.json()["error"]["code"] == "unauthorized"

        capability_response = http.get(
            "/meadow/v1/capabilities",
            headers=meadow_proxy.authorization_headers,
        )
        assert capability_response.status_code == 200, capability_response.text
        capabilities = capability_response.json()
        assert capabilities["protocol"] == "meadow-acp-direct"
        assert capabilities["protocol_major"] == 1
        assert capabilities["consumer_mode"] == "meadow-direct"
        assert capabilities["continuity_generation_id"] == meadow_proxy.metadata[
            "continuity_generation_id"
        ]
        assert capabilities["canonical_workspace"] == os.path.realpath(
            _REPOSITORY_ROOT
        )
        assert capabilities["execution_authority"]["profile"] == "trusted-host"
        assert REQUIRED_LIVE_MODEL in capabilities["model_ids"]
        assert UNADVERTISED_LIVE_MODEL not in capabilities["model_ids"]

        legacy_route = http.get("/v1/models")
        assert legacy_route.status_code == 410, legacy_route.text
        assert legacy_route.json()["error"]["code"] == "legacy_mode_required"

        stable_instructions = ""
        stable_digest = hashlib.sha256(stable_instructions.encode("utf-8")).hexdigest()
        generation_id = capabilities["continuity_generation_id"]
        workspace = capabilities["canonical_workspace"]
        negative_create = http.post(
            "/meadow/v1/sessions",
            headers=meadow_proxy.authorization_headers,
            json={
                "protocol_major": 1,
                "continuity_generation_id": generation_id,
                "operation_id": "negative-model-create",
                "logical_session_id": "negative-model-session",
                "expected_canonical_workspace": workspace,
                "actor_ref": "integration-probe",
                "title": "Negative model probe",
                "model_id": UNADVERTISED_LIVE_MODEL,
                "stable_instruction_digest": stable_digest,
            },
        )
        assert negative_create.status_code == 409, negative_create.text
        assert negative_create.json()["error"]["code"] == "conflict"

        logical_session_id = "live-session"
        create_response = http.post(
            "/meadow/v1/sessions",
            headers=meadow_proxy.authorization_headers,
            json={
                "protocol_major": 1,
                "continuity_generation_id": generation_id,
                "operation_id": "live-create",
                "logical_session_id": logical_session_id,
                "expected_canonical_workspace": workspace,
                "actor_ref": "integration-probe",
                "title": "Live continuity probe",
                "model_id": REQUIRED_LIVE_MODEL,
                "stable_instruction_digest": stable_digest,
            },
        )
        assert create_response.status_code == 200, create_response.text
        created = create_response.json()
        assert created["kind"] == "create_session"
        assert created["state"] == "completed"
        assert created["error"] is None
        assert created["result"]["logical_session_id"] == logical_session_id
        assert created["result"]["model_id"] == REQUIRED_LIVE_MODEL
        assert created["result"]["continuity_generation_id"] == generation_id
        backend_session_id = created["result"]["backend_session_id"]
        assert isinstance(backend_session_id, str) and backend_session_id

        output_contract = "Return only the requested value as plain text."
        output_contract_digest = hashlib.sha256(
            output_contract.encode("utf-8")
        ).hexdigest()
        initial_response = http.post(
            f"/meadow/v1/sessions/{logical_session_id}/requests",
            headers=meadow_proxy.authorization_headers,
            json={
                "protocol_major": 1,
                "continuity_generation_id": generation_id,
                "operation_id": "live-initial",
                "invocation_id": "live-invocation-one",
                "phase": "initial",
                "stable_instruction_digest": stable_digest,
                "output_contract_digest": output_contract_digest,
                "execution_timeout_s": 120,
                "stable_instructions": stable_instructions,
                "prompt": (
                    "Remember the continuity marker DIRECT_CONTINUITY_OK. "
                    "Reply with exactly: DIRECT_INITIAL_OK.\n\n"
                    "## Legal Typed Routes\nNo routes are available."
                ),
                "output_contract": output_contract,
            },
        )
        assert initial_response.status_code == 200, initial_response.text
        initial = initial_response.json()
        assert initial["kind"] == "prompt"
        assert initial["state"] == "completed"
        assert initial["error"] is None
        initial_result = initial["result"]
        assert initial_result["logical_session_id"] == logical_session_id
        assert initial_result["backend_session_id"] == backend_session_id
        assert initial_result["model_id"] == REQUIRED_LIVE_MODEL
        assert initial_result["continuity_generation_id"] == generation_id
        assert "DIRECT_INITIAL_OK" in initial_result["response_text"]
        assert initial_result["acp_stop_reason"] == "end_turn"
        assert initial_result["instruction_submission"] == "submitted_once"
        assert initial_result["stable_instruction_digest"] == stable_digest
        assert initial_result["output_contract_digest"] == output_contract_digest

        later_response = http.post(
            f"/meadow/v1/sessions/{logical_session_id}/requests",
            headers=meadow_proxy.authorization_headers,
            json={
                "protocol_major": 1,
                "continuity_generation_id": generation_id,
                "operation_id": "live-later",
                "invocation_id": "live-invocation-two",
                "phase": "invocation",
                "stable_instruction_digest": stable_digest,
                "output_contract_digest": output_contract_digest,
                "execution_timeout_s": 120,
                "prompt": (
                    "Reply with exactly the continuity marker from the preceding "
                    "request.\n\n## Legal Typed Routes\nNo routes are available."
                ),
                "output_contract": output_contract,
            },
        )
        assert later_response.status_code == 200, later_response.text
        later = later_response.json()
        assert later["kind"] == "prompt"
        assert later["state"] == "completed"
        assert later["error"] is None
        later_result = later["result"]
        assert later_result["logical_session_id"] == logical_session_id
        assert later_result["backend_session_id"] == backend_session_id
        assert later_result["model_id"] == REQUIRED_LIVE_MODEL
        assert later_result["continuity_generation_id"] == generation_id
        assert "DIRECT_CONTINUITY_OK" in later_result["response_text"]
        assert later_result["acp_stop_reason"] == "end_turn"
        assert later_result["instruction_submission"] == (
            "not_resubmitted_same_session"
        )

        status_response = http.get(
            "/meadow/v1/operations/live-later",
            headers=meadow_proxy.authorization_headers,
            params={
                "protocol_major": 1,
                "continuity_generation_id": generation_id,
            },
        )
        assert status_response.status_code == 200, status_response.text
        assert status_response.json() == later


def test_opencode_legacy_proxy_http_roundtrip(legacy_proxy: LiveProxy) -> None:
    """The deprecated adapter is exercised through its real CLI and TCP API."""

    with httpx.Client(
        base_url=legacy_proxy.base_url,
        timeout=_HTTP_TIMEOUT,
        trust_env=False,
    ) as http:
        direct_route = http.get("/meadow/v1/capabilities")
        assert direct_route.status_code == 410, direct_route.text
        assert direct_route.json()["error"]["code"] == (
            "meadow_direct_mode_required"
        )

        model_response = http.get("/v1/models")
        assert model_response.status_code == 200, model_response.text
        models = model_response.json()
        assert models["object"] == "list"
        assert REQUIRED_LIVE_MODEL in {model["id"] for model in models["data"]}

        completion_response = http.post(
            "/v1/chat/completions",
            json={
                "model": REQUIRED_LIVE_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": "Reply with exactly: LEGACY_HTTP_OK",
                    }
                ],
                "stream": False,
            },
        )
        assert completion_response.status_code == 200, completion_response.text
        completion = completion_response.json()
        assert completion["choices"][0]["finish_reason"] == "stop"
        assert "LEGACY_HTTP_OK" in completion["choices"][0]["message"]["content"]
