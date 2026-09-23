#!/usr/bin/env python3
"""Run Meadow Bridge's complete repository checkout gate.

The executable inventory is intentionally centralized here so agents and
developers invoke one standard-library Python entry point instead of copying a
changing set of validation commands. Every step runs from the repository root,
is judged by its own exit status, and writes combined stdout/stderr to a
retained log. Successful child output stays in that log; failures receive a
bounded console excerpt plus the full log path.

Change-relative typing and root-project validation are separate suites with
separate log directories. Change-relative typing compares the checkout with
``refs/heads/main``. Tests include the real copilot-language-server integration
suite and run serially to bound external service load.

Run ``python3 scripts/checkout_gate.py`` (or ``python`` on Windows).
``--list`` prints the inventory without executing it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_CHANGE_BASE = "refs/heads/main"
UV = ("uv", "--cache-dir", ".uv-cache")
UV_RUN = UV + ("run", "--no-sync", "--no-env-file")
PASS = "PASS"
FAIL = "FAIL"
FAILURE_TAIL_LINES = 60
REMOVED_SUBPROCESS_ENV = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "UV_CONFIG_FILE",
    "UV_ENV_FILE",
    "UV_NO_CONFIG",
    "UV_NO_PROJECT",
    "UV_PROJECT",
    "UV_PROJECT_ENVIRONMENT",
    "UV_WORKING_DIR",
)


@dataclass(frozen=True)
class Step:
    """One independently judged validation command."""

    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Suite:
    """A reporting and log boundary for related validation steps."""

    key: str
    title: str
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class Outcome:
    """Observed result of one command or final checkout-state check."""

    label: str
    status: str
    exit_code: int | None
    seconds: float
    argv: tuple[str, ...] = ()
    detail: str | None = None
    log_path: Path | None = None


@dataclass(frozen=True)
class CheckoutSnapshot:
    """Content-sensitive state used to detect mutations made by the gate."""

    head_oid: bytes
    head_reference: bytes
    porcelain: bytes
    tracked_diff: bytes
    tracked_paths: tuple[str, ...]
    tracked_files: tuple[tuple[str, str], ...]
    untracked_files: tuple[tuple[str, str], ...]


class CheckoutInspectionError(RuntimeError):
    """Raised when Git cannot provide a trustworthy checkout snapshot."""


SUITES: tuple[Suite, ...] = (
    Suite(
        key="environment",
        title="environment",
        steps=(
            Step("uv-sync", UV + ("sync", "--locked", "--extra", "dev")),
        ),
    ),
    Suite(
        key="change-relative-typing",
        title="change-relative typing",
        steps=(
            Step(
                "mypy-pyrefly",
                UV_RUN
                + (
                    "python",
                    "scripts/typecheck_change.py",
                    "--base",
                    CANONICAL_CHANGE_BASE,
                ),
            ),
        ),
    ),
    Suite(
        key="root",
        title="root project",
        steps=(
            Step(
                "pytest",
                UV_RUN + ("pytest", "tests"),
            ),
            Step(
                "ruff",
                UV_RUN
                + (
                    "ruff",
                    "check",
                    ".",
                ),
            ),
        ),
    ),
)


def _resolve(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    """Resolve a command executable without invoking a shell."""
    executable = shutil.which(argv[0], path=os.environ.get("PATH"))
    if executable is None:
        return None
    return (executable,) + argv[1:]


def _validation_environment() -> dict[str, str]:
    """Return context without Git, pytest, or uv project-selection injection."""
    environment = os.environ.copy()
    for name in REMOVED_SUBPROCESS_ENV:
        environment.pop(name, None)
    return environment


def _run_step(step: Step, suite: Suite, log_directory: Path) -> Outcome:
    """Run one step, capturing all child output in its suite log."""
    label = f"{suite.key}/{step.name}"
    suite_log_directory = log_directory / suite.key.replace("/", "-")
    suite_log_directory.mkdir(parents=True, exist_ok=True)
    log_path = suite_log_directory / f"{step.name}.log"
    resolved = _resolve(step.argv)
    if resolved is None:
        detail = f"executable not found: {step.argv[0]}"
        log_path.write_text(detail + "\n", encoding="utf-8")
        return Outcome(
            label=label,
            status=FAIL,
            exit_code=None,
            seconds=0.0,
            argv=step.argv,
            detail=detail,
            log_path=log_path,
        )

    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            completed = subprocess.run(
                resolved,
                cwd=REPOSITORY_ROOT,
                env=_validation_environment(),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
    except OSError as exc:
        seconds = time.monotonic() - started
        log_path.write_text(f"could not start command: {exc}\n", encoding="utf-8")
        return Outcome(
            label=label,
            status=FAIL,
            exit_code=None,
            seconds=seconds,
            argv=step.argv,
            detail=f"could not start command: {exc}",
            log_path=log_path,
        )

    seconds = time.monotonic() - started
    return Outcome(
        label=label,
        status=PASS if completed.returncode == 0 else FAIL,
        exit_code=completed.returncode,
        seconds=seconds,
        argv=step.argv,
        log_path=log_path,
    )


def _git_output(*arguments: str) -> bytes:
    """Run one read-only Git inspection command or raise an actionable error."""
    argv = ("git", *arguments)
    resolved = _resolve(argv)
    if resolved is None:
        raise CheckoutInspectionError("executable not found: git")
    try:
        completed = subprocess.run(
            resolved,
            cwd=REPOSITORY_ROOT,
            env=_validation_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise CheckoutInspectionError(f"could not start git: {exc}") from exc
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode(errors="replace").strip()
        raise CheckoutInspectionError(
            f"{shlex.join(argv)} exited {completed.returncode}: {diagnostic}"
        )
    return completed.stdout


def _nul_paths(output: bytes) -> tuple[str, ...]:
    """Decode Git's NUL-delimited repository-relative path output."""
    return tuple(
        os.fsdecode(path) for path in output.split(b"\0") if path
    )


