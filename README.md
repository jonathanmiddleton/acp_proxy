# ACP Proxy

Bridges [OpenCode](https://opencode.ai) to GitHub Copilot by exposing the
`copilot-language-server` ACP interface as an OpenAI-compatible HTTP endpoint.
This enables orchestrating Copilot-hosted models (GPT-4.1, GPT-4o, Claude
Sonnet 4, Gemini 2.5 Pro) through OpenCode's agent framework — useful when
Copilot is the available model provider and you want orchestration
beyond IDE-integrated chat.

```
OpenCode  →  ACP Proxy (localhost:8765)  →  copilot-language-server  →  Copilot backend
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
| **Node.js / npm**                                         | System package manager                                                | Required to install OpenCode                                                    |
| **[OpenCode](https://opencode.ai)**                       | `npm i -g opencode-ai@latest`                                         | Agent framework that connects to the proxy as an OpenAI-compatible provider     |
| **JetBrains IDE with GitHub Copilot plugin** (>= 1.442.0) | JetBrains Toolbox or standalone installer; plugin via IDE marketplace | Provides the `copilot-language-server` binary and cached Copilot authentication |
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

### 1. Start the proxy

From your project directory:

```bash
cd ~/projects/my-app
acp-proxy
```

The current working directory becomes the ACP workspace — the copilot-language-server scans it and scopes file operations to it.

The proxy auto-discovers the `copilot-language-server` binary from running processes or JetBrains plugin directories. To specify the path explicitly:

```bash
acp-proxy --binary /path/to/copilot-language-server
```

`python -m acp_proxy` also works as an alternative invocation.

### 2. Configure OpenCode

Copy the provided `opencode.json` to your project root, or to `~/.config/opencode/opencode.json` for global use. It configures a `copilot` provider pointing at the proxy.

### 3. Start OpenCode

```bash
opencode
```

Available models will appear as `copilot/gpt-4.1`, `copilot/gpt-4o`, `copilot/auto`.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Integration tests require the `copilot-language-server` binary to be available. They **fail** (not skip) if the binary is not found — see [ADR-005](adrs/005-fail-loud-testing.md). Run unit tests only with: `python -m pytest tests/test_transport.py tests/test_server.py tests/test_discovery.py -v`

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

### Context injection

The proxy automatically injects workspace markdown files into the system
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
| `--binary`        | auto-discovered   | Path to `copilot-language-server`                                              |
| `--host`          | 127.0.0.1         | Address on which the HTTP server listens                                       |
| `--port`          | 8765              | Port for the HTTP server                                                       |
| `--cwd`           | current directory | Working directory for ACP sessions (default: `cwd` where acp_proxy is executed |
| `--log-level`     | DEBUG             | DEBUG, INFO, WARNING, ERROR (DEBUG default during development phase)            |
| `--log-file`      | logs/proxy.log    | Log file path (always DEBUG level)                                             |
| `--system-prompt` | none              | Path to a file containing a system prompt to inject into each new session      |

The default bind is loopback-only. Passing `--host 0.0.0.0` exposes the
unauthenticated proxy on every IPv4 interface visible to the process; use it
only inside an isolated container or on a trusted network. Meadow's Podman mode
binds this address inside the container while publishing the host port only on
`127.0.0.1`.
