# ACP Proxy

Connects Meadow to the installed GitHub Copilot `copilot-language-server`
through its ACP interface and the authenticated `/meadow/v1` HTTP contract.

```
Meadow → ACP Proxy `/meadow/v1` → copilot-language-server (ACP)
```

Copilot owns model access, authentication refresh, and agent-internal tools.
The service owns its child process, explicit sessions and operations, bounded
admission, and settlement evidence. It does not implement the native IDE backend.

## Dependencies

### Runtime dependencies (Python)

Installed automatically via `pip install`:

- **FastAPI** — HTTP server exposing the authenticated direct protocol
- **Uvicorn** — ASGI server
- **Pydantic** — Request/response validation

### External dependencies

These must be present in the environment before using the proxy.

| Dependency                                                | Suggested install                                                     | Purpose                                                                         |
|-----------------------------------------------------------|-----------------------------------------------------------------------|---------------------------------------------------------------------------------|
| **Python 3.11+**                                          | System package manager                                                | Runtime for the proxy itself                                                    |
| **JetBrains IDE with GitHub Copilot plugin** (`copilot-language-server` meeting the configured minimum) | JetBrains Toolbox or standalone installer; plugin via IDE marketplace | Provides the version-admitted ACP binary and cached Copilot authentication |
| **GitHub Copilot subscription**                           | Signed in via the JetBrains plugin                                    | The proxy uses the cached OAuth token at `~/.config/github-copilot/`            |

## Install

```bash
git clone https://github.com/jonathanmiddleton/acp_proxy.git
cd acp_proxy
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

### Authenticated direct service

The managed Meadow launcher generates the launch secret and passes it to both
processes without logging it. For an external trusted-host deployment, provide
an equivalent pre-shared value through a secret manager, then bind loopback:

```bash
cd ~/projects/my-app
export ACP_PROXY_MEADOW_SECRET='<at-least-32-secret-bytes>'
acp-proxy \
  --execution-authority trusted-host