def _working_path_digest(relative_path: str) -> str:
    """Hash one working path without following a symlink outside the checkout."""
    path = REPOSITORY_ROOT / relative_path
    digest = hashlib.sha256()
    try:
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.fsencode(os.readlink(path)))
            return digest.hexdigest()
        if path.is_file():
            digest.update(f"file\0{path.stat().st_mode}\0".encode())
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest()
        stat_result = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise CheckoutInspectionError(
            f"could not fingerprint working path {relative_path!r}: {exc}"
        ) from exc
    digest.update(f"special\0{stat_result.st_mode}\0{stat_result.st_size}".encode())
    return digest.hexdigest()


def _checkout_snapshot() -> CheckoutSnapshot:
    """Read enough Git and file state to catch gate-created checkout mutations."""
    head_oid = _git_output("rev-parse", "--verify", "HEAD").strip()
    head_reference = _git_output(
        "rev-parse",
        "--symbolic-full-name",
        "HEAD",
    ).strip()
    porcelain = _git_output(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    tracked_diff = _git_output("diff", "--binary", "--no-ext-diff", "HEAD", "--")
    tracked_paths = _nul_paths(
        _git_output("diff", "--name-only", "-z", "HEAD", "--")
    )
    tracked_files = tuple(
        (path, _working_path_digest(path)) for path in tracked_paths
    )
    untracked_paths = _nul_paths(
        _git_output("ls-files", "--others", "--exclude-standard", "-z")
    )
    untracked_files = tuple(
        (path, _working_path_digest(path)) for path in untracked_paths
    )
    return CheckoutSnapshot(
        head_oid=head_oid,
        head_reference=head_reference,
        porcelain=porcelain,
        tracked_diff=tracked_diff,
        tracked_paths=tracked_paths,
        tracked_files=tracked_files,
        untracked_files=untracked_files,
    )


def _checkout_change_detail(
    before: CheckoutSnapshot,
    after: CheckoutSnapshot,
) -> str:
    """Describe the observable checkout state changed during the gate."""
    details: list[str] = []
    if before.head_oid != after.head_oid:
        details.append(
            "HEAD commit changed during the gate: "
            f"{before.head_oid.decode(errors='replace')} -> "
            f"{after.head_oid.decode(errors='replace')}"
        )
    if before.head_reference != after.head_reference:
        details.append(
            "HEAD reference changed during the gate: "
            f"{before.head_reference.decode(errors='replace')} -> "
            f"{after.head_reference.decode(errors='replace')}"
        )
    if before.tracked_files != after.tracked_files:
        before_files = dict(before.tracked_files)
        after_files = dict(after.tracked_files)
        paths = sorted(
            path
            for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)
        )
        details.append(
            "tracked content changed during the gate: " + ", ".join(paths)
        )
    elif before.tracked_diff != after.tracked_diff:
        paths = sorted(set(before.tracked_paths) | set(after.tracked_paths))
        path_text = ", ".join(paths) if paths else "unknown tracked path"
        details.append(f"tracked diff metadata changed during the gate: {path_text}")
    if before.untracked_files != after.untracked_files:
        before_files = dict(before.untracked_files)
        after_files = dict(after.untracked_files)
        changed = sorted(
            path
            for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)
        )
        details.append(
            "untracked content changed during the gate: " + ", ".join(changed)
        )
    if before.porcelain != after.porcelain and not details:
        details.append("git status changed during the gate")
    return "; ".join(details)


