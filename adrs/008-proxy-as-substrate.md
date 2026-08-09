# ADR-008: Proxy as Substrate — Installable Command, cwd as Workspace

**Status:** Accepted; startup contract amended by [ADR-012](012-meadow-direct-consumer-protocol.md)
**Date:** 2026-04-03  

## Context

The proxy has evolved from a proof-of-concept bridge into the foundational
service through which all Copilot model access flows in the constrained
environment. It is the substrate — the layer that must be running before
OpenCode, Meadow, or any other consumer can function.

The previous invocation pattern was `python -m acp_proxy`, which requires
the user to be in the repo directory (or manage `PYTHONPATH`). This is
appropriate for a development tool; it is not appropriate for infrastructure.

The proxy's `--cwd` flag (defaulting to `os.getcwd()`) already establishes
the ACP session's workspace context. The copilot-language-server uses `cwd`
from `session/new` to anchor its file system boundary — it scans the
directory at session start and knows file names within it.

## Decision

Register `acp-proxy` as a console script entry point via `[project.scripts]`
in `pyproject.toml`:

```toml
[project.scripts]
acp-proxy = "acp_proxy.__main__:main"
```

After `pip install`, `acp-proxy` is available on `PATH`. The user starts the
proxy from any project directory:

```bash
cd ~/projects/my-app
acp-proxy
```

The current working directory becomes the ACP workspace. The
copilot-language-server scans it, the model knows about files in it, and all
file operations are scoped to it. No `--cwd` flag needed for the common case.

## Rationale

- **Substrate should be invocable from anywhere.** Infrastructure services
  are installed once and run from whatever directory needs them. Requiring
  the user to navigate to the proxy's source directory breaks this model.
- **cwd as implicit workspace is natural.** The convention of "the directory
  you're in is the project you're working on" is universal in CLI tools
  (git, npm, cargo, etc.). The proxy follows the same convention.
- **Zero configuration for the common case.** `acp-proxy` with no flags
  does the right thing: auto-discovers the binary, listens on 8765, uses
  cwd as workspace, logs to `logs/proxy.log`. Flags exist for when defaults
  don't apply.
- **pip install is the deployment model.** On the target environment, the
  proxy is installed into a virtualenv and the `acp-proxy` command is
  available. No source checkout required at runtime.

## Consequences

- **The command name is `acp-proxy` (hyphenated).** This matches the package
  name (`acp-proxy`) and is consistent with Python packaging conventions.
  The Python module remains `acp_proxy` (underscored). `python -m acp_proxy`
  continues to work as an alternative invocation.
- **Log file path is relative to cwd by default.** `logs/proxy.log` is
  created relative to wherever the command is run. This means each project
  gets its own log directory. Use `--log-file` for a fixed location.
- **The proxy must be started before OpenCode.** This is the substrate
  contract. OpenCode's `opencode.json` points at `localhost:8765`. If the
  proxy isn't running, OpenCode's provider connection fails immediately
  (no silent degradation — consistent with the failure philosophy in
  CODING_STANDARDS.md).
