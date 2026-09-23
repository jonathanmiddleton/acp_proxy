"""Exercise change-relative validation with real Git trees and type checkers."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = REPOSITORY_ROOT / "scripts" / "typecheck_change.py"
LEGACY_SOURCE = 'def legacy() -> int:\n    return "existing error"\n'
PYREFLY_CONFIGURATION = (
    '[tool.pyrefly]\n'
    'preset = "legacy"\n'
    'python_version = "3.11"\n'
    'search_path = ["src", "."]\n'
    '[tool.pyrefly.errors]\n'
    'no-any-return = "error"\n'
    'redundant-cast = "warn"\n'
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True, capture_output=True, text=True,
    ).stdout


def _commit(root: Path) -> None:
    _git(root, "add", ".")
    _git(
        root, "-c", "user.name=Typecheck Fixture", "-c",
        "user.email=typecheck@example.invalid", "commit", "--quiet", "-m", "fixture",
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for directory in ("scripts", "src/acp_proxy", "tests", "experiments/demo"):
        (root / directory).mkdir(parents=True)
    shutil.copyfile(VALIDATOR, root / "scripts/typecheck_change.py")
    (root / "src/acp_proxy/__init__.py").write_text("", encoding="utf-8")
    (root / "src/acp_proxy/sample.py").write_text(LEGACY_SOURCE, encoding="utf-8")
    (root / "tests/__init__.py").write_text("", encoding="utf-8")
    (root / "experiments/demo/check.py").write_text("VALUE: int = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0"\n', encoding="utf-8",
    )
    (root / ".gitignore").write_text("__pycache__/\n.mypy_cache/\n", encoding="utf-8")
    subprocess.run(
        ("git", "init", "--quiet", "--initial-branch=main", str(root)), check=True,
    )
    _commit(root)
    return root


@pytest.fixture
def checker_environment() -> dict[str, str]:
    environment = os.environ.copy()
    binary_directory = REPOSITORY_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    environment["PATH"] = os.pathsep.join((str(binary_directory), environment.get("PATH", "")))
    for checker in ("mypy", "pyrefly"):
        assert shutil.which(checker, path=environment["PATH"]) is not None, (
            f"{checker} is required; run uv sync --extra dev"
        )
    return environment


def _validate(
    checkout: Path, environment: dict[str, str], *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, str(checkout / "scripts/typecheck_change.py"), *arguments),
        cwd=checkout, env=environment, check=False, capture_output=True, text=True,
        timeout=120,
    )


def _assert_passed(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr
    assert "summary: 2 passed, 0 failed" in result.stdout


def _assert_both_failed(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 1, result.stdout + result.stderr
    assert "[ FAIL  ] mypy" in result.stdout
    assert "[ FAIL  ] pyrefly" in result.stdout
    assert "summary: 0 passed, 2 failed" in result.stdout


def test_unchanged_declaration_debt_survives_line_shifts(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    sample = checkout / "src/acp_proxy/sample.py"
    sample.write_text("def added() -> int:\n    return 1\n\n" + LEGACY_SOURCE, encoding="utf-8")
    before = _git(checkout, "diff", "--binary")

    result = _validate(checkout, checker_environment)

    _assert_passed(result)
    assert "existing outside the changed surface" in result.stdout
    assert _git(checkout, "diff", "--binary") == before
    assert _git(checkout, "status", "--short") == " M src/acp_proxy/sample.py\n"


@pytest.mark.parametrize("change", ["addition", "edit", "deletion"])
def test_new_errors_and_changed_declaration_debt_block(
    checkout: Path, checker_environment: dict[str, str], change: str,
) -> None:
    sample = checkout / "src/acp_proxy/sample.py"
    if change == "addition":
        sample.write_text(LEGACY_SOURCE + '\ndef broken() -> str:\n    return 1\n', encoding="utf-8")
    elif change == "edit":
        sample.write_text(LEGACY_SOURCE.replace("    return", "    unused = 1\n    return"), encoding="utf-8")
    else:
        sample.write_text(LEGACY_SOURCE.replace("    return", "    unused = 1\n    return"), encoding="utf-8")
        _commit(checkout)
        sample.write_text(LEGACY_SOURCE, encoding="utf-8")

    result = _validate(checkout, checker_environment)

    _assert_both_failed(result)
    assert ("new diagnostic" if change == "addition" else "affected declaration") in result.stdout


def test_removing_a_predecessor_does_not_own_the_untouched_successor(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    sample = checkout / "src/acp_proxy/sample.py"
    sample.write_text("def removable() -> int:\n    return 1\n\n" + LEGACY_SOURCE, encoding="utf-8")
    _commit(checkout)
    sample.write_text(LEGACY_SOURCE, encoding="utf-8")

    _assert_passed(_validate(checkout, checker_environment))


def test_base_and_current_src_imports_use_their_own_snapshot(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    api = checkout / "src/acp_proxy/api.py"
    api.write_text('def value() -> str:\n    return "base"\n', encoding="utf-8")
    (checkout / "tests/consumer.py").write_text(
        "from acp_proxy.api import value\nRESULT: str = value()\n", encoding="utf-8",
    )
    (checkout / "experiments/demo/check.py").write_text(
        "from acp_proxy.api import value\nRESULT: str = value()\n", encoding="utf-8",
    )
    _commit(checkout)
    api.write_text("def value() -> int:\n    return 1\n", encoding="utf-8")

    result = _validate(checkout, checker_environment)

    _assert_both_failed(result)
    assert "tests/consumer.py" in result.stdout
    assert "experiments/demo/check.py" in result.stdout
    assert "new diagnostic" in result.stdout
    assert "could not establish" not in result.stdout


def test_warm_imported_debt_keeps_snapshot_relative_diagnostic_paths(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    consumer = checkout / "tests/consumer.py"
    original = "from acp_proxy.sample import legacy\nVALUE: int = legacy()\n"
    consumer.write_text(original, encoding="utf-8")
    _commit(checkout)
    project = checkout / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8")
        + '[tool.mypy]\nmypy_path = "$MYPY_CONFIG_FILE_DIR/src"\n',
        encoding="utf-8",
    )
    consumer.write_text(original + "ADDED: int = 1\n", encoding="utf-8")

    for _ in range(2):
        result = _validate(checkout, checker_environment)
        _assert_passed(result)
        assert "new diagnostic" not in result.stdout


def test_script_change_checks_its_unchanged_test_consumer(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    helper = checkout / "scripts/helper.py"
    helper.write_text('VALUE: str = "base"\n', encoding="utf-8")
    (checkout / "tests/consumer.py").write_text(
        "from scripts.helper import VALUE\nRESULT: str = VALUE\n", encoding="utf-8",
    )
    _commit(checkout)
    helper.write_text("VALUE: int = 1\n", encoding="utf-8")

    result = _validate(checkout, checker_environment)

    _assert_both_failed(result)
    assert "scopes: scripts, tests" in result.stdout
    assert "tests/consumer.py" in result.stdout


def test_bootstrap_configuration_does_not_create_false_debt(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    (checkout / "src/acp_proxy/any_return.py").write_text(
        'import json\ndef value() -> int:\n    return json.loads("0")\n',
        encoding="utf-8",
    )
    _commit(checkout)
    project = checkout / "pyproject.toml"
    project.write_text(project.read_text(encoding="utf-8") + PYREFLY_CONFIGURATION, encoding="utf-8")

    result = _validate(checkout, checker_environment)

    _assert_passed(result)
    assert "scopes: experiments/demo, scripts, src, tests" in result.stdout


def test_configuration_severity_changes_remain_effective(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    sample = checkout / "src/acp_proxy/sample.py"
    sample.write_text(
        'import json\ndef value() -> int:\n    return json.loads("0")\n',
        encoding="utf-8",
    )
    project = checkout / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8")
        + PYREFLY_CONFIGURATION.replace('no-any-return = "error"', 'no-any-return = "warn"'),
        encoding="utf-8",
    )
    _commit(checkout)
    project.write_text(
        project.read_text(encoding="utf-8").replace('no-any-return = "warn"', 'no-any-return = "error"'),
        encoding="utf-8",
    )

    result = _validate(checkout, checker_environment)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "[ PASS  ] mypy" in result.stdout
    assert "[ FAIL  ] pyrefly" in result.stdout
    assert "no-any-return" in result.stdout
    assert "new diagnostic" in result.stdout


def test_default_main_covers_committed_changes_despite_feature_upstream(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    _git(checkout, "switch", "--quiet", "--create", "feature")
    (checkout / "src/acp_proxy/new.py").write_text('VALUE: int = "wrong"\n', encoding="utf-8")
    _commit(checkout)
    _git(checkout, "branch", "feature-upstream", "HEAD")
    _git(checkout, "branch", "--set-upstream-to=feature-upstream", "feature")

    result = _validate(checkout, checker_environment)

    _assert_both_failed(result)
    assert "relative to refs/heads/main" in result.stdout
    assert "new diagnostic" in result.stdout


def test_missing_main_requires_an_explicit_comparison_base(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    _git(checkout, "branch", "--move", "feature")

    result = _validate(checkout, checker_environment)

    assert result.returncode == 2
    assert "canonical comparison ref refs/heads/main is unavailable" in result.stderr
    assert "pass --base" in result.stderr
    explicit = _validate(checkout, checker_environment, "--base", "HEAD")
    assert explicit.returncode == 0, explicit.stdout + explicit.stderr
    assert "no maintained Python changes" in explicit.stdout


def test_warm_cache_rechecks_same_size_and_timestamp_edits(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    sample = checkout / "src/acp_proxy/sample.py"
    valid = "VALUE: int = 1\n"
    sample.write_text(valid, encoding="utf-8")
    _commit(checkout)
    (checkout / "src/acp_proxy/added.py").write_text("", encoding="utf-8")
    _assert_passed(_validate(checkout, checker_environment))
    git_directory = Path(_git(checkout, "rev-parse", "--absolute-git-dir").strip())
    cache = git_directory / "acp-proxy-typecheck-cache"
    assert tuple(cache.rglob("*.db")) or tuple(cache.rglob("*.json"))
    timestamp = int(_git(checkout, "show", "-s", "--format=%ct", "HEAD"))
    sample.write_text(valid.replace("int", "str"), encoding="utf-8")
    os.utime(sample, (timestamp, timestamp))
    before = _git(checkout, "diff", "--binary")
    before_stat = sample.stat()

    result = _validate(checkout, checker_environment)

    _assert_both_failed(result)
    assert _git(checkout, "diff", "--binary") == before
    assert sample.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert sample.stat().st_size == before_stat.st_size


def test_invalid_checker_configuration_is_infrastructure_failure(
    checkout: Path, checker_environment: dict[str, str],
) -> None:
    (checkout / "pyrefly.toml").write_text("invalid = [\n", encoding="utf-8")

    result = _validate(checkout, checker_environment)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "[ PASS  ] mypy" in result.stdout
    assert "[ FAIL  ] pyrefly" in result.stdout
    assert "could not read Pyrefly configuration" in result.stdout
