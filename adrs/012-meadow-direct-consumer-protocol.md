# ADR-012: Authenticated Meadow-Direct Consumer Protocol

**Status:** Accepted; out-of-prompt session-state retention partially
superseded by [ADR-014](014-correlate-direct-session-state.md)
**Date:** 2026-08-09

> **Partial supersession note (2026-08-11):** ADR-014 replaces the requirement
> to retain known out-of-prompt command and configuration state. Recognized
> state-only updates are correlated, bounded, structurally validated and
> logged, then discarded unless they enforce an explicit Meadow contract.
> Exact selected-model integrity and prompt-scoped ordered event evidence
> remain binding.

## Context

The original HTTP surface was designed for stock OpenCode. It accepted
OpenAI-compatible message replay, inferred conversation identity from prompt
content, injected proxy-authored context, and exposed no durable operation
identity. Meadow requires a different boundary: its logical session identity,
invocation lifetime, prompt layers, correction policy, and reconciliation
semantics must survive changing prompt bytes without depending on OpenCode.

ACP is stateful, but its initialization response does not contain the model
catalog. The catalog and model configuration are session-scoped. ACP v1 also
defines `session/cancel` as a notification and requires
`session/set_config_option` to return the complete `configOptions` state.
The current agent does not negotiate stable session close, effect observation,
or usage reporting.

The owned ACP child may execute tools internally. Denying client callbacks
does not confine those agent-internal effects. The direct protocol therefore
has to report execution authority rather than imply a sandbox.

## Decision

### Explicit, isolated consumer modes

Every process start and every programmatic `run()` call must select exactly one
mode:

- `meadow-direct` performs operations only through authenticated
  `/meadow/v1/*`; unauthenticated `/health` reports mode but not capability
  state. Its `/v1/*` catch-all is rejection-only and unauthenticated so a stock
  OpenCode caller receives a structured migration error instead of a generic
  authentication failure.
- `opencode-legacy` exposes the existing `/v1/*` compatibility API.

Each mode returns a structured incompatibility error for the other mode's
routes. There is no default, autodetection, or legacy fallback.

Legacy mode is deprecated for the complete 0.2.x release line. The first 0.3.0
release removes it. Release 0.3.0 is the removal trigger; it is not conditioned
on traffic heuristics or silent caller detection.

### Direct startup and authentication

Direct mode requires a launch-scoped bearer secret containing at least 32 UTF-8
bytes. The secret is present before readiness, authenticates capability,
status, and mutation routes, is never forwarded to the ACP child, and is not
written to metadata or logs.

The direct ACP child receives an allowlisted environment: process path/home,
temporary and locale settings, XDG configuration/cache paths, certificate and
network-proxy configuration, required OS runtime keys, and the non-secret
Copilot enterprise URI. Provider API keys, Meadow
credentials, launch attestation, and unrelated `*_TOKEN`, `*_SECRET`, or
`*_API_KEY` values are excluded. Legacy mode retains its separate compatibility
environment behavior.

`trusted-host` direct mode binds only to loopback. A non-loopback bind is
accepted only for the explicit `confined-container` profile when the managed
launcher supplies its private-transport attestation and the process observes a
container-runtime marker. The marker proves only process placement; the proxy
cannot observe how a container port is published. Meadow's managed launcher
publishes the port to host loopback. A standalone operator must provide an
equally private transport (or add authenticated TLS) and is responsible for the
truth of the attestation. A caller-set environment bit alone is not evidence of
confinement. This profile also does not claim that ACP-agent internal tools are
callback-confined.

Startup creates exactly one internal, non-prompted ACP session to discover the
model catalog. Before HTTP readiness, that same session must prove
`session/set_config_option` by returning the complete `configOptions` state
with the catalog default as the exact model `currentValue`. Method absence,
malformed or incomplete state, or a different current model fails startup;
there is no acknowledgement-free fallback. That session is not a Meadow
logical session. Capability reads create no sessions. Backend close is reported
as unsupported until ACP negotiation proves otherwise.

