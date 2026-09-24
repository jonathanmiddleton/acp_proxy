"""
User configuration for the Meadow Bridge.

Loads settings from ``~/.meadow_bridge/config.json``.  This file is per-user,
not per-project — it stores environment-specific settings like proxy
configuration that apply to all repos on the machine.

Environment variables take precedence over config file values.  This lets
users override per-session without editing the file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping

logger = logging.getLogger(__name__)

_CONFIG_DIR = ".meadow_bridge"
_CONFIG_FILE = "config.json"

# POSIX consumers use both proxy spellings; Windows composition emits one
# canonical key because its environment names are case-insensitive.
_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "NO_PROXY",
    "no_proxy",
)


def config_dir() -> str:
    """Return the path to the config directory (``~/.meadow_bridge/``)."""
    return os.path.join(os.path.expanduser("~"), _CONFIG_DIR)


def config_path() -> str:
    """Return the path to the config file."""
    return os.path.join(config_dir(), _CONFIG_FILE)


def ensure_default_config() -> str:
    """Create the default config file if it doesn't exist.

    Returns the path to the config file.  If the file already exists,
    it is not modified.
    """
    path = config_path()
    if os.path.isfile(path):
        return path

    directory = config_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w") as f:
            json.dump(_DEFAULT_CONFIG, f, indent=2)
            f.write("\n")
        logger.info("Created default config at %s", path)
    except OSError as e:
        logger.warning("Could not create default config at %s: %s", path, e)

    return path


def load_config() -> dict[str, object]:
    """Load user configuration from disk.

    On first run, creates a default config file at ``~/.meadow_bridge/config.json``
    with default network proxy fields.

    Returns an empty dict if the config file cannot be read.
    Logs a warning if the file is malformed.
    """
    ensure_default_config()

    path = config_path()
    if not os.path.isfile(path):
        logger.debug("No config file at %s", path)
        return {}

    try:
        with open(path) as f:
            data: object = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Config file %s is not a JSON object, ignoring", path)
            return {}
        logger.info("Loaded config from %s", path)
        return {key: value for key, value in data.items() if isinstance(key, str)}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read config file %s: %s", path, e)
        return {}


def build_subprocess_env(cfg: Mapping[str, object] | None = None) -> dict[str, str]:
    """Build the environment dict for the language server subprocess.

    Starts with the current process environment, then applies proxy
    settings from the config file.  Environment variables already set
    in the current process take precedence over config file values —
    they are never overwritten.

    Config file keys are matched case-insensitively to the canonical
    environment variable names.  Recognized keys:

    - ``http_proxy`` / ``HTTP_PROXY``
    - ``https_proxy`` / ``HTTPS_PROXY``
    - ``no_proxy`` / ``NO_PROXY``

    Returns a new dict suitable for passing as ``env`` to subprocess
    creation.  The original ``os.environ`` is not modified.
    Windows keys are canonicalized to uppercase before config composition;
    POSIX keeps its case-sensitive environment names.
    """
    windows = sys.platform == "win32"
    env: dict[str, str] = {}
    for key, inherited_value in os.environ.items():
        name = key.upper() if windows else key
        if name in env and env[name] != inherited_value:
            raise ValueError("Environment contains conflicting case-insensitive keys")
        env[name] = inherited_value

    if cfg is None:
        cfg = load_config()

    # Build a case-insensitive lookup of config proxy values
    cfg_lower = {k.lower(): v for k, v in cfg.items() if isinstance(v, str)}

    for var in _PROXY_ENV_VARS:
        if windows:
            var = var.upper()
        # Skip if already set in the environment
        if var in env:
            logger.debug(
                "Proxy var %s already set in environment, skipping config", var
            )
            continue

        # Look up in config (case-insensitive)
        value = cfg_lower.get(var.lower())
        if value:
            env[var] = value
            logger.info("Set %s from config file", var)

    return env


# Default network configuration written on first run.
_DEFAULT_CONFIG: dict[str, str] = {
    "_doc": "Meadow Bridge configuration. See README.md for details.",
    "https_proxy": "",
    "http_proxy": "",
    "no_proxy": "localhost,127.0.0.1",
}
