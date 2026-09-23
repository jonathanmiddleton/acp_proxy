"""Shared test configuration."""

import sys
import os

import pytest

# Add src to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class _NoSkippedTests:
    """Make ADR-005 apply to collection and runtime skips alike."""

    def __init__(self) -> None:
        self.skipped = False

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.skipped = True

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped:
            self.skipped = True

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.skipped:
            print("\nSkipped tests violate ADR-005; this run cannot pass.")
            if exitstatus == pytest.ExitCode.OK:
                session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(_NoSkippedTests(), "acp-no-skips")
