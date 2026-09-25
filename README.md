# Meadow Bridge

Connects Meadow to the installed GitHub Copilot `copilot-language-server`
through its native IDE interface and the authenticated `/meadow/v2` contract.

```
Meadow → Meadow Bridge `/meadow/v2` → copilot-language-server --stdio
```

Copilot owns model access, authentication refresh and model invocation. Bridge
owns logical sessions, registered workspace callbacks, explicit noninteractive
permission decisions, bounded operations and settlement evidence. It uses a
child process and LSP-framed JSON-RPC. Copilot CLI and MCP servers are not used.
See [ADR-020](adrs/020-native-ide-backend.md) for the contract and its limits.

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
| **Python 3.13.3 or later within Python 3.13**               | System package manager                                                | Runtime for the proxy itself                                                    |
| **JetBrains IDE with GitHub Copilot plugin** (`copilot-language-server` meeting the configured minimum) | JetBrains Toolbox or standalone installer; plugin via IDE marketplace | Provides the version-admitted native executable and cached Copilot authentication |
| **GitHub Copilot subscription**                           | Signed in via the JetBrains plugin                                    | The proxy uses the cached OAuth token at `~/.config/github-copilot/`            |

## Install

```bash
git clone https://github.com/jonathanmiddleton/meadow-bridge.git
cd meadow-bridge
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
export MEADOW_BRIDGE_MEADOW_SECRET='<at-least-32-secret-bytes>'
meadow-bridge \
  --execution-authority trusted-host
```

Before HTTP readiness, Bridge initializes the native server, admits its model
catalog and registers workspace tools. `GET /meadow/v2/capabilities` identifies
the protocol, generation, workspace, concrete agent-capable models, execution authority and limits.
Model-selection aliases such as `auto` are not admitted because they do not pin
a concrete model identity. Every mutation pins that generation and uses explicit logical-session,
invocation and operation IDs. There is no v1 or OpenAI-compatible endpoint.

Creating a session returns a stable logical handle immediately, with a null
backend conversation ID and `binding_state: allocated`. It performs no model
call. The first prompt creates the native conversation; later invocations use
that same conversation. Retirement destroys a bound conversation or releases
an unused allocation. Native conversation destruction does not delete provider
transcripts. Uncertain requests are reconciled by operation ID, never replayed
on a replacement session automatically.

Session creation requires an explicit versioned `allow_all` policy. Meadow
supplies it through its Bridge adapter. Policy decisions and confirmation
handling live separately from execution so later restrictive policies can be
added without changing transport ownership. This release never asks a person
for approval and does not interpret Meadow role permission lists. Copilot may
omit confirmation callbacks for generic tools; Bridge checks the session
policy independently before every actual invocation.

Registered tools create files, apply exact-context edits and execute commands.
Compatible file changes can be grouped in one call. Preconditions reject stale
content or paths outside the workspace; partial effects are reported. Command
execution uses the process user's authority and is not a workspace sandbox.
Windows defaults to Windows PowerShell 5.1 and POSIX to `/bin/sh`; `--shell`
selects a supported shell executable, including PowerShell 7. Duration, output
and owned process lifetime are bounded; truncation is explicit in the receipt.
Commands run in the foreground. Cancellation settles the owned POSIX process
group or Windows Job; deliberately detached background services are unsupported.

`confined-container` may bind `0.0.0.0` only inside an actual container runtime
with both the runtime marker and the managed launch attestation
`MEADOW_BRIDGE_CONTAINER_BOUNDARY=1`. This profile is intended for a private
container namespace. Meadow's managed launcher publishes its host port only on
loopback. The proxy can observe the runtime marker but cannot inspect external
port publishing, so a standalone operator must provide an equally private
transport (or authenticated TLS) and truthfully supply the launcher
attestation. Merely setting the environment variable on a host is rejected.

