"""Exercise the public checkout gate against disposable, real Git checkouts."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest


GATE_SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "checkout_gate.py"


def _command_path(directory: Path, name: str) -> Path:
    return directory / (f"{name}.cmd" if os.name == "nt" else name)


def _install_python_command(directory: Path, name: str, source: str) -> None:
    script = directory / f"_{name}.py"
    script.write_text(source, encoding="utf-8")
    launcher = _command_path(directory, name)
    command = (
        f'@"{sys.executable}" "{script}" %*\n'
        if os.name == "nt"
        else (
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} \"$@\"\n"
        )
    )
    launcher.write_text(command, encoding="utf-8")
    launcher.chmod(0o755)


@dataclass(frozen=True)
class GateCheckout:
    root: Path
    executables: Path
    calls: Path
    git: str

    def run(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        child_environment = os.environ.copy()
        child_environment["PATH"] = str(self.executables)
        child_environment["ACP_GATE_TEST_CALLS"] = str(self.calls)
        if environment is not None:
            child_environment.update(environment)
        return subprocess.run(
            (sys.executable, str(self.root / "scripts/checkout_gate.py"), *arguments),
            cwd=self.root.parent,
            env=child_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )

    def git_command(self, *arguments: str) -> str:
        environment = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            environment.pop(name, None)
        return subprocess.run(
            (self.git, *arguments),
            cwd=self.root,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout

    def install_uv(self, body: str = "") -> None:
        _install_python_command(
            self.executables,
            "uv",
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "with Path(os.environ['ACP_GATE_TEST_CALLS']).open('a') as calls:\n"
            "    calls.write(' '.join(sys.argv[1:]) + '\\n')\n"
            + textwrap.dedent(body),
        )


@pytest.fixture
def gate_checkout(tmp_path: Path) -> GateCheckout:
    git = shutil.which("git")
    assert git is not None, "checkout gate tests require Git on PATH"
    root = tmp_path / "checkout"
    root.mkdir()
    scripts = root / "scripts"
    scripts.mkdir()
    shutil.copyfile(GATE_SOURCE, scripts / "checkout_gate.py")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(".uv-cache/\n", encoding="utf-8")
    executables = tmp_path / "bin"
    executables.mkdir()
    _install_python_command(
        executables,
        "git",
        f"import os\nimport sys\nos.execv({git!r}, [{git!r}, *sys.argv[1:]])\n",
    )
    checkout = GateCheckout(root, executables, tmp_path / "calls.txt", git)
    checkout.git_command("init", "--initial-branch=main")
    checkout.git_command("add", ".")
    checkout.git_command(
        "-c",
        "user.name=Gate Test",
        "-c",
        "user.email=gate-test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "Initial test checkout",
    )
    return checkout


def _log_directory(result: subprocess.CompletedProcess[str]) -> Path:
    first_line = result.stdout.splitlines()[0]
    _, separator, path = first_line.partition("; logs in ")
    assert separator, result.stdout
    return Path(path)


def test_list_needs_no_git_or_validation_tools(gate_checkout: GateCheckout) -> None:
    _command_path(gate_checkout.executables, "git").unlink()
    result = gate_checkout.run("--list")
    assert result.returncode == 0, result.stdout
    assert "checkout state must be unchanged" in result.stdout
    assert not gate_checkout.calls.exists()


def test_missing_git_fails_before_validation(gate_checkout: GateCheckout) -> None:
    gate_checkout.install_uv()
    _command_path(gate_checkout.executables, "git").unlink()
    result = gate_checkout.run()
    assert result.returncode == 1, result.stdout
    assert "executable not found: git" in result.stdout
    assert "[ FAIL  ] checkout-inspection" in result.stdout
    assert not gate_checkout.calls.exists()
    assert "executable not found: git" in (
        _log_directory(result) / "checkout-inspection.log"
    ).read_text(encoding="utf-8")


def test_missing_uv_is_reported_with_retained_logs(gate_checkout: GateCheckout) -> None:
    result = gate_checkout.run()
    assert result.returncode == 1, result.stdout
    assert "executable not found: uv" in result.stdout
    assert "FAILED: root/ruff" in result.stdout
    assert "[ PASS  ] checkout-unchanged" in result.stdout
    assert "executable not found: uv" in (
        _log_directory(result) / "environment/uv-sync.log"
    ).read_text(encoding="utf-8")


def test_unstartable_command_reports_os_error(gate_checkout: GateCheckout) -> None:
    executable = gate_checkout.executables / ("uv.exe" if os.name == "nt" else "uv")
    executable.write_text("not an executable format\n", encoding="utf-8")
    executable.chmod(0o755)
    result = gate_checkout.run()
    assert result.returncode == 1, result.stdout
    assert "could not start command" in result.stdout
    assert "could not start command" in (
        _log_directory(result) / "environment/uv-sync.log"
    ).read_text(encoding="utf-8")


def test_failure_retains_full_output_and_runs_remaining_checks(
    gate_checkout: GateCheckout,
) -> None:
    gate_checkout.install_uv(
        """
        if 'sync' in sys.argv:
            for number in range(100):
                print(f'failure-line-{number}', flush=True)
            print('failure-stderr', file=sys.stderr)
            sys.exit(23)
        print('successful child output')
        """
    )
    result = gate_checkout.run()
    assert result.returncode == 1, result.stdout
    assert "exit 23" in result.stdout
    assert "earlier lines omitted; see the full log" in result.stdout
    assert "failure-line-0\n" not in result.stdout
    assert "failure-line-99" in result.stdout
    assert "failure-stderr" in result.stdout
    assert "successful child output" not in result.stdout
    logs = _log_directory(result)
    failure = (logs / "environment/uv-sync.log").read_text(encoding="utf-8")
    assert "failure-line-0\n" in failure
    assert "failure-line-99\n" in failure
    assert "failure-stderr\n" in failure
    assert (logs / "root/ruff.log").read_text(encoding="utf-8") == (
        "successful child output\n"
    )


def test_gate_isolates_project_selection_and_preserves_existing_edits(
    gate_checkout: GateCheckout,
) -> None:
    dirty = gate_checkout.root / "tracked.txt"
    dirty.write_text("existing edit\n", encoding="utf-8")
    untracked = gate_checkout.root / "notes.txt"
    untracked.write_text("existing notes\n", encoding="utf-8")
    before = gate_checkout.git_command("status", "--porcelain=v1")
    gate_checkout.install_uv(
        """
        forbidden = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE',
                     'PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'UV_PROJECT',
                     'UV_ENV_FILE', 'UV_PROJECT_ENVIRONMENT')
        assert not any(name in os.environ for name in forbidden), os.environ.keys()
        assert Path.cwd().name == 'checkout', Path.cwd()
        assert os.environ['ACP_GATE_TEST_KEEP'] == 'preserved'
        cache = Path('.uv-cache')
        cache.mkdir(exist_ok=True)
        (cache / 'ignored.txt').write_text('allowed cache write')
        """
    )
    result = gate_checkout.run(
        environment={
            "GIT_DIR": str(gate_checkout.root.parent / "wrong.git"),
            "GIT_WORK_TREE": str(gate_checkout.root.parent),
            "GIT_INDEX_FILE": str(gate_checkout.root.parent / "wrong-index"),
            "PYTEST_ADDOPTS": "--collect-only -k no_tests",
            "PYTEST_PLUGINS": "missing_plugin",
            "UV_PROJECT": str(gate_checkout.root.parent),
            "UV_ENV_FILE": str(gate_checkout.root.parent / "missing.env"),
            "UV_PROJECT_ENVIRONMENT": str(gate_checkout.root.parent / "wrong-venv"),
            "ACP_GATE_TEST_KEEP": "preserved",
        }
    )
    assert result.returncode == 0, result.stdout
    assert "0 failed" in result.stdout
    assert gate_checkout.git_command("status", "--porcelain=v1") == before
    assert dirty.read_text(encoding="utf-8") == "existing edit\n"
    assert untracked.read_text(encoding="utf-8") == "existing notes\n"
    assert (gate_checkout.root / ".uv-cache/ignored.txt").exists()


@pytest.mark.parametrize("path", ["tracked.txt", "notes.txt"])
def test_detects_content_mutation_even_when_git_status_is_unchanged(
    gate_checkout: GateCheckout,
    path: str,
) -> None:
    target = gate_checkout.root / path
    target.write_text("before validation\n", encoding="utf-8")
    before = gate_checkout.git_command("status", "--porcelain=v1")
    gate_checkout.install_uv(
        f"Path({path!r}).write_text('changed by validation\\n')\n"
    )
    result = gate_checkout.run()
    assert result.returncode == 1, result.stdout
    assert gate_checkout.git_command("status", "--porcelain=v1") == before
    assert "FAILED: checkout-unchanged" in result.stdout
    diagnostic = (_log_directory(result) / "checkout-unchanged.log").read_text(
        encoding="utf-8"
    )
    assert "content changed during the gate" in diagnostic
    assert path in diagnostic


def test_detects_branch_change_with_identical_contents(
    gate_checkout: GateCheckout,
) -> None:
    gate_checkout.install_uv(
        """
        if 'sync' in sys.argv:
            import shutil
            import subprocess
            git = shutil.which('git')
            assert git is not None
            subprocess.run((git, 'switch', '-c', 'validation'), check=True)
        """
    )
    result = gate_checkout.run()
    assert result.returncode == 1, result.stdout
    assert "HEAD reference changed during the gate" in result.stdout
    assert "FAILED: checkout-unchanged" in result.stdout