```

Before direct HTTP readiness, the proxy uses one non-prompted catalog session
to discover the available models, require an advertised usable default, and
freeze one model-binding strategy for the child generation. The verified ACP
`session/set_config_option` path is preferred; only method-not-found selects
the Copilot `session/set_model` compatibility path. Every Meadow session then
applies that frozen strategy before becoming ready. Direct readiness is
negotiated at authenticated `GET /meadow/v1/capabilities`.
The response identifies the protocol major, continuity generation, canonical
workspace, exact model catalog, execution authority, resource limits, evidence
support, and underlying ACP capabilities. Every mutation pins that generation
and uses explicit logical-session, invocation, and operation IDs.
Only `/meadow/v1/*` performs consumer work. The OpenAI-compatible adapter and
its `/v1/*` routes have been removed.

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
Prompt-scoped `usage_update` and `session_info_update` payloads remain bounded,
ordered raw diagnostics in direct v1. The proxy advertises usage reporting as
unsupported and never reinterprets booleans, negative values, or unknown fields
as normalized token counters. Recognized state updates outside a prompt are
boundedly correlated, structurally validated and logged, then discarded unless
they enforce the acknowledged model.

The current working directory (or `--cwd`) becomes the ACP workspace.

The proxy combines named candidates from running processes with a recursive
search below the platform JetBrains data directory, rejects reported language-
server versions below `MIN_COPILOT_LANGUAGE_SERVER_VERSION` in
`application_policy.py`, and deterministically selects the highest admitted
version (using canonical path as the stable tie-break). IDE product,
release, plugin layout, and bundled architecture are not compatibility
evidence. To specify the path explicitly:

```bash
acp-proxy --execution-authority trusted-host --binary /path/to/copilot-language-server
```

`--binary` bypasses candidate discovery only. The selected executable must
still report a strict `MAJOR.MINOR.PATCH` version at or above that configured
application minimum.

`python -m acp_proxy` supports the same mandatory options.

## Development and validation

Read [CODING_STANDARDS.md](CODING_STANDARDS.md) before editing. The required
completion command, ported from Meadow, is:

```bash
python3 scripts/checkout_gate.py
```

Install `uv` first. The gate synchronizes the locked development dependencies,
runs strict Mypy and Pyrefly change-relative validation, the complete test suite,
and Ruff, then verifies that validation left the checkout unchanged. It retains
full command logs and reports every failed check. `--list` shows its inventory.

Typing compares the working tree (including staged, unstaged, and untracked
work) with the merge base of `HEAD` and the local `refs/heads/main`. Keep that
canonical ref available in your clone. New diagnostics and existing diagnostics
inside changed declarations fail; unchanged diagnostics elsewhere are not a
checked-in baseline or a claim of repository-wide strict cleanliness.

For focused development after environment setup:

```bash
uv sync --locked --extra dev
python3 scripts/typecheck_change.py
uv run --no-sync --no-env-file ruff check .
```

An explicit `typecheck_change.py --base <revision>` inspects another comparison
boundary; it does not replace the full gate. When changing dependencies, update
`uv.lock` with `uv lock` before running the gate.

### Tests

```bash
uv run --no-sync --no-env-file pytest tests/ -v
```

Integration tests require the `copilot-language-server` binary to be available.
They **fail** (not skip) if the binary is not found — see
[ADR-005](adrs/005-fail-loud-testing.md). Run unit tests only with:

```bash
uv run --no-sync --no-env-file pytest tests/ --ignore=tests/test_integration.py -v
```
The live direct integration probe requires the advertised
`gpt-5.3-codex` model, proves exact advertised-model binding through the public
Meadow contract, verifies that its catalog gate rejects an unadvertised
control, and exercises two turns on one continuity generation.
It also needs usable cached Copilot authentication. Missing prerequisites fail
the complete gate; a unit-only run is useful development feedback, not checkout
completion.

## Configuration

On first run, the proxy creates a default config at `~/.acp_proxy/config.json`:

```json
{
  "_doc": "ACP Proxy configuration. See README.md for details.",
  "https_proxy": "",
  "http_proxy": "",
  "no_proxy": "localhost,127.0.0.1"
}
```

### Proxy settings

When the language server needs an HTTP network proxy, configure
`https_proxy` and `http_proxy` with its URL (for example,
`"http://proxy-host:port"`).

The proxy injects these into the language server subprocess environment
only — the global environment is not modified. Shell environment variables
(`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`) take precedence over config file
values if both are set.

### GitHub environment forwarding

The proxy forwards every environment variable whose name begins with `GH` or
`GITHUB` unchanged to the `copilot-language-server` subprocess. This includes
`GH_COPILOT_TOKEN` and `GITHUB_COPILOT_TOKEN`; if both are present, both are
forwarded. Names are matched case-insensitively for stable Windows behavior,
and no aliases are synthesized.

For CLI startup, an explicit non-empty `GH_COPILOT_TOKEN` or
`GITHUB_COPILOT_TOKEN` remains authoritative. If neither is set, the proxy
loads the single prior OAuth `accessToken` from the GitHub Copilot
`oauth.json` file and supplies it to the child as `GITHUB_COPILOT_TOKEN`.
Windows uses `%LOCALAPPDATA%\github-copilot\oauth.json` (with the conventional
`%USERPROFILE%\AppData\Local` fallback). On macOS, it uses an absolute
`$XDG_CONFIG_HOME/github-copilot/oauth.json` when configured and otherwise
`$HOME/.config/github-copilot/oauth.json`. Automatic file discovery is limited
to these verified Windows and macOS layouts; other platforms must use one of
the explicit token variables. The file must contain exactly one OAuth account
so the proxy never guesses between identities. Authority and endpoint routing
remain owned by the surrounding `GH*`/`GITHUB*` environment and network
proxy settings; those values are forwarded unchanged. Missing, malformed, empty,
or ambiguous credentials stop startup before the language server is launched.
Credential values are never logged, and the language server remains
responsible for exchanging the durable OAuth credential for and refreshing its
short-lived Copilot service token.

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
| `--log-level`     | WARNING           | DEBUG, INFO, WARNING, ERROR; environment override supported            |
| `--log-file`      | logs/proxy.log    | Log file path (always DEBUG level)                                             |
| `--raw-event-file` | disabled        | Separate opt-in NDJSON capture of full ACP updates and prompt boundaries       |
| `--execution-authority` | none        | Required direct profile: `trusted-host` or `confined-container`                |

The default bind is loopback. Trusted-host direct mode rejects non-loopback
binds. Confined direct mode requires both managed attestation and
an observable runtime container boundary.

## Raw event diagnostics

Pass `--raw-event-file /absolute/path/events.jsonl` to retain complete decoded
`session/update` envelopes before client validation or projection, including
outer and nested `_meta`, message IDs, content, and unknown fields. The parent
directory must exist. Ordinary logs continue to contain protocol metadata.

Each NDJSON record has `version`, `capture_id`, `sequence`, `timestamp`, and
`kind`. `session_update` and `prompt_response` records carry the full JSON-RPC
`message`. `prompt_request` records capture dispatch intent with `request_id`
and `session_id`; `prompt_response` records carry the same identifiers and the
terminal result or error. Prompt bodies, HTTP credentials, and child stderr
are outside this capture. Agent output may itself contain sensitive content.

Capture is disabled by default. Existing complete files are appended with a
new `capture_id` and sequence starting at zero, preserving resumed-run evidence.
`capture_start` and `capture_end` delimit each clean writer lifetime. A missing
prompt response means settlement was not observed; a missing `capture_end`
means the capture is incomplete. An unterminated existing record fails startup.
Records are flushed without truncation or rotation. A bounded writer queue
keeps filesystem I/O off the event loop; overflow or I/O failure reports an
error, revokes transport continuity, and makes proxy shutdown fail.

Meadow's `acp_proxy.capture_raw_events: true` setting passes a run-owned file at
`<run-log-directory>/acp-events-<run_id>.jsonl`. Both the host proxy installation
and any selected container image must include this option.