Meadow owns stable instructions, prompt layers, output contracts and correction
deltas. Bridge submits stable instructions once and distinguishes native
conversation binding from successful instruction submission. It does not claim
a provider-native system role or guaranteed behavioral recall.

Tool observations distinguish server and Bridge ownership. Permission decisions
carry the session policy digest. Effect receipts cover Bridge callbacks only;
they do not attest every native tool effect. Ordered native progress remains
available as diagnostic evidence. Normalized token usage, transparent recovery,
native output schemas and cross-session parallel prompts are not advertised.
Native built-in read/search tools remain available; catalog replacement and
role-derived policy translation are future work.

The current working directory (or `--cwd`) is the workspace. Request, response,
event, session, operation and queue limits are negotiated at admission. Native
terminal progress and matching RPC results must agree, and owned effects must
settle, before an operation is reported complete.

Ordered events have a 16 MiB serialized-payload budget per turn, without an
event-count ceiling; response text remains bounded at 2,000,000 UTF-8 bytes.
The separately advertised `max_http_response_bytes` defaults to 128 MiB and
bounds the exact encoded HTTP body, including repeated diagnostic receipts.
An oversized body returns `response_too_large` with the operation's actual
state and byte counts. The complete ledger result remains intact and the
operation is not replayed. Bridge 0.4.1 and its strict consumers must deploy
together. See [ADR-021](adrs/021-byte-bounded-diagnostic-results.md).

The proxy combines named candidates from running processes with a recursive
search below the platform JetBrains data directory, rejects reported language-
server versions below `MIN_COPILOT_LANGUAGE_SERVER_VERSION` in
`application_policy.py`, and deterministically selects the highest admitted
version (using canonical path as the stable tie-break). IDE product,
release, plugin layout, and bundled architecture are not compatibility
evidence. To specify the path explicitly:

```bash
meadow-bridge --execution-authority trusted-host --binary /path/to/copilot-language-server
```

`--binary` bypasses candidate discovery only. The selected executable must
still report a strict `MAJOR.MINOR.PATCH` version at or above that configured
application minimum.

`python -m meadow_bridge` supports the same mandatory options.

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
work) with the merge base of `HEAD` and the local `refs/heads/master`. Keep that
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
`gpt-5.6-sol` model. It checks advertised-model admission, rejection of an
unadvertised control, deferred native creation, real create/edit/command
callbacks, retained context across two turns and native retirement through the
public Meadow contract.
It also needs usable cached Copilot authentication. Missing prerequisites fail
the complete gate; a unit-only run is useful development feedback, not checkout
completion.

### Prior release Windows qualification