ADR-006 owns executable discovery and the global minimum language-server
version. Direct startup adds this behavioral capability proof because version
admission alone is not exact model acknowledgement. Every later logical
session independently repeats the same complete exact-model proof.

This internal catalog session is control-plane startup work and necessarily
precedes inbound authentication because ACP initialization has no catalog.
Accordingly, the pre-effect rejection rule for unauthorized, wrong-mode, and
incompatible direct requests means that those requests induce no additional
`session/new` or prompt work; it does not mean the process can advertise a
model catalog without its one startup probe. A future ACP initialization
catalog would remove this explicit exception.

### Handshake and identity

The authenticated capability document reports the exact protocol identifier
and major, package version, immutable continuity generation, selected mode,
canonical workspace, execution-authority profile, resource limits, model
catalog, normalized feature states, and raw negotiated ACP evidence.

Every mutation pins protocol major and continuity generation and carries a
caller-minted operation ID. Session creation additionally carries a Meadow
logical session ID. Prompt mutations carry an invocation ID; cancellation has
an independent operation ID and target operation ID.

One logical session maps to one real ACP session. Session creation selects a
catalog model and is successful only when the complete model config state
reports that exact model as `currentValue`. Actor labels are evidence only and
never choose an ACP agent.

All wire identities use one path-safe opaque grammar: an ASCII alphanumeric
first byte followed only by ASCII alphanumerics or `._~-`. Before dispatch,
the proxy records the operation ID and canonical request
digest in a generation-long bounded ledger. An identical duplicate joins or
returns the same record. Conflicting reuse fails. Ledger entries and retired
session tombstones are never evicted; exhaustion rejects new work.
`max_sessions` therefore bounds generation-long logical-session mappings,
including retired and lost tombstones, rather than only currently live
sessions. Retirement does not create reusable identity capacity.

### Prompt lifetime

Direct mode accepts one model-facing `prompt` field. Meadow's current prompt
already contains legal typed routes; the proxy neither extracts nor duplicates
them. A separate prose `output_contract` accompanies each new invocation.
Digests are evidence and admission metadata and are never rendered.

- The initial request submits the stable-instruction field exactly once. The
  field must be present, but empty bytes are valid and have the ordinary SHA-256
  digest of empty bytes.
- The initial and later invocation requests submit the current prompt and one
  complete prose output contract.
- Correction and continuation requests contain only their delta plus the
  current invocation and contract digest. They cannot target an older
  invocation or retransmit stable, prompt, route, or contract text.

Results report `submitted_once` for the initial stable layer and
`not_resubmitted_same_session` thereafter. The latter is a byte-submission
fact, not a claim that the model behaviorally recalled those instructions.

Direct mode forbids `--system-prompt`, context-file injection, synthetic
identity messages, and any proxy-authored prompt context. This is first-turn
ACP prompt injection, not a provider-native system or developer role.

### Serialization, settlement, and continuity

There is one active prompt per logical session. The initial direct profile also
serializes prompts globally because safe cross-session parallel cancellation
has not been established by a live probe. Queue admission is atomic and
bounded; overflow fails before ACP dispatch. Cancelling queued work settles it
locally without sending `session/cancel` and it can never dispatch later.

After dispatched cancellation, the proxy accepts late updates until the
original `session/prompt` settles. A deadline is `timed_out` only when ACP
settles with `stopReason=cancelled`; another or absent stop reason is
`in_doubt`. A spontaneous cancelled stop is `cancelled` and makes the session
non-reusable. Retirement rejects active, unknown, already-retired, and later
work.

The continuity generation owns the ACP child, ledger, logical session map,
prompt lock, queue reservations, collectors, and execution tasks. Unexpected
ACP stdout closure fails pending JSON-RPC work, quarantines all of that state,
marks unsettled work `in_doubt`, rejects all further direct admission, removes
readiness, and stops the proxy. A fresh healthy generation requires a newly
initialized and negotiated child; it is never minted over the failed child.

