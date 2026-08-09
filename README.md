# ACP Proxy

Exposes GitHub Copilot's `copilot-language-server` ACP interface through two
explicitly selected HTTP contracts:

- an authenticated, stateful `/meadow/v1` protocol for direct Meadow use;
- a deprecated OpenAI-compatible `/v1` adapter for stock OpenCode.

```
Meadow ───────────────→ ACP Proxy `/meadow/v1` ─→ copilot-language-server
OpenCode (deprecated) → ACP Proxy `/v1` ─────────→ copilot-language-server
```

## Dependencies

### Runtime dependencies (Python)

Installed automatically via `pip install`:

- **FastAPI** — HTTP server exposing OpenAI-compatible endpoints
- **Uvicorn** — ASGI server
- **Pydantic** — Request/response validation

### External dependencies

These must be present in the environment before using the proxy.

| Dependency                                                | Suggested install                                                     | Purpose                                                                         |
|-----------------------------------------------------------|-----------------------------------------------------------------------|---------------------------------------------------------------------------------|
| **Python 3.11+**                                          | System package manager                                                | Runtime for the proxy itself                                                    |
| **Node.js / npm**                                         | System package manager                                                | Required only for deprecated OpenCode compatibility                             |
| **[OpenCode](https://opencode.ai)**                       | `npm i -g opencode-ai@latest`                                         | Optional legacy consumer                                                        |
| **JetBrains IDE with GitHub Copilot plugin** (`copilot-language-server` >= 1.523.3) | JetBrains Toolbox or standalone installer; plugin via IDE marketplace | Provides the version-admitted ACP binary and cached Copilot authentication |
| **GitHub Copilot subscription**                           | Signed in via the JetBrains plugin                                    | The proxy uses the cached OAuth token at `~/.config/github-copilot/`            |

Alternative installation paths exist for OpenCode (building from source, other
package managers) and for the Copilot plugin (VS Code, Neovim). The versions
above are tested and known to work together.

## Install

```bash
git clone https://github.com/jonathanmiddleton/acp_proxy.git
cd acp_proxy
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

### Meadow direct mode

The managed Meadow launcher generates the launch secret and passes it to both
processes without logging it. For an external trusted-host deployment, provide
an equivalent pre-shared value through a secret manager, then bind loopback:

```bash
cd ~/projects/my-app
export ACP_PROXY_MEADOW_SECRET='<at-least-32-secret-bytes>'
acp-proxy \
  --consumer-mode meadow-direct \
  --execution-authority trusted-host
```

Before direct HTTP readiness, the proxy uses its one non-prompted catalog
session to require a complete `session/set_config_option` response whose model
`currentValue` exactly matches the catalog default. A language server that
cannot prove that capability fails startup. Direct readiness is then negotiated
at authenticated `GET /meadow/v1/capabilities`.
The response identifies the protocol major, continuity generation, canonical
workspace, exact model catalog, execution authority, resource limits, evidence
support, and underlying ACP capabilities. Every mutation pins that generation
and uses explicit logical-session, invocation, and operation IDs.
Only `/meadow/v1/*` performs direct work. `/v1/*` exists in this mode solely as
an unauthenticated `410 legacy_mode_required` migration response for a
misconfigured stock OpenCode process; it cannot reach ACP.

`confined-container` may bind `0.0.0.0` only inside an actual container runtime
with both the runtime marker and the managed launch attestation
`ACP_PROXY_CONTAINER_BOUNDARY=1`. This profile is intended for a private
container namespace. Meadow's managed launcher publishes its host port only on
loopback. The proxy can observe the runtime marker but cannot inspect external
port publishing, so a standalone operator must provide an equally private
transport (or authenticated TLS) and truthfully supply the launcher
attestation. Merely setting the environment variable on a host is rejected.

Direct mode does not accept `--system-prompt` or context-file injection. Meadow
owns stable instructions, the current prompt (including legal routes), the prose
output contract, and correction deltas. The proxy reports first-turn injection;
it does not claim a provider-native system or developer role.
Later results report that stable instructions were not resubmitted on the same
ACP session; they do not claim behavioral recall. Event bytes, event count,
response bytes, request bytes, sessions, operations, queued prompts, and
deadlines are all negotiated and fail closed at their respective boundaries.
Agent-defined `usage_update` and `session_info_update` payloads remain bounded,
ordered raw diagnostics in direct v1. The proxy advertises usage reporting as
unsupported and never reinterprets booleans, negative values, or unknown fields
as normalized token counters.

### Deprecated OpenCode compatibility

Stock OpenCode remains available only through an explicit legacy mode during
the 0.2.x release line:

```bash
cd ~/projects/my-app
acp-proxy --consumer-mode opencode-legacy
```

Configure OpenCode with the provided `opencode.json`, then start `opencode`.
Legacy mode is removed in 0.3.0. Direct and legacy endpoints reject each
other's traffic rather than guessing caller semantics.

The current working directory (or `--cwd`) becomes the ACP workspace.

The proxy combines candidates from running processes and supported JetBrains
plugin directories, rejects versions below 1.523.3, and deterministically
selects the highest admitted version (using canonical path as the stable
tie-break). This minimum is global: deprecated legacy mode does not admit an
older binary. To specify the path explicitly:

```bash
acp-proxy --consumer-mode opencode-legacy --binary /path/to/copilot-language-server
```

`--binary` bypasses candidate discovery only. The selected executable must
still report a strict `MAJOR.MINOR.PATCH` version at or above 1.523.3.

`python -m acp_proxy` supports the same mandatory options.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Integration tests require the `copilot-language-server` binary to be available.
They **fail** (not skip) if the binary is not found — see
[ADR-005](adrs/005-fail-loud-testing.md). Run unit tests only with:

```bash
python -m pytest tests/test_transport.py tests/test_client.py tests/test_server.py tests/test_direct_*.py tests/test_discovery.py tests/test_binary_admission.py tests/test_main.py -v
```
The live direct integration probe requires the advertised
`gpt-5.3-codex` model and exercises exact model acknowledgement plus two turns
on one continuity generation.

## Configuration

On first run, the proxy creates a default config at `~/.acp_proxy/config.json`:

```json
{
  "_doc": "ACP Proxy configuration. See README.md for details.",
  "https_proxy": "",
  "http_proxy": "",
  "no_proxy": "localhost,127.0.0.1",
  "context_files": ["AGENTS.md", "CLAUDE.md", "COPILOT-INSTRUCTIONS.md"]
}
```

### Proxy settings

In corporate environments, the `copilot-language-server` needs proxy
settings to reach `api.github.com`. Edit `https_proxy` and `http_proxy`
with your corporate proxy URL (e.g., `"http://proxy-host:port"`).

The proxy injects these into the language server subprocess environment
only — the global environment is not modified. Shell environment variables
(`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`) take precedence over config file
values if both are set.

### Legacy context injection

Only `opencode-legacy` mode injects workspace markdown files into the system
prompt for each ACP session. The `context_files` list controls which files
are scanned in the workspace (`--cwd`). Files that don't exist are silently
skipped — a generous default list works across different repos.

To customize, edit `context_files` in the config:

```json
{
  "context_files": ["AGENTS.md", "CODING_STANDARDS.md", "docs/ARCHITECTURE.md"]
}
```

To disable auto-injection entirely: `"context_files": []`

If `--system-prompt` is also provided, the explicit prompt comes first
(positional priority) and context files are appended after a separator.

The proxy logs estimated token counts for the composed prompt at startup
and per request. These are estimates (~4 chars/token) — actual usage is
higher because Copilot's backend injects its own system prompt, safety
policies, and tool definitions that we cannot observe.

## ACP Specification

The [Agent Client Protocol](https://agentclientprotocol.com) standardizes
communication between code editors and coding agents. The full documentation
index is at https://agentclientprotocol.com/llms.txt.

Key references: [session setup](https://agentclientprotocol.com/protocol/session-setup.md)
(`session/new`, `session/load`),
[prompt turn](https://agentclientprotocol.com/protocol/prompt-turn.md),
[schema](https://agentclientprotocol.com/protocol/schema.md).

## Options

| Flag              | Default           | Description                                                                    |
|-------------------|-------------------|--------------------------------------------------------------------------------|
| `--consumer-mode` | required          | `meadow-direct` or deprecated `opencode-legacy`; never inferred                |
| `--binary`        | auto-discovered   | Path to `copilot-language-server`                                              |
| `--host`          | 127.0.0.1         | Address on which the HTTP server listens                                       |
| `--port`          | 8765              | Port for the HTTP server                                                       |
| `--cwd`           | current directory | Working directory for ACP sessions (default: `cwd` where acp_proxy is executed |
| `--log-level`     | DEBUG             | DEBUG, INFO, WARNING, ERROR (DEBUG default during development phase)            |
| `--log-file`      | logs/proxy.log    | Log file path (always DEBUG level)                                             |
| `--execution-authority` | none        | Required direct profile: `trusted-host` or `confined-container`                |
| `--system-prompt` | none              | Legacy-only prompt file; rejected in direct mode                               |
| `--context-files` | configured list   | Legacy-only workspace context override; rejected in direct mode                |

The default bind is loopback. Trusted-host direct mode rejects non-loopback
binds. Legacy mode remains unauthenticated and must not be exposed to an
untrusted network. Confined direct mode requires both managed attestation and
an observable runtime container boundary.
