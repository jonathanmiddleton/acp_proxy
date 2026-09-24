# Native IDE first delivery

Status: Done

Deliver one managed Meadow-to-Bridge-to-Copilot path using the installed
language server over LSP-framed stdio. Preserve caller-owned logical session
IDs, generation-pinned operation reconciliation, instruction lifetime and
typed uncertainty. No Copilot CLI, MCP, legacy ACP production route, or
compatibility fallback is part of the new release.

## Coordinated contract

The breaking direct protocol is `meadow-bridge-direct`, major 2, at
`/meadow/v2`. Health is `{status: "ok", protocol_major: 2}`. Capability
identity uses `bridge_version`; ACP-specific capability fields and the
obsolete consumer-mode field are removed. Server identity is explicit native
IDE evidence. Retain the existing operation/status/error shape and limits.

Session creation requires `permission_policy: {version: 1, mode: "allow_all"}`.
Its canonical sorted compact JSON SHA-256 is acknowledged as
`permission_policy_digest` and participates in the create-operation digest.
Meadow's adapter supplies this fixed policy; the generic session interface is
unchanged. A create acknowledgement returns the caller's logical session ID,
`backend_session_id: null`, `binding_state: "allocated"`, admitted model ID,
instruction/policy digests and continuity generation. Native conversation
creation occurs only on the first prompt. Native binding and successful initial
instruction submission are distinct facts. Retirement before a prompt performs
no inference.

Prompt results use `stop_reason` rather than `acp_stop_reason`, retain the
existing operation/invocation/digest identity and ordered event envelope, and
carry the native conversation ID after binding. Native events retain their
native meanings; no fabricated ACP updates. Tool, permission and effect
evidence describes the Bridge-executed scope separately from server-owned
tools. Raw native usage remains unavailable as normalized counters until its
mapping is qualified. Cross-session parallelism, transparent recovery, native
output schemas and provider system-role placement remain unadvertised.

## Runtime boundaries

- Native transport/client own framing, request correlation, native model
  catalog, conversation lifecycle, progress, callback identity, deduplication
  and generation failure. One owned server process per workspace.
- Permission policy and confirmation handling are separate small owners.
  The executor checks the policy again on every actual invocation.
- Workspace tools support file creation and exact-context edits, including
  multiple compatible file changes, and non-interactive command execution.
  Paths are admitted under the workspace; commands run under the process
  identity, not an OS sandbox. Preconditions fail before the corresponding
  effect; any partial multi-file effects are explicitly reported.
- Commands use an explicit shell: Windows PowerShell 5.1 by default, PowerShell
  7 only when selected; a configured/default POSIX shell on macOS. No custom
  shell grammar. Bound duration and stdout/stderr with explicit truncation.
  Cancellation and shutdown settle owned POSIX process groups or Windows Jobs
  before completion. Foreground commands are supported; deliberately detached
  background services are outside this contract.
- Native built-in read/search tools remain available initially. Catalog
  replacement and role-derived policy translation are deferred.

## Acceptance

Prove create-without-inference, real create/edit/command callbacks, two prompts
on one logical/native session, original role and skill-catalog delivery,
retirement, malformed/stale/duplicate callback rejection, first-turn failure
after native creation, cancellation and child-loss settlement. Completion
requires the affected repository gates, live native integration and native
Windows shell/process ownership qualification. Preserve Meadow's independent
OpenCode path. Publish dependency revisions in Bridge, Meadow, then
workflow-algebra order; no local dependency overrides in committed metadata.

The diagnostic branch and archived experiments are empirical inputs. New
production modules meet current typing and lifecycle standards; historical
test obligations are preserved or explicitly replaced by native equivalents.

## Work ownership

Production work uses the `codex/native-ide-bridge` worktrees in the three
repositories. Native transport/client, workspace tools, and Meadow adapter
changes have separate writers. The coordinating writer owns Bridge HTTP/state
integration, public documentation, assembled validation and delivery. Full
gates run serially against a stable assembled candidate.

## Qualified delivery

The coordinated implementation was published on `codex/native-ide-bridge`:

- Bridge `13a2894d23043587eca29930208276bc636f3b3a` provides version `0.4.0`.
- Meadow `d781d4d0cd7ace27cb0f2e39b66eb899c78dabd1` pins that Bridge revision.
- workflow-algebra `ff14037dd7961ed237b66a9f9671be4ea8d835a9` pins both revisions.

Bridge's complete checkout gate exited zero on macOS and native Windows,
with 300 tests on each platform, including real two-turn Copilot integration.
The observed language-server versions were `1.545.1` and `1.537.5` respectively;
Windows command tests used Windows PowerShell `5.1.26100.9457`.

Managed Meadow acceptance passed on the macOS host and in the freshly built
Linux container with language server `1.523.3`. Each run retained the same
logical/native session across two turns, recalled distinct role/skill/support
markers after their source files were removed, performed receipt-backed file
creation/edit/command effects, and retired its conversation with settled
resources. No correction or request replay was used. These local runs observed
no confirmation callbacks; execution still checked the explicit session policy.

Meadow's complete gate exited zero with nine successful checks, 6,906 root
tests passed, two unchanged platform skips and 30 prototype tests passed.
Its retained full-runtime experiments additionally prove allocated/null native
identities become bound during prompting and retire those exact bindings,
including a mixed OpenCode/Bridge run. workflow-algebra's complete gate exited
zero with 26 successful checks and no failed or skipped steps.

The Windows qualification covers Bridge; the complete Meadow and
workflow-algebra gates ran on macOS. These observations do not expand the
unsupported capabilities listed above. Final tracking-only commits do not
change the implementation revisions pinned by consumers.
