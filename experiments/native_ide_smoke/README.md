# Native IDE smoke test

A small, manual acceptance test for the JetBrains-bundled Copilot language
server's native IDE interface. Run it on the enterprise host before investing
in a Meadow integration. It uses Python's standard library and existing
repository helpers; no package installation, Copilot CLI, or MCP server is
required.

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

## Get the candidate

From your existing `acp_proxy` checkout, create a separate checkout:

```text
git fetch origin diagnostics/native-ide-smoke
git worktree add ../acp-native-ide-smoke FETCH_HEAD
cd ../acp-native-ide-smoke
```

Run the commands manually on the target host. Python 3.11 or newer and an
existing authorized JetBrains Copilot login are required. If your Python
command is `python3` or `py -3`, substitute it for `python` below.

## Run

First list the models advertised by the installed language server:

```text
python experiments/native_ide_smoke/run.py --list-models
```

Then choose a concrete advertised model ID and run the acceptance test
(automatic model routing is not qualified):

```text
python experiments/native_ide_smoke/run.py --model "<model-id-from-list>"
```

To select a particular deployed plugin executable, add this option to either
command:

```text
--binary "<full-path-to-copilot-language-server-or-copilot-language-server.exe>"
```

Without `--binary`, the repository's existing discovery selects an installed
language server. The selected path, actual version and SHA-256 are recorded.
The existing discovery version floor is only a preflight check; the native
methods and effects must pass on the actual deployed version. There is no
pinned 1.545.1 requirement, automatic model substitution, downloaded binary,
retry, or alternative transport fallback.

The runner reads existing proxy settings from `~/.acp_proxy/config.json`, if
present, and preserves environment proxy/certificate settings. It does not
create or modify that config. Authentication uses the existing repository
OAuth bridge: explicit Copilot token environment variables, or the prior IDE
`oauth.json` login. Missing or ambiguous credentials fail clearly. It does not
extract credentials from a keychain or `auth.db`, or initiate a new login.

## Interpret and return the result

The runner prints the evidence directory. A model listing is **catalog only**;
it is not a successful implementation test. For the smoke test, exit code 0
means every required acceptance and cleanup check passed. Any missing
capability, refusal, protocol failure, timeout, cancellation, or incomplete
cleanup produces a nonzero exit and a failure result.

Bring back `result.json` from the reported directory. It records the failed
stage when the test cannot finish. Keep the accompanying ordered wire evidence
for diagnosis. Output directories default to this experiment's ignored
`results/` directory; `--output-dir` can select a new directory elsewhere.
Credential-bearing runtime state lives separately in a temporary directory
and is cleaned after the owned child exits. It is not included in the result
artifacts. Captured evidence redacts known credential values.

The language server is launched directly with `--stdio` and bundled mode
forced. No MCP servers are supplied. CLI guards record and reject attempted
PATH launches. These are observable test controls, not an OS security sandbox.
The callback permits only the declared scratch file and exact small Python
program, launched by argv without a shell; it has no arbitrary command access.

A pass establishes this bounded task on the recorded target installation.
It does not qualify automatic compression, restart/crash recovery, arbitrary
commands, parallel sessions, or the full Meadow integration. Those remain
separate work after the target result.

## Local checks

The portable offline checks use no Copilot service:

```text
python -m unittest discover -s experiments/native_ide_smoke -p "test_*.py" -v
```

The authenticated smoke command above is the end-to-end acceptance check.
A local Mac pass is preparation evidence; only a run on the enterprise host
qualifies that target.
