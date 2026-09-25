## User

The user's name is Jonathan.

## Coding Standards

**Read [CODING_STANDARDS.md](CODING_STANDARDS.md) before making any code changes.** It
contains the project's binding standards covering failure handling, error
surfacing, resilience policy, and testing philosophy.

Required completion validation is
`python3 scripts/checkout_gate.py`; `--list` prints its inventory. The gate
synchronizes the locked development environment, runs Mypy/Pyrefly
change-relative validation against `refs/heads/master`, all tests, and Ruff, and
requires the checkout to remain unchanged. Focused checks do not replace it.
During development, run `python3 scripts/typecheck_change.py` after bounded
Python edits. Do not add diagnostic baselines or weaken checks to obtain a pass.

## Project Overview

This repo connects Meadow to GitHub Copilot's native IDE interface through
an authenticated `/meadow/v2` contract. Read [ADR-020](adrs/020-native-ide-backend.md)
for current lifecycle, tool, permission and evidence ownership. Historical ACP
ADRs preserve experiments; they do not define the current transport.

```
Meadow → Meadow Bridge `/meadow/v2` → copilot-language-server --stdio
```

## Module Architecture

| Module         | Owns                                                                                                                                                           | Does NOT own                                |
|----------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------|
| `native_transport.py` | Owned child lifecycle, LSP framing, bounded JSON-RPC correlation/callback tasks, ordered terminal signaling, and unexpected-close reporting | HTTP protocol and settlement policy          |
| `native_client.py` | Native initialization, catalog, conversation/turn correlation and registered tool callbacks                                               | HTTP serving or direct operation identity   |
| `direct_protocol.py`, `direct_state.py` | Strict Meadow wire shapes, generation-long operation ledger, and state vocabulary                                               | Native method execution                         |
| `direct_service.py`, `direct_server.py` | Authenticated direct orchestration, explicit identities, prompt lifetime, settlement, evidence, and resource limits                    | Legacy replay or prompt hashing              |
| `workspace_tools.py`, `owned_commands.py` | File preconditions, bounded command effects and owned process settlement | Model invocation or HTTP identity |
| `permission_policy.py`, `permission_handler.py` | Explicit noninteractive session policy and callback decisions | Role parsing or interactive prompts |
| `discovery.py` | Recursive JetBrains binary discovery and version-reported admission                                                                        | Protocol, sessions, serving                  |
| `__main__.py`  | Direct bind/auth policy, owned lifecycle, and HTTP wiring                                                                                    | Binary discovery logic                       |

## Tests

- Avoid mocks as much as possible
- Test actual implementations, do not duplicate logic into tests
- Favor writing property based tests
- **Unit/property tests** (`test_native_*`, `test_workspace_tools.py`, `test_owned_commands.py`, `test_raw_events.py`, `test_direct_*`, `test_discovery.py`): bounded protocol fixtures and real local file/process effects; no live model calls.
- **Integration tests** (`test_integration.py`): Real copilot-language-server. **Fails** (not skips) if binary not found — a missing binary means the environment is misconfigured.
- **No skips.** Tests must never use `skipif` or `pytest.skip()`. See CODING_STANDARDS.md.
- Run all: `python -m pytest tests/ -v`
- Run without live Copilot integration: `python -m pytest tests/ --ignore=tests/test_integration.py -v`
- Validation-driver tests exercise real temporary Git repositories and tool
  subprocess boundaries; they do not launch Copilot. The complete gate includes
  them and the live integration suite.

## Architectural Decisions

**Read the relevant ADRs before making any architectural or design change.**
They document current binding decisions and their empirical basis. Historical
rows retain evidence only; the listed superseding ADR governs current behavior.

| ADR                                                 | Decision                                                                                                      |
|-----------------------------------------------------|---------------------------------------------------------------------------------------------------------------|
| [ADR-001](adrs/001-acp-proxy-architecture.md)       | Historical adapter architecture; removed by ADR-018                                   |
| [ADR-002](adrs/002-session-per-conversation.md)     | Historical first-message-hash session identity; removed by ADR-018                             |
| [ADR-003](adrs/003-system-prompt-injection.md)      | Historical proxy-authored system prompt injection; removed by ADR-018                                     |
| [ADR-004](adrs/004-last-user-message-extraction.md) | Historical last-user-message extraction; removed by ADR-018                               |
| [ADR-005](adrs/005-fail-loud-testing.md)            | Fail-loud testing — no skips (why skips are banned, what they masked)                                         |
| [ADR-006](adrs/006-binary-discovery.md)             | Version-bounded JetBrains binary discovery and wrong-binary failure evidence                     |
| [ADR-007](adrs/007-tool-ownership.md)               | Historical ACP tool ownership; superseded by ADR-020               |
| [ADR-008](adrs/008-proxy-as-substrate.md)           | Installable command and cwd workspace retained; ADR-018 removes OpenCode startup, ADR-019 owns Meadow Bridge identity                                                    |
| [ADR-009](adrs/009-intra-process-session-scaling.md)| Retained scaling evidence; direct pool/affinity clauses superseded                                            |
| [ADR-011](adrs/011-context-injection-boundary.md)   | Historical context-injection boundary; removed by ADR-018                                                                 |
| [ADR-012](adrs/012-meadow-direct-consumer-protocol.md) | Direct lifecycle retained; ACP wire and binding superseded by ADR-020                       |
| [ADR-014](adrs/014-correlate-direct-session-state.md) | Historical ACP state correlation; native mechanics governed by ADR-020                         |
| [ADR-015](adrs/015-order-direct-model-binding-transitions.md) | Historical ACP model transitions; native mechanics governed by ADR-020 |
| [ADR-016](adrs/016-opt-in-raw-acp-event-capture.md) | Explicit raw event diagnostics with ordered capture, bounded queues, and visible failures |
| [ADR-017](adrs/017-change-relative-typing-and-checkout-gate.md) | Meadow-derived typing, complete checkout validation, snapshot isolation, and no-skips enforcement |
| [ADR-018](adrs/018-remove-openai-compatible-adapter.md) | Remove the deprecated adapter and consumer-mode selector; native backend governed by ADR-020 |
| [ADR-019](adrs/019-meadow-bridge-product-identity.md) | Meadow Bridge identity; retained ACP wire clauses superseded by ADR-020 |
| [ADR-020](adrs/020-native-ide-backend.md) | Current native IDE transport, direct v2, workspace callbacks and explicit policy |
| [ADR-021](adrs/021-byte-bounded-diagnostic-results.md) | Byte-bounded diagnostic evidence and actual encoded HTTP-result delivery |

The ADRs explain the *why* behind the module ownership rules in the table
above. A change that contradicts an accepted ADR requires a new ADR
superseding it, not a silent deviation.

## Configuration

User configuration contains network proxy settings only. Meadow owns prompt
layers; the service has no OpenAI-compatible adapter, context-file injection,
or consumer-mode selector. See ADR-018 for the removal decision.

## Git Conventions

- Commit messages describe the "why" not the "what".
- No user IDs or environment-specific paths in committed code.

## Runtime Constraints

- Use the installed JetBrains Copilot plugin language-server executable.
- Discover per-user binary paths; never hardcode user-specific locations.
- Available models and modes vary. Handle the advertised server capabilities
  without silently degrading a required capability.