On 2026-09-24 UTC, revision
[`3613648`](https://github.com/jonathanmiddleton/meadow-bridge/commit/361364850040b1ba04ddcb2752a2b3f27c46e6a9)
completed the pre-native-backend `python -u scripts/checkout_gate.py` natively
on Windows with
**exit 0: all five checks passed**. The full suite passed **390 tests**, with
no skips or deselections, including real Copilot integration and native process
control. Locked dependency synchronization, change-relative Mypy/Pyrefly,
Ruff, and checkout preservation all passed. The canonical comparison base
remained unchanged, and the gate left the validated checkout clean.

## Configuration

On first run, the proxy creates a default config at `~/.meadow_bridge/config.json`:

```json
{
  "_doc": "Meadow Bridge configuration. See README.md for details.",
  "https_proxy": "",
  "http_proxy": "",
  "no_proxy": "localhost,127.0.0.1"
}
```

### Proxy settings

When the language server needs an HTTP network proxy, configure
`https_proxy` and `http_proxy` with its URL (for example,
`"http://proxy-host:port"`).

Bridge applies these settings to the language-server and command subprocess
environments; it does not modify the global environment. Discovered Copilot
OAuth credentials are injected only into the language-server environment. Shell environment variables
(`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`) take precedence over config file
values if both are set.

### GitHub environment forwarding

The proxy forwards every environment variable whose name begins with `GH` or
`GITHUB` to the `copilot-language-server` subprocess. This includes
`GH_COPILOT_TOKEN` and `GITHUB_COPILOT_TOKEN`; if both are present, both are
forwarded. Names are matched case-insensitively for stable Windows behavior,
and no aliases are synthesized. `GITHUB_COPILOT_ACP_USE_CLI` is the explicit
exception: Bridge forces it to `0` for the native child.

For CLI startup, an explicit non-empty `GH_COPILOT_TOKEN` or
`GITHUB_COPILOT_TOKEN` remains authoritative. If neither is set, the proxy
loads the single prior OAuth `accessToken` from the GitHub Copilot
`oauth.json` file and supplies it to the child as `GITHUB_COPILOT_TOKEN`.
Windows uses `%LOCALAPPDATA%\github-copilot\oauth.json` (with the conventional
`%USERPROFILE%\AppData\Local` fallback). On macOS, it uses an absolute
`$XDG_CONFIG_HOME/github-copilot/oauth.json` when configured and otherwise
`$HOME/.config/github-copilot/oauth.json`. Automatic home-directory discovery is
limited to these verified Windows and macOS layouts. Other POSIX platforms can
use an explicit absolute `XDG_CONFIG_HOME` for a projected credential file, or
one of the explicit token variables. The file must contain exactly one OAuth account
so the proxy never guesses between identities. Authority and endpoint routing
remain owned by the surrounding `GH*`/`GITHUB*` environment and network
proxy settings; those values are forwarded unchanged. Missing, malformed, empty,
or ambiguous credentials stop startup before the language server is launched.
Credential values are never logged, and the language server remains
responsible for exchanging the durable OAuth credential for and refreshing its
short-lived Copilot service token.

## Options

| Flag              | Default           | Description                                                                    |
|-------------------|-------------------|--------------------------------------------------------------------------------|
| `--binary`        | auto-discovered   | Path to `copilot-language-server`                                              |
| `--host`          | 127.0.0.1         | Address on which the HTTP server listens                                       |
| `--port`          | 8765              | Port for the HTTP server                                                       |
| `--cwd`           | current directory | Workspace root (default: command working directory) |
| `--log-level`     | WARNING           | DEBUG, INFO, WARNING, ERROR; environment override supported            |
| `--log-file`      | logs/meadow-bridge.log    | Log file path (always DEBUG level)                                             |
| `--raw-event-file` | disabled        | Separate opt-in NDJSON native protocol capture       |
| `--metadata-file` | disabled | Managed readiness metadata destination, removed on shutdown |
| `--shell` | platform default | Supported command shell executable; optional PowerShell 7 selection |
| `--execution-authority` | none        | Required direct profile: `trusted-host` or `confined-container`                |

The default bind is loopback. Trusted-host direct mode rejects non-loopback
binds. Confined direct mode requires both managed attestation and
an observable runtime container boundary.

## Raw event diagnostics

Pass `--raw-event-file /absolute/path/events.jsonl` to retain decoded native
JSON-RPC messages separately from ordinary payload-safe logs. The parent
directory must exist. Capture is disabled by default. Captured prompt, tool and
response content can contain sensitive workspace information; protect these
artifacts accordingly. HTTP credentials, child environment and stderr are not
part of this capture.

Each NDJSON record has `version`, `capture_id`, `sequence`, `timestamp`, and
`kind`. Native inbound and outbound records retain the JSON-RPC message.
`capture_start` and `capture_end` delimit clean writer lifetimes. Existing
complete files are appended with a fresh capture ID and sequence; an incomplete
last record fails startup. Missing terminal protocol evidence or a missing
`capture_end` means the artifact is incomplete. Records are flushed without
rotation. A bounded queue keeps disk I/O off the event loop; overflow or I/O
failure revokes continuity and makes shutdown fail.

Meadow's `meadow_bridge.capture_raw_events: true` setting supplies a run-owned
capture file. The host installation and selected container image must include
the same native backend version.
