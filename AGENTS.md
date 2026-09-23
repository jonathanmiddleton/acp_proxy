## User

The user's name is Jonathan.

## Coding Standards

**Read [CODING_STANDARDS.md](CODING_STANDARDS.md) before making any code changes.** It
contains the project's binding standards covering failure handling, error
surfacing, resilience policy, and testing philosophy.

It also owns the Meadow-derived strong typing, semantic design, and
test-retention rules. Required completion validation is
`python3 scripts/checkout_gate.py`; `--list` prints its inventory. The gate
synchronizes the locked development environment, runs Mypy/Pyrefly
change-relative validation against `refs/heads/main`, all tests, and Ruff, and
requires the checkout to remain unchanged. Focused checks do not replace it.
During development, run `python3 scripts/typecheck_change.py` after bounded
Python edits. Do not add diagnostic baselines or weaken checks to obtain a pass.

## Project Overview

This repo owns two explicit inbound contracts over GitHub Copilot's
`copilot-language-server` ACP interface: Meadow's authenticated direct protocol
and a deprecated OpenAI-compatible adapter for stock OpenCode.

```
Meadow ───────────────→ ACP Proxy `/meadow/v1` ─→ copilot-language-server
OpenCode (deprecated) → ACP Proxy `/v1` ─────────→ copilot-language-server
```

## ACP Specification Reference

The Agent Client Protocol specification is at **https://agentclientprotocol.com**.
The full documentation index (suitable for LLM consumption) is at
**https://agentclientprotocol.com/llms.txt**.

Key spec pages relevant to this proxy:

| Topic | URL | Notes |
|-------|-----|-------|
| Protocol overview | https://agentclientprotocol.com/protocol/overview.md | Core architecture and concepts |
| Session setup | https://agentclientprotocol.com/protocol/session-setup.md | `session/new` and `session/load` — load replays conversation history |
| Session list | https://agentclientprotocol.com/protocol/session-list.md | `session/list` — discover existing sessions (stabilized) |
| Prompt turn | https://agentclientprotocol.com/protocol/prompt-turn.md | `session/prompt` and `session/update` streaming |
| Session modes | https://agentclientprotocol.com/protocol/session-modes.md | Agent operating modes (Ask, Agent, Plan, etc.) |
| Session config | https://agentclientprotocol.com/protocol/session-config-options.md | `session/set_config_option` (stabilized) |
| Initialization | https://agentclientprotocol.com/protocol/initialization.md | Capability negotiation |
| Terminals | https://agentclientprotocol.com/protocol/terminals.md | Terminal callback handling |
| File system | https://agentclientprotocol.com/protocol/file-system.md | File read/write callbacks |
| Schema | https://agentclientprotocol.com/protocol/schema.md | Full type definitions |

RFDs (Requests for Dialog — proposed but not yet stabilized):

| RFD | URL | Status |
|-----|-----|--------|
| Session close | https://agentclientprotocol.com/rfds/session-close.md | Proposed — would allow explicit session cleanup |
| Session resume | https://agentclientprotocol.com/rfds/session-resume.md | Proposed — like load but without history replay |
| Session delete | https://agentclientprotocol.com/rfds/session-delete.md | Proposed |
| Request cancellation | https://agentclientprotocol.com/rfds/request-cancellation.md | Proposed — `$/cancel_request` for any JSON-RPC request |
| Session usage | https://agentclientprotocol.com/rfds/session-usage.md | Proposed — token/context/cost tracking |
| Proxy chains | https://agentclientprotocol.com/rfds/proxy-chains.md | Proposed — agent extensions via proxies |
| Custom LLM endpoint | https://agentclientprotocol.com/rfds/custom-llm-endpoint.md | Proposed — configurable LLM providers |

The OpenAPI schema is at https://agentclientprotocol.com/api-reference/openapi.json.

## Module Architecture

| Module         | Owns                                                                                                                                                           | Does NOT own                                |
|----------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------|
| `transport.py` | Owned child lifecycle, NDJSON framing, bounded JSON-RPC correlation/callback tasks, ordered terminal signaling, and unexpected-close reporting | HTTP protocol and settlement policy          |
| `client.py`    | ACP initialization, exact model acknowledgement, session/prompt primitives, mode-selected callback policy                                               | HTTP serving or direct operation identity   |
| `direct_protocol.py`, `direct_state.py` | Strict Meadow wire shapes, generation-long operation ledger, and state vocabulary                                               | ACP method execution                         |
| `direct_service.py`, `direct_server.py` | Authenticated direct orchestration, explicit identities, prompt lifetime, settlement, evidence, and resource limits                    | Legacy replay or prompt hashing              |
| `server.py`    | Isolated deprecated OpenAI-compatible endpoints, replay heuristics, and SSE translation                                                                        | Meadow direct traffic                        |
| `discovery.py` | Binary resolution for the supported IntelliJ IDEA/PyCharm 2025.3 and 2026.1 plugin paths                                                                        | Protocol, sessions, serving                  |
| `__main__.py`  | Mandatory mode selection, bind/auth policy, owned lifecycle, and HTTP wiring                                                                                    | Binary discovery logic                       |

## Tests

