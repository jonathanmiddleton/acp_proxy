# Native IDE smoke test

A bounded diagnostic for the JetBrains-bundled Copilot language server's
native IDE interface. It uses Python's standard library and existing
repository helpers; no package installation, Copilot CLI, or MCP server is
required. Python 3.11 or newer and an existing authorized Copilot login are
prerequisites.

The test has one workspace, one Python file, one explicitly selected model,
and one language-server process. The model must create the file, edit its
contents, execute it through a native client-tool callback, and retain context
across two conversation turns. The runner checks actual file bytes, callback
activity, process exit/output, and the model's receipt of the execution result.
An answer claiming success without those effects fails the test.

File operations use registered `smoke_create_file` and `smoke_edit_file` client
tools, plus `run_in_terminal` for the exact Python command. The distinct file
tool names dispatch directly to client callbacks. The vendor's built-in edit
wrapper can run an additional model to rewrite edits; this diagnostic does
not require that transformation.

This is an isolated experiment. It does not change the production proxy or
Meadow's protocol, and it does not claim to implement a general shell or a
production adapter.

## Command interface

Entry point: `python experiments/native_ide_smoke/run.py`.

| Option | Behavior |
| --- | --- |
| `--list-models` | Records the installed server's advertised model catalog without executing the tool test. |
| `--model <id>` | Runs the two-turn test with an explicit advertised model ID. Automatic model routing is not qualified. |
| `--binary <path>` | Selects an installed language-server executable explicitly. |
| `--output-dir <path>` | Selects a new or empty directory for diagnostic artifacts. |

Without `--binary`, the repository's existing discovery selects an installed
language server. The selected path, actual version and SHA-256 are recorded.
The existing discovery version floor is only a preflight check; the native
methods and effects must pass on the actual deployed version. There is no
pinned 1.545.1 requirement, automatic model substitution, downloaded binary,
retry, or alternative transport fallback.

The runner reads existing proxy settings from `~/.acp_proxy/config.json`, if
present, and preserves existing proxy variables and certificate variables such
as `NODE_EXTRA_CA_CERTS`. It removes `NODE_OPTIONS` during environment isolation.
It does not create or modify the config. Authentication uses the existing repository
OAuth bridge: explicit Copilot token environment variables, or the prior IDE
`oauth.json` login. Missing or ambiguous credentials fail clearly. It does not
extract credentials from a keychain or `auth.db`, or initiate a new login.

## Result contract

The runner prints the evidence directory. A model listing is **catalog only**;
it is not a successful implementation test. For the smoke test, exit code 0
means every required acceptance and cleanup check passed. Any missing
capability, refusal, protocol failure, timeout, cancellation, or incomplete
cleanup produces a nonzero exit and a failure result.

`result.json` records the outcome and the failed stage when the test cannot
finish. `wire.jsonl` retains ordered protocol evidence. Output directories
default to this experiment's ignored
`results/` directory; `--output-dir` can select a new directory elsewhere.
Credential-bearing runtime state lives separately in a temporary directory
and is cleaned after the owned child exits. It is not included in the result
artifacts. Captured evidence redacts known credential values.

The language server is launched directly with `--stdio` and bundled mode
forced. The native MCP server configuration is explicitly empty. The runner
requires `mcp/getTools` to return an empty server list before and after the
diagnostic, and rejects nonempty or malformed `copilot/mcpTools` notifications.
The query reads the server's catalog without starting an MCP server. A disabled
MCP manager can remain silent, so a change notification is not required.
`controls.mcp_catalog_snapshots` records both responses; the separate
`empty_mcp_catalog_notification_observed` flag records notification receipt.
CLI guards record and reject attempted
PATH launches. These are observable test controls, not an OS security sandbox.
The callback permits only the declared scratch file and exact small Python
program, launched by argv without a shell; it has no arbitrary command access.
The runner accepts matching confirmation requests after checking that
allowlist. It does not implement interactive or remembered user approvals,
and the result does not attest to centrally managed tool authorization.

A pass establishes this bounded task on the recorded installation.
It does not qualify automatic compression, restart/crash recovery, arbitrary
commands, parallel sessions, or the full Meadow integration.

## Local checks

The portable offline checks use no Copilot service:

```text
python -m unittest discover -s experiments/native_ide_smoke -p "test_*.py" -v
```

The authenticated `--model` invocation is the end-to-end acceptance check.
Each result applies to its recorded installation and environment.