def _print_failure_log(log_path: Path) -> None:
    """Print a bounded tail while retaining the complete failure log on disk."""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    omitted = max(0, len(lines) - FAILURE_TAIL_LINES)
    if omitted:
        print(f"    | ... {omitted} earlier lines omitted; see the full log", flush=True)
    for line in lines[-FAILURE_TAIL_LINES:]:
        print(f"    | {line}", flush=True)


def _print_outcome(outcome: Outcome) -> None:
    """Emit one concise status line and failure-only diagnostics."""
    exit_text = "-" if outcome.exit_code is None else str(outcome.exit_code)
    line = (
        f"[{outcome.status:^7}] {outcome.label}"
        f" (exit {exit_text}, {outcome.seconds:.1f}s)"
    )
    if outcome.detail:
        line += f" — {outcome.detail}"
    print(line, flush=True)
    if outcome.status != FAIL:
        return
    if outcome.argv:
        print(f"    command: {shlex.join(outcome.argv)}", flush=True)
        print(f"    cwd: {REPOSITORY_ROOT}", flush=True)
    if outcome.log_path is not None:
        print(f"    log: {outcome.log_path}", flush=True)
        _print_failure_log(outcome.log_path)


def _print_inventory() -> None:
    """Print the suite-separated command inventory without executing it."""
    for suite in SUITES:
        print(f"{suite.title}:")
        for step in suite.steps:
            print(f"  {step.name}: {shlex.join(step.argv)}")
    print("final: checkout state must be unchanged by the gate run")


def _configure_console_output() -> None:
    """Keep diagnostics printable under strict redirected platform codepages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")


def main() -> int:
    """Run every suite and return zero only when every obligation passes."""
    _configure_console_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the complete step inventory without running it",
    )
    arguments = parser.parse_args()
    if arguments.list:
        _print_inventory()
        return 0

    log_directory = Path(
        tempfile.mkdtemp(prefix="meadow-bridge-checkout-gate-")
    ).resolve()
    step_count = sum(len(suite.steps) for suite in SUITES)
    print(
        f"repository checkout gate: {step_count} steps from {REPOSITORY_ROOT}; "
        f"logs in {log_directory}",
        flush=True,
    )

    try:
        before = _checkout_snapshot()
    except CheckoutInspectionError as exc:
        inspection_log = log_directory / "checkout-inspection.log"
        inspection_log.write_text(str(exc) + "\n", encoding="utf-8")
        outcome = Outcome(
            label="checkout-inspection",
            status=FAIL,
            exit_code=None,
            seconds=0.0,
            detail=str(exc),
            log_path=inspection_log,
        )
        _print_outcome(outcome)
        print("\nsummary: 0 passed, 1 failed", flush=True)
        return 1

    outcomes: list[Outcome] = []
    try:
        for suite in SUITES:
            print(f"\n{suite.title}", flush=True)
            for step in suite.steps:
                outcome = _run_step(step, suite, log_directory)
                outcomes.append(outcome)
                _print_outcome(outcome)
    except KeyboardInterrupt:
        print(f"\ninterrupted; retained logs in {log_directory}", flush=True)
        return 130

    try:
        after = _checkout_snapshot()
        change_detail = _checkout_change_detail(before, after)
        checkout_outcome = Outcome(
            label="checkout-unchanged",
            status=FAIL if change_detail else PASS,
            exit_code=None if change_detail else 0,
            seconds=0.0,
            detail=change_detail or None,
            log_path=(log_directory / "checkout-unchanged.log")
            if change_detail
            else None,
        )
        if checkout_outcome.log_path is not None:
            checkout_outcome.log_path.write_text(
                change_detail + "\n",
                encoding="utf-8",
            )
    except CheckoutInspectionError as exc:
        checkout_log = log_directory / "checkout-unchanged.log"
        checkout_log.write_text(str(exc) + "\n", encoding="utf-8")
        checkout_outcome = Outcome(
            label="checkout-unchanged",
            status=FAIL,
            exit_code=None,
            seconds=0.0,
            detail=f"could not inspect final checkout state: {exc}",
            log_path=checkout_log,
        )
    outcomes.append(checkout_outcome)
    print()
    _print_outcome(checkout_outcome)

    failures = [outcome for outcome in outcomes if outcome.status == FAIL]
    print(
        f"\nsummary: {len(outcomes) - len(failures)} passed, "
        f"{len(failures)} failed",
        flush=True,
    )
    for outcome in failures:
        print(f"  FAILED: {outcome.label}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