The transport inserts the terminal `session/prompt` response into the same
ordered internal stream as session updates. Prompt completion cannot overtake
an unsettled permission/callback response. Late prompt output, tool, or thought
updates after that terminal boundary, unknown response IDs, malformed or
unknown direct updates, and selected-model drift revoke continuity. Recognized
session-state updates outside an active prompt may race ahead of `session/new`
settlement. Direct mode admits them only when boundedly correlated to an
outstanding create or a known session and confirms every provisional identity
against an eventual `session/new` response. It validates their recognized
envelopes and control-update bounds and logs their structural shape, but does
not retain command, current-mode, non-model configuration, usage, or
session-information payloads because Meadow v1 neither exposes nor acts on
them. Selected-model state is the exception: a configuration update must agree
with an in-progress binding expectation and, after exact acknowledgement, with
the session's acknowledged model. Malformed or drifting selected-model state
revokes continuity. Message, thought, plan, and tool updates outside an active
prompt remain invalid and revoke continuity.

### Evidence and authority

Ordered ACP updates are request-scoped. Normal results report exact observed
tool-call IDs and ordered raw events. Missing effect observation and usage are
`unavailable`, never inferred as zero. Negotiated response/event limits do not
silently truncate a successful result: overflow sends cancellation, waits for
settlement, fails visibly, and retains the exact bounded evidence prefix with
an explicit completeness flag.

When received during an active prompt, ACP `usage_update` and
`session_info_update` payloads have no normalized shape proven by this
implementation. Direct v1 retains them only as bounded ordered raw diagnostics,
advertises usage reporting as unsupported, and derives no counter or
session-information claim from their contents. Outside an active prompt they
are correlated, structurally validated, logged, and discarded under ADR-014.

The reader bounds cumulative event bytes and event count before queue
retention, in addition to the normalized result limits. Incoming callback
tasks are separately bounded, associated with their active session, tracked to
settlement, and cancelled during stop/abort. A child that outruns either bound
causes fail-closed continuity quarantine rather than unbounded memory growth.

Direct initialization advertises no permission, filesystem, or terminal
callbacks. Permission requests settle as cancelled; every other unadvertised
callback fails closed. `allow_always` is forbidden. This policy does not
constrain tools executed internally by the ACP agent, so the capability
document separately reports trusted-host or container-boundary authority.

## Relationship to Earlier Decisions

- ADR-001 remains historical and governs explicit OpenCode legacy mode only;
  its rejection of a direct Meadow path is superseded.
- ADR-002, ADR-003, ADR-004, and ADR-011 govern explicit OpenCode legacy mode
  only. Their content-derived identity and proxy-authored prompt rules do not
  enter direct mode.
- ADR-007's observation that the ACP agent owns internal tools remains valid.
  Its permissive client-callback behavior is amended: direct mode advertises
  no callbacks and fails closed.
- ADR-008 remains the process/workspace decision and is amended by mandatory
  consumer mode, direct authentication, and bind-profile validation.
- ADR-009's empirical single-process and same-session cancellation findings
  remain evidence. Its unimplemented pre-created pool, content-derived
  affinity, and per-session-only concurrency policy are superseded. Direct
  mode creates sessions explicitly and begins with conservative global
  serialization.
- The former index entry for ADR-010 had no artifact. Collision evidence and
  active decisions are carried by ADR-003, ADR-004, ADR-007, and this ADR; the
  dangling route is removed rather than reconstructed as an accepted record.

## Consequences

- Meadow continuity no longer depends on OpenCode replay or prompt hashes.
- Response loss is reconciled through generation-pinned operation status, not
  blind redispatch.
- Direct and legacy callers cannot silently enter each other's semantics.
- Cross-session parallel prompts, session load/resume, backend close, native
  structured output, provider-role placement, effect observation, and usage
  reporting remain unsupported until separately proved and negotiated.
- The direct protocol is intentionally stricter than the compatibility API;
  adding a field or capability requires a protocol-compatible decision and
  observable proof.
