# ADR-016: Opt-In Raw ACP Event Capture

> **Native backend supersession:** [ADR-020](020-native-ide-backend.md) replaces
> the ACP-specific transport, callback, binding, and wire provisions below.
> Historical evidence is retained; consult ADR-020 for current ownership.

**Status:** Accepted
**Date:** 2026-09-22
**Related ADRs:** [ADR-012](012-meadow-direct-consumer-protocol.md),
[ADR-014](014-correlate-direct-session-state.md)

## Context

Meadow parses the proxy's concatenated agent-message text as structured JSON.
The current filter already excludes agent-thought, plan, and tool updates.
A retained run showed a JSON parse correction with message chunks but no
thought chunks. Metadata-only logs and normalized durable evidence did not
retain the rejected text or message metadata needed to establish a reliable
answer boundary.

## Decision

An explicit `--raw-event-file` option enables a separate diagnostic NDJSON
artifact. Transport records complete decoded `session/update` envelopes before
client validation/projection, plus prompt dispatch intent and correlated raw
terminal prompt responses or errors. Dispatch intent records request/session
identity without prompt bodies. Each record carries format version, capture
identity, sequence, and UTC observation time.

This opt-in artifact is independent of ordinary payload-safe logging,
normalized HTTP results, and session-state retention. ADR-014's unsupported
state discard policy remains the runtime rule; explicitly requested diagnostic
capture may persist those observed envelopes without making state claims.
The file can contain agent text and tool content. It never captures inbound
HTTP credentials, child environment, or stderr.

Sequencing and serialization happen synchronously at wire observation.
Existing dispatch observers run without an added await. A single async writer
performs filesystem I/O off the event loop, with bounded queued bytes and
record count. Accepted records are flushed intact. Overflow or I/O failure
reports incomplete evidence and revokes continuity. Cleanup joins the writer;
capture failure produces an unsuccessful proxy exit.

The destination is opened before child startup, with owner-only creation
permissions where supported. Complete existing files are appended using a
fresh capture identity per process. An unterminated tail fails startup rather
than corrupting subsequent records. Clean capture lifetimes have start/end
records; crash/interruption evidence remains visibly incomplete. Successful
capture is never rotated or truncated.

## Consequences

One instrumented run can distinguish actual upstream message identity and
metadata from text assembly defects. Capturing events does not change answer
selection or Meadow's JSON validation/repair policy. Capture is disabled unless
the caller explicitly chooses a destination; Meadow owns the per-run option
and diagnostic path in its managed launcher.