- Avoid mocks as much as possible
- Test actual implementations, do not duplicate logic into tests
- Favor writing property based tests
- **Unit/property tests** (`test_transport.py`, `test_raw_events.py`, `test_client.py`, `test_model_binding_order.py`, `test_server.py`, `test_direct_*`, `test_discovery.py`): in-process boundaries, no real subprocess.
- **Integration tests** (`test_integration.py`): Real copilot-language-server. **Fails** (not skips) if binary not found — a missing binary means the environment is misconfigured.
- **No skips.** Tests must never use `skipif` or `pytest.skip()`. See CODING_STANDARDS.md.
- Run all: `python -m pytest tests/ -v`
- Run without live Copilot integration: `python -m pytest tests/ --ignore=tests/test_integration.py -v`
- Validation-driver tests exercise real temporary Git repositories and tool
  subprocess boundaries; they do not launch Copilot. The complete gate includes
  them and the live integration suite.

## Architectural Decisions

**Read the relevant ADRs before making any architectural or design change.**
They document the binding decisions and the empirical evidence behind them —
particularly the failure modes that motivated each decision.

| ADR                                                 | Decision                                                                                                      |
|-----------------------------------------------------|---------------------------------------------------------------------------------------------------------------|
| [ADR-001](adrs/001-acp-proxy-architecture.md)       | Route OpenCode through ACP proxy (why this architecture, what was rejected)                                   |
| [ADR-002](adrs/002-session-per-conversation.md)     | Session-per-conversation via first-message hash (why sessions are keyed this way)                             |
| [ADR-003](adrs/003-system-prompt-injection.md)      | System prompt injection as primary control surface (why and how it works)                                     |
| [ADR-004](adrs/004-last-user-message-extraction.md) | Extract only the last user message (why full history replay causes duplication)                               |
| [ADR-005](adrs/005-fail-loud-testing.md)            | Fail-loud testing — no skips (why skips are banned, what they masked)                                         |
| [ADR-006](adrs/006-binary-discovery.md)             | Version-bounded JetBrains binary discovery and wrong-binary failure evidence                     |
| [ADR-007](adrs/007-tool-ownership.md)               | The ACP server owns tools — do not inject or override (protocol constraint, empirical evidence)               |
| [ADR-008](adrs/008-proxy-as-substrate.md)           | Proxy as substrate — installable command, cwd as workspace                                                    |
| [ADR-009](adrs/009-intra-process-session-scaling.md)| Retained scaling evidence; direct pool/affinity clauses superseded                                            |
| [ADR-011](adrs/011-context-injection-boundary.md)   | Deprecated legacy context-injection boundary                                                                 |
| [ADR-012](adrs/012-meadow-direct-consumer-protocol.md) | Authenticated direct protocol, migration, lifecycle, evidence, and authority policy                       |
| [ADR-014](adrs/014-correlate-direct-session-state.md) | Correlate and validate direct session state without retaining unsupported payloads                         |
| [ADR-015](adrs/015-order-direct-model-binding-transitions.md) | Bound prior/target model transitions until ordered RPC settlement; preserve exact acknowledgement and post-binding integrity |
| [ADR-016](adrs/016-opt-in-raw-acp-event-capture.md) | Explicit raw event diagnostics with ordered capture, bounded queues, and visible failures |
| [ADR-017](adrs/017-change-relative-typing-and-checkout-gate.md) | Meadow-derived typing, complete checkout validation, snapshot isolation, and no-skips enforcement |

The ADRs explain the *why* behind the module ownership rules in the table
above. A change that contradicts an accepted ADR requires a new ADR
superseding it, not a silent deviation.

## Configuration

**`opencode.json`** (repo root) configures deprecated `opencode-legacy` mode as
an OpenCode provider. It does not describe Meadow direct mode. It points OpenCode at the proxy as its
Copilot provider. It points OpenCode at `http://127.0.0.1:8765/v1` with no
auth, and declares the model IDs the proxy must handle: `gpt-4.1`, `gpt-4o`,
`claude-sonnet-4`, `gemini-2.5-pro`, `auto`. When adding model routing logic,
this file defines what model IDs are valid — they must match what the proxy
advertises on `GET /v1/models`.

## Diagnostic Scripts

Two standalone scripts in `src/` exist for protocol-level debugging — they
are not part of the proxy package and should not be imported by production
code:

- **`acp_probe.py`** — Keeps a language-server subprocess alive and sends a
  sequence of raw JSON-RPC messages. Use to explore the ACP wire protocol
  directly.
- **`acp_validate.py`** — Runs the full init → session → prompt lifecycle and
  prints structured results per step. Use to validate a binary is responsive
  before debugging the proxy layer.

## Git Conventions

- Commit messages describe the "why" not the "what".
- No user IDs or environment-specific paths in committed code.

## Target Environment Constraints

- The target is a restricted enterprise environment. The copilot-language-server
  binary bundled with the JetBrains Copilot plugin is the only sanctioned path
  to Copilot.
- Binary path varies by user. Auto-discovery via `ps` or supported JetBrains plugin
  directory search. Never hardcode user-specific paths.
- OpenCode is the stock prebuilt binary installed via npm. No source builds,
  no custom forks.
- Available models and modes vary between environments. The proxy must handle
  whatever the server advertises but must not silently degrade required
  capabilities.
