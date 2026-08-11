# ADR-014: Correlate Direct Session State Without Retaining Unsupported State

**Status:** Accepted
**Date:** 2026-08-11
**Supersedes:** ADR-012's out-of-prompt command and configuration retention
policy
**Related ADRs:** [ADR-012](012-meadow-direct-consumer-protocol.md)

## Context

A real `copilot-language-server` can emit a `session/update` before its
`session/new` response. Full-suite black-box runs observed both
`available_commands_update` and `config_option_update` in that position. The
direct client admitted only a provisional command update, so the equally
correlated configuration update revoked continuity before the proxy became
ready. Process scheduling and pipe buffering made the ordering intermittent.

Meadow v1 does not expose or act on agent commands, current mode, non-model
configuration, out-of-prompt usage, or session information. Retaining those
payloads as generation-long client state would add memory and reconciliation
obligations without supporting a Meadow claim. Blindly ignoring notifications
would be unsafe, however: their session identity and selected-model state can
reveal a correlation failure or loss of exact model binding.

Prompt-scoped updates are different. `PromptResult.events` is an explicit
Meadow evidence contract, so recognized updates observed during an active
prompt remain ordered request evidence even when the proxy cannot normalize
their contents.

## Decision

Direct mode distinguishes session-state notifications from prompt/effect
notifications.

Recognized session-state updates outside an active prompt are admitted only
when their session identity is boundedly correlated to a known session or an
outstanding `session/new`. At most one provisional session identity is admitted
per unresolved create, and every provisional identity must match an eventual
`session/new` response. Extra, orphaned, or mismatched identities revoke
continuity.

The client validates the recognized envelope, the kind-specific fields it
relies on, and control-update size before handling an out-of-prompt state
update. It records payload-safe structural diagnostics, then discards command,
current-mode, non-model configuration, usage, and session-information
payloads. Those payloads are not accumulated as client state and do not create
Meadow claims. Unknown update kinds, malformed recognized updates, and limit
violations remain fail-closed.

Selected-model configuration is the exception. A `config_option_update` must
agree with an in-progress exact-model binding expectation and, after
acknowledgement, with the session's acknowledged model. The complete
`session/set_config_option` response remains the authoritative acknowledgement.
Malformed selected-model state or model drift revokes continuity; the client
retains only the acknowledged model already required by Meadow's session
contract.

Message, thought, plan, and tool updates have prompt/effect semantics. They are
invalid outside an active prompt and continue to revoke continuity there.
During an active prompt, recognized updates remain subject to the existing
reader-side event bounds and are retained as ordered `PromptResult.events`.
Prompt-scoped `usage_update` and `session_info_update` therefore remain raw
evidence only: usage reporting stays unavailable, and no normalized usage or
session-information claim is derived from them.

## Consequences

- The observed pre-response state-update ordering no longer creates a
  scheduling-dependent startup failure.
- Unsupported session state does not become unused generation-long memory.
- Provisional session identity and exact selected-model integrity remain
  fail-closed.
- Meadow receives no new configuration or command surface. Adding one later
  requires an explicit protocol decision and the corresponding retained state.
- Prompt/effect attribution and bounded ordered prompt evidence are unchanged.

## Rejected Alternatives

### Retain every recognized session-state payload

Meadow has no consumer for most of this state. Retention would create an
internal cache with no defined freshness, reconciliation, or public meaning.

### Drop all out-of-prompt notifications without validation

This would mask malformed traffic, provisional identity mismatches, and model
drift. Correlation and exact-model state are meaningful even when the remaining
payload is not.

### Admit every pre-response update kind

Prompt/effect updates cannot be truthfully attributed before a prompt is
active. Only recognized session-state updates receive the bounded provisional
path.

### Reject every update before `session/new` returns

The live language server demonstrably emits legitimate session-state updates
in that order. Treating the ordering as a violation caused the reproduced
cross-environment flake.
