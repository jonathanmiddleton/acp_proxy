"""Black-box integration tests for the bridge's public process contracts.

The tests launch the real CLI and observe only process exit, readiness
metadata, and TCP HTTP.  Meadow startup owns credential setup, binary
admission, native startup, model catalog admission, service construction, and wiring.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
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
from typing import BinaryIO

import httpx
import pytest

from meadow_bridge.discovery import BinaryCompatibilityError, find_binary
from meadow_bridge.direct_protocol import CapabilitiesResponse, OperationView, PromptResult
from meadow_bridge.json_types import JsonObject, json_object, parse_json

REQUIRED_LIVE_MODEL = "gpt-5.6-sol"
UNADVERTISED_LIVE_MODEL = "meadow-bridge-negative-control-model"
_DIRECT_SECRET_ENV = "MEADOW_BRIDGE_MEADOW_SECRET"
_COPILOT_TOKEN_ENV_NAMES = frozenset(
    {"GH_COPILOT_TOKEN", "GITHUB_COPILOT_TOKEN"}
)
_BRIDGE_START_TIMEOUT_S = 90.0
_BRIDGE_STOP_TIMEOUT_S = 20.0
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
class _Redactions:
    """Keep inherited credentials out of pytest argument representations."""

    values: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class LiveBridge:
    """One ready bridge process exposed only through its public boundary."""

    base_url: str
    metadata: JsonObject
    debug_log_path: Path
    workspace: Path
    launch_secret: str = field(repr=False)

    @property
    def authorization_headers(self) -> dict[str, str]:
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


def _redact(text: str, sensitive_values: _Redactions) -> str:
    redacted = text
    for value in sensitive_values.values:
        redacted = redacted.replace(value, "<redacted>")
    return redacted


def _files_contain_sensitive_value(
    log_path: Path, sensitive_values: _Redactions
) -> bool:
    """Scan a bounded rotating-log set without loading it into memory."""

    needles = tuple(value.encode("utf-8") for value in sensitive_values.values if value)
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

    def __init__(self, sensitive_values: _Redactions) -> None:
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()
        self._needles = tuple(
            value.encode("utf-8") for value in sensitive_values.values if value
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
    sensitive_values: _Redactions,
) -> str:
    return (
        f"bridge return code: {process.poll()}\n"
        f"bridge DEBUG log: {debug_log_path}\n"
        "bounded bridge console tail:\n"
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
    """Stop the owned bridge tree without leaving its native child behind."""

    if sys.platform == "win32":
        if process.poll() is None and graceful:
            try:
                # Windows process groups disable CTRL+C but accept CTRL+BREAK.
                process.send_signal(getattr(signal, "CTRL_BREAK_EVENT"))
                return process.wait(timeout=_BRIDGE_STOP_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired):
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
    else:
        return_code = process.poll()
        if return_code is None and graceful:
            process.send_signal(signal.SIGTERM)
            try:
                return_code = process.wait(timeout=_BRIDGE_STOP_TIMEOUT_S)
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
                        f"bridge process group {process_group_id} survived SIGKILL"
                    )
        return return_code


def _wait_for_readiness(
    process: subprocess.Popen[bytes],
    metadata_path: Path,
    console: _ConsoleCapture,
    debug_log_path: Path,
    sensitive_values: _Redactions,
) -> JsonObject:
    deadline = time.monotonic() + _BRIDGE_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                "bridge exited before readiness\n"
                + _process_diagnostic(
                    process, console, debug_log_path, sensitive_values
                )
            )
        if metadata_path.is_file():
            try:
                metadata = json_object(parse_json(metadata_path.read_bytes()))
            except (OSError, ValueError) as exc:
                raise AssertionError(
                    f"bridge readiness metadata is unreadable: {exc}"
                ) from exc
            if not isinstance(metadata, dict):
                raise AssertionError("bridge readiness metadata is not an object")
            return metadata
        time.sleep(0.05)

    raise AssertionError(
        f"bridge did not become ready within {_BRIDGE_START_TIMEOUT_S:.0f}s\n"
        + _process_diagnostic(process, console, debug_log_path, sensitive_values)
    )


def _wait_for_health(base_url: str) -> None:
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
        assert health["protocol_major"] == 2
        return
    raise AssertionError(f"bridge health endpoint did not become ready: {last_error}")


@contextmanager
def _running_bridge(
    *,
    binary: str,
    environment: Mapping[str, str],
    runtime_dir: Path,
    launch_secret: str,
) -> Iterator[LiveBridge]:
    """Launch the real CLI and yield only after its HTTP socket is ready."""

    metadata_path = runtime_dir / "ready.json"
    debug_log_path = runtime_dir / "bridge.log"
    workspace = runtime_dir / "workspace"
    workspace.mkdir(parents=True)
    command = [
        sys.executable,
        "-m",
        "meadow_bridge",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--cwd",
        str(workspace),
        "--binary",
        binary,
        "--metadata-file",
        str(metadata_path),
        "--log-file",
        str(debug_log_path),
        "--raw-event-file",
        str(runtime_dir / "native-events.jsonl"),
        "--log-level",
        "INFO",
    ]
    command.extend(["--execution-authority", "trusted-host"])

    process_env = _source_environment(environment)
    process_env[_DIRECT_SECRET_ENV] = launch_secret
    sensitive_values = _Redactions(tuple(
        dict.fromkeys((*_copilot_credential_values(process_env), launch_secret))
    ))

    creation_flag = 0
    if os.name == "nt":
        windows_creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
        if not isinstance(windows_creation_flag, int):
            raise TypeError("Windows subprocess creation flag must be an integer")
        creation_flag = windows_creation_flag

    runtime_dir.mkdir(parents=True, exist_ok=True)
    console = _ConsoleCapture(sensitive_values)
    process = subprocess.Popen(
        command,
        cwd=_REPOSITORY_ROOT,
        env=process_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creation_flag,
        start_new_session=os.name != "nt",
    )
    assert process.stdout is not None
    process_group_id = process.pid
    console_thread = threading.Thread(
        target=console.drain,
        args=(process.stdout,),
        name=f"bridge-console-{process.pid}",
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
        metadata_pid = metadata["pid"]
        assert type(metadata_pid) is int and metadata_pid > 0
        # A Windows venv python.exe is a redirector: Popen owns the launcher
        # and process group, while metadata correctly names the serving child.
        if os.name != "nt":
            assert metadata_pid == process.pid
        assert metadata["status"] == "ready"
        assert metadata["host"] == "127.0.0.1"
        port = metadata["port"]
        assert type(port) is int and 1 <= port <= 65535
        protocol_major = metadata["protocol_major"]
        assert type(protocol_major) is int and protocol_major == 2
        generation_id = metadata["continuity_generation_id"]
        assert isinstance(generation_id, str)
        assert _PATH_SAFE_GENERATION.fullmatch(generation_id) is not None
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(base_url)
        ready = True
        yield LiveBridge(
            base_url=base_url,
            metadata=metadata,
            launch_secret=launch_secret,
            debug_log_path=debug_log_path,
            workspace=workspace,
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
                    "ready bridge exited before fixture teardown\n"
                    + _process_diagnostic(
                        process, console, debug_log_path, sensitive_values
                    )
                )
            elif ready and return_code != 0:
                cleanup_errors.append(
                    f"ready bridge exited with status {return_code}\n"
                    + _process_diagnostic(
                        process, console, debug_log_path, sensitive_values
                    )
                )
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup_errors.append(f"could not stop bridge process tree: {exc}")

        console_thread.join(timeout=5.0)
        if console_thread.is_alive():
            cleanup_errors.append("bridge console drain thread did not stop")
        if console.drain_error is not None:
            cleanup_errors.append(
                f"bridge console drain failed: {console.drain_error}"
            )
        if ready and metadata_path.exists():
            cleanup_errors.append("ready bridge did not remove readiness metadata")
        if console.sensitive_value_seen or _files_contain_sensitive_value(
            debug_log_path, sensitive_values
        ):
            cleanup_errors.append("bridge diagnostics exposed a launch credential")

        if cleanup_errors:
            cleanup_message = "\n".join(cleanup_errors)
            if active_exception is not None:
                active_exception.add_note(
                    f"Additional bridge cleanup failure:\n{cleanup_message}"
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
def meadow_bridge(binary: str, tmp_path: Path) -> Iterator[LiveBridge]:
    """Run the production Meadow-direct process with its real OAuth setup."""

    launch_secret = secrets.token_urlsafe(32)
    with _running_bridge(
        binary=binary,
        environment=os.environ,
        runtime_dir=tmp_path / "meadow-direct",
        launch_secret=launch_secret,
    ) as bridge:
        yield bridge


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
            "meadow_bridge",
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
        log_path, _Redactions((launch_secret,))
    ):
        raise AssertionError("launch credential leaked into bridge diagnostics")
    assert not metadata_path.exists()


def test_meadow_direct_bridge_model_binding_and_continuity(
    meadow_bridge: LiveBridge,
) -> None:
    """Two real turns preserve context and settle create/edit/command callbacks."""

    with httpx.Client(
        base_url=meadow_bridge.base_url,
        headers=meadow_bridge.authorization_headers,
        timeout=httpx.Timeout(360.0, connect=5.0),
        trust_env=False,
    ) as http:
        unauthorized = http.get("/meadow/v2/capabilities", headers={"Authorization": ""})
        assert unauthorized.status_code == 401, unauthorized.text
        capability_response = http.get("/meadow/v2/capabilities")
        capability_response.raise_for_status()
        capabilities = CapabilitiesResponse.model_validate_json(capability_response.content)
        assert capabilities.protocol == "meadow-bridge-direct"
        assert capabilities.protocol_major == 2
        assert capabilities.continuity_generation_id == meadow_bridge.metadata[
            "continuity_generation_id"
        ]
        assert capabilities.canonical_workspace == str(meadow_bridge.workspace.resolve())
        assert capabilities.execution_authority.profile == "trusted-host"
        assert capabilities.execution_authority.effect_observation_scope == "bridge_callbacks"
        assert REQUIRED_LIVE_MODEL in capabilities.model_ids
        assert UNADVERTISED_LIVE_MODEL not in capabilities.model_ids
        assert http.get("/meadow/v1/capabilities").status_code == 404
        assert http.get("/v1/models").status_code == 404

        marker = "continuity-" + secrets.token_hex(16)
        nonce = "execution-" + secrets.token_hex(16)
        stable_instructions = (
            f"Remember the private continuity marker {marker}. "
            "Use the registered workspace tools for requested file and command effects. "
            "Report only actual execution results."
        )
        stable_digest = hashlib.sha256(stable_instructions.encode()).hexdigest()
        generation_id = capabilities.continuity_generation_id
        policy = {"version": 1, "mode": "allow_all"}
        policy_digest = hashlib.sha256(
            json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        common_create = {
            "protocol_major": 2,
            "continuity_generation_id": generation_id,
            "expected_canonical_workspace": capabilities.canonical_workspace,
            "actor_ref": "integration-probe",
            "title": "Native continuity and effects probe",
            "stable_instruction_digest": stable_digest,
            "permission_policy": policy,
        }
        negative = http.post(
            "/meadow/v2/sessions",
            json={**common_create, "operation_id": "negative-model-create",
                  "logical_session_id": "negative-model-session",
                  "model_id": UNADVERTISED_LIVE_MODEL},
        )
        assert negative.status_code == 409, negative.text
        create_response = http.post(
            "/meadow/v2/sessions",
            json={**common_create, "operation_id": "live-create",
                  "logical_session_id": "live-session", "model_id": REQUIRED_LIVE_MODEL},
        )
        create_response.raise_for_status()
        created = OperationView.model_validate_json(create_response.content)
        assert created.state == "completed", (created, meadow_bridge.debug_log_path)
        assert created.error is None
        assert created.result is not None
        assert created.result["logical_session_id"] == "live-session"
        assert created.result["backend_session_id"] is None
        assert created.result["binding_state"] == "allocated"
        assert created.result["permission_policy_digest"] == policy_digest
        assert not tuple(meadow_bridge.workspace.iterdir())

        initial_source = "print('initial')\n"
        final_source = f"print('{nonce}')\n"
        script = meadow_bridge.workspace / "hello_world.py"
        output_contract = "Return a brief plain-text result and any requested continuity marker."
        output_digest = hashlib.sha256(output_contract.encode()).hexdigest()
        common_prompt = {
            "protocol_major": 2, "continuity_generation_id": generation_id,
            "stable_instruction_digest": stable_digest,
            "output_contract_digest": output_digest,
            "execution_timeout_s": 300,
            "output_contract": output_contract,
        }
        initial_response = http.post(
            "/meadow/v2/sessions/live-session/requests",
            json={**common_prompt, "operation_id": "live-initial",
                  "invocation_id": "live-invocation-one", "phase": "initial",
                  "stable_instructions": stable_instructions,
                  "prompt": f"Create hello_world.py with exactly this UTF-8 content: {initial_source!r}. Do not edit or run it yet."},
        )
        initial_response.raise_for_status()
        initial = OperationView.model_validate_json(initial_response.content)
        assert initial.state == "completed", (initial, meadow_bridge.debug_log_path)
        assert initial.error is None and initial.result is not None
        initial_result = PromptResult.model_validate(initial.result)
        assert initial_result.stop_reason == "completed"
        assert initial_result.instruction_submission == "submitted_once"
        assert initial_result.stable_instruction_digest == stable_digest
        assert initial_result.output_contract_digest == output_digest
        assert script.read_bytes() == initial_source.encode()
        assert initial_result.effect_evidence == "observed"
        assert initial_result.effect_events
        backend_id = initial_result.backend_session_id

        if os.name == "nt":
            command = "& '" + sys.executable.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'"
        else:
            command = shlex.join([sys.executable, str(script)])
        later_response = http.post(
            "/meadow/v2/sessions/live-session/requests",
            json={**common_prompt, "operation_id": "live-later",
                  "invocation_id": "live-invocation-two", "phase": "invocation",
                  "prompt": (
                      f"Edit hello_world.py to exactly {final_source!r}. Then execute exactly "
                      f"this shell command once: {command}. Report its actual stdout and exit code, "
                      "and the private continuity marker from the initial instructions."
                  )},
        )
        later_response.raise_for_status()
        later = OperationView.model_validate_json(later_response.content)
        assert later.state == "completed", (later, meadow_bridge.debug_log_path)
        assert later.error is None and later.result is not None
        result = PromptResult.model_validate(later.result)
        assert result.logical_session_id == "live-session"
        assert result.backend_session_id == backend_id
        assert result.model_id == REQUIRED_LIVE_MODEL
        assert result.continuity_generation_id == generation_id
        assert result.stop_reason == "completed"
        assert result.instruction_submission == "not_resubmitted_same_session"
        assert marker in result.response_text
        assert nonce in result.response_text
        assert script.read_bytes() == final_source.encode()
        assert result.effect_evidence == "observed"
        assert len(result.effect_events) >= 2
        commands = [effect.receipt["command"] for effect in result.effects
                    if "command" in effect.receipt]
        assert len(commands) == 1
        command_receipt = commands[0]
        assert isinstance(command_receipt, dict)
        assert command_receipt["stdout"] in {nonce + "\n", nonce + "\r\n"}
        assert command_receipt["stderr"] == ""
        assert command_receipt["exit_code"] == 0
        assert command_receipt["stdout_truncated"] is False
        assert command_receipt["stderr_truncated"] is False
        assert command_receipt["timed_out"] is False
        assert result.permission_evidence.availability == "observed"
        assert result.permission_evidence.events == [
            event for event in result.events
            if event.update_type == "native.client_tool.confirmation"
        ]
        assert all(decision.allowed and decision.policy_digest == policy_digest
                   for decision in result.permission_evidence.decisions)
        status_response = http.get(
            "/meadow/v2/operations/live-later",
            params={"protocol_major": 2, "continuity_generation_id": generation_id},
        )
        status_response.raise_for_status()
        assert OperationView.model_validate_json(status_response.content) == later
        retired_response = http.post(
            "/meadow/v2/sessions/live-session/retire",
            json={"protocol_major": 2, "continuity_generation_id": generation_id,
                  "operation_id": "live-retire", "logical_session_id": "live-session"},
        )
        retired_response.raise_for_status()
        retired = OperationView.model_validate_json(retired_response.content)
        assert retired.state == "completed", retired
        assert retired.result is not None
        assert retired.result["backend_close"] == "destroyed"
