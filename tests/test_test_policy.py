"""Prove the no-skips policy through isolated pytest process outcomes."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("extra_test", "expected_status"),
    [
        ("def test_another_pass() -> None:\n    assert True\n", 0),
        # These deliberately invalid child suites prove the rejection boundary;
        # they do not skip any test in the maintained suite.
        (
            "import pytest\ndef test_runtime_skip() -> None:\n"
            "    pytest.skip('missing prerequisite')\n",
            1,
        ),
        ("import pytest\npytest.skip('missing prerequisite', allow_module_level=True)\n", 1),
    ],
    ids=["passing-suite", "runtime-skip", "collection-skip"],
)
def test_skips_prevent_successful_pytest_exit(
    tmp_path: Path, extra_test: str, expected_status: int
) -> None:
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_pass.py").write_text(
        "def test_pass() -> None:\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "test_extra.py").write_text(extra_test, encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    completed = subprocess.run(
        (sys.executable, "-m", "pytest", "-q", str(tmp_path)),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == expected_status, completed.stdout + completed.stderr
    if expected_status:
        assert "Skipped tests violate ADR-005" in completed.stdout
