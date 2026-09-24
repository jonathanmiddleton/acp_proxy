# ADR-015: Order Direct Model Binding at RPC Settlement

> **Native backend supersession:** [ADR-020](020-native-ide-backend.md) replaces
> the ACP-specific transport, callback, binding, and wire provisions below.
> Historical evidence is retained; consult ADR-020 for current ownership.

**Status:** Accepted
**Date:** 2026-09-22
**Supersedes:** ADR-012 and ADR-014's requirement that configuration updates
match the requested model throughout an unresolved binding RPC
**Related ADRs:** [ADR-012](012-meadow-direct-consumer-protocol.md),
[ADR-014](014-correlate-direct-session-state.md)

## Context

The direct client creates an ACP session with its own advertised current
model, then selects Meadow's requested model before exposing the session as
ready. A configuration notification can report the initial model while that
selection RPC remains unresolved. Sending a selection request does not prove
that the agent has already applied it.

On 2026-09-22, an observation-only run of
`test_meadow_direct_proxy_model_binding_and_continuity` captured this sequence:

1. `session/new` reported `claude-sonnet-5` as the current model.
2. The proxy requested `gpt-5.3-codex` with `session/set_config_option`.
3. Before that RPC settled, a `config_option_update` reported
   `claude-sonnet-5` again.
4. The proxy classified this as selected-model drift, revoked continuity, and
   returned session creation as `in_doubt` with `error.code=continuity_lost`.

The child had not exited when the proxy rejected the update, and no prompt
had been dispatched. Retained passing runs received the initial configuration
notification before the new-session response. A deterministic in-process
reproduction using the actual client and transport confirmed that moving the
same prior-model notification across selection dispatch changed success into
failure. A full suite passed while an isolated live run failed; suite
membership is therefore not required for this race. These observations do not
establish a change in its frequency.

ADR-014 admits correlated session-state notifications that race with session
creation, but its pending-model invariant still treats the requested target as
already established. That invariant confuses a valid transitional snapshot
with drift after binding has succeeded.

## Decision

Direct model binding has an explicit, session-correlated transition from the
exact prior model to the requested target. The prior model comes from that
session's reported or successfully bound state, never from another session's
catalog. Overlapping bindings must not allow one response to settle another
binding attempt.

While a binding RPC is unresolved, a structurally valid configuration
notification may report either the exact prior model or the requested target.
It does not acknowledge the RPC or mutate the session's bound model. A missing
model, malformed model value, or third model remains a protocol failure. The
existing correlation and control-update bounds continue to apply.

For the standard `session/set_config_option` strategy, the response must still
contain the required complete `configOptions` state and explicitly report the
exact requested model. A wrong, missing, or malformed acknowledgement fails;
a notification cannot substitute for it. The negotiated Copilot
`session/set_model` strategy retains its existing successful-RPC-settlement
contract and does not gain an independently observed post-state claim from a
notification. No Meadow session becomes ready before its negotiated binding
contract succeeds.

The successful binding boundary is the response observer in the ordered ACP
read path, before the transport resolves the request future. At that boundary,
the transition closes and only the target model remains admissible. An update
reporting the prior model after the acknowledgement is drift even if both
messages are already buffered and the awaiting coroutine has not resumed.

An error response ends its binding attempt without establishing the target.
The prior model remains the baseline. Startup's existing method-not-found
negotiation may then begin a separate attempt through its supported selector;
other errors retain their existing failure behavior. Cleanup and child
teardown clear transitional state so a later operation cannot inherit it.

## Consequences

- Session creation no longer depends on whether an initial configuration
  notification is scheduled before or during model selection.
- Exact acknowledgement and post-binding drift detection remain mandatory.
  Recognizing an unresolved transition does not authorize inference with an
  unacknowledged model.
- Transition state is bounded to unresolved bindings and retains only the
  correlation and model values needed to validate them. Unsupported
  configuration payloads remain unretained under ADR-014.
- Tests must exercise real client and transport ordering on both sides of the
  response boundary, including buffered post-response updates before coroutine
  resumption, rejected acknowledgements, and selector negotiation errors.

## Rejected Alternatives

### Require the target as soon as selection is dispatched

The captured live ordering demonstrates that a prior-model snapshot can still
arrive at that point. Dispatch is not acknowledgement.

### Ignore every model update while binding

This would hide malformed, missing, and unrelated model state. Only the exact
prior model and requested target belong to the transition.

### End the transition when the awaiting coroutine resumes

The reader can dispatch several buffered messages before that coroutine runs.
This would admit stale model state received after the authoritative response.

### Retry creation, delay selection, or test only the default model

These choices change the probability or visibility of the race without
correcting the production ordering assumption. Selecting only the current
default also avoids exercising the transition that exposed the failure.
