"""Immutable compatibility policy shared by every application entry point."""

from typing import Final

SemanticVersion = tuple[int, int, int]

MIN_COPILOT_LANGUAGE_SERVER_VERSION: Final[SemanticVersion] = (1, 518, 3)
"""The single application-wide minimum admitted language-server version."""
