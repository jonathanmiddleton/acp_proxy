# Candidate preparation evidence

The enterprise host has **not** been tested by the author. This record describes
local preparation on macOS on 22 September 2026. The manual target procedure is
in [README.md](README.md).

## Accepted local candidate

- Runner SHA-256: `f1ff64cd37881168e9cf8123e9d1d8b522cf63cfcf486041a202e07d89feb6b5`.
- Installed JetBrains language-server version: `1.545.1`.
- Binary SHA-256: `f2477b6d4c7952eba995b4826b0cee322c4a5c5fcf1442ce636cfd50c19dcf2b`.
- Explicit main model: `gpt-5.3-codex`; both turns reported the matching catalog name.
- Offline command: `python3 -m unittest discover -s experiments/native_ide_smoke -p 'test_*.py' -v` — **13 tests passed**, exit **0**.
- Authenticated command: `python3 experiments/native_ide_smoke/run.py --model gpt-5.3-codex --binary <installed-plugin-binary>` — **PASS**, exit **0**.
- Model listing: `python3 -S experiments/native_ide_smoke/run.py --list-models` — **catalog_only**, exit **0**; confirms the invoked path needs no installed site packages.

The accepted live run observed exactly these callbacks, in order:
`smoke_create_file`, `smoke_edit_file`, `run_in_terminal`. Initial/final file
bytes matched the allowlist. The real Python child returned exit 0, empty
stderr, and the expected nonce-bearing hello-world stdout. The model recalled
the marker sent only in the first turn and copied the independently generated
execution receipt from the command result. Conversation identity remained
stable and turn identity advanced.

The native MCP notification reported an empty server list; CLI guards were not
invoked. The server exited 0, the command was reaped, cleanup reported no errors,
and temporary credential state was removed. The retained local result directory
is `results/20260922T211431-f10e0c3f/`; runtime evidence is intentionally ignored
by Git and contains no credential cache.

## Development observations retained separately

Two earlier candidates failed visibly and were not accepted:

1. `20260922T210735-feb50328`: stopped at registration before model inference.
   The runner incorrectly expected builtin file tools to have a client catalog
   type; the vendor exposes shared wrappers over them.
2. `20260922T210955-60bd7072`: created the real file, then rejected an edit whose
   callback omitted the expected trailing newline. No command was executed.
   Source inspection showed that the builtin edit wrapper calls a separate
   CodeMapper model and forwards its generated source. The missing newline
   alone does not prove a universal trimming rule or which model changed it.

The final candidate registers distinct generic file-tool names that dispatch
straight to the client. It requests an exact newline-free final statement and
retains the literal source allowlist, actual-byte checks, exactly-once callback
sequence, command output checks, and independent execution receipt. It does not
relax those checks or retry failed model turns.

The native settings projection recognizes
`settings.github.copilot.enableBuiltInGitHubMcpServer = false`. Earlier
`chat.*` disable keys were not effective on that projection. The final runner
uses the recognized key and requires an observed empty MCP server catalog for
smoke acceptance.

## Scope

The subprocess and filesystem failure checks cover ordinary single-cancellation
cleanup, malformed/truncated protocol input, EOF with a pending operation,
terminal progress errors despite ordinary RPC results, credential redaction,
and rejected or out-of-order effects. No Windows execution or enterprise
network/policy qualification is claimed. General shell execution, automatic
compression, restoration, and production Meadow integration remain outside
this smoke test.
