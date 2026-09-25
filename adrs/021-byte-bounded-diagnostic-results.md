# ADR-021: Byte-bounded diagnostic results

**Status:** Accepted  
**Date:** 2026-09-24

## Context

A native turn exhausted the 4096-event evidence ceiling while its response was
still being streamed. Each ordinary progress chunk consumes an event, so an
event-count ceiling can cancel a byte-bounded response solely because the
provider chose smaller chunks. Ordered native evidence remains useful for
diagnosis and is retained by the direct contract.

The HTTP result includes raw events, selected tool/permission/effect events,
typed observations, receipts and response text. A client-tool receipt can appear
in four places. A transport bound estimated from twice the raw event bytes and
a fixed allowance per event does not bound the actual serialized result.

## Decision

Release 0.4.1 removes `max_event_count` from the strict direct limits and removes
per-turn event-count enforcement. There is no legacy field or count-based
fallback. The event payload budget becomes 16 MiB (16,777,216 bytes), measured
using the existing compact JSON encoding. Response text remains bounded at
2,000,000 UTF-8 bytes. Byte overflow still cancels and settles the native turn,
retains the admitted evidence prefix and reports incomplete evidence; it cannot
become a successful truncated result.

The limits advertise `max_http_response_bytes`, defaulting to 128 MiB
(134,217,728 bytes), with a minimum of 4096 bytes for a compact delivery error.
The HTTP owner constructs a JSON response once and measures its actual encoded
body, including Unicode encoding, escaping, metadata, arrays and duplicated
receipts. A body within the limit is sent unchanged. This budget is independent
of the raw event-payload budget; consumers use the advertised body limit rather
than reconstructing a capacity estimate from other limits.

When the full body exceeds the advertised limit, the route returns HTTP 500
with `error.code: response_too_large`, `actual_bytes` and `max_bytes`. Operation
routes also report the exact `operation_id` and `operation_state`. This is a
delivery failure: the ledger's execution state and complete retained result
are unchanged. It neither truncates the result nor relabels a completed
operation as failed. Reading or repeating the same operation returns the same
delivery failure without redispatching the native request. Consumers surface
this determinate error instead of repeatedly reconciling or replaying work.

Native progress, typed tool/permission observations, effect receipts and the
operation ledger remain intact. Optional raw capture remains separate and
unchanged: its limits apply to pending writes (32 MiB or 4096 records), not to
total recorded events or file size. Capture failure remains visible and revokes
continuity under ADR-016 and ADR-020. Request, prompt, tool-output, native frame,
session, operation, queue and deadline limits are unchanged.

This is a coordinated development cutover within direct protocol major 2.
Bridge and its strict consumers must deploy together; older schemas are not
accepted by compatibility adapters.

## Supersession

This replaces ADR-012's cumulative per-turn event-count requirement under
"Evidence and authority", retained by ADR-020's resource-bound obligations.
It refines ADR-020's evidence delivery with an actual encoded HTTP-body limit.
Their remaining lifecycle, settlement, replay-protection and diagnostic
integrity requirements continue to apply.

## Rejected alternatives

- Raising the count ceiling preserves dependence on provider chunking and
  complicates guessed transport limits.
- Dropping raw events or silently trimming evidence loses diagnostic content.
- Estimating HTTP size from a fixed raw-payload multiplier does not establish
  the actual body bound across all evidence shapes and encodings.
- Converting delivery failure to execution failure misstates an operation that
  may already have completed effects.

## Verification

Native-client regression coverage drives more than 4096 real framed progress
notifications and checks the complete response and ordered evidence. Router
coverage checks duplicated Unicode/escaped receipts, exact-boundary delivery,
oversize diagnostics, unchanged ledger results, and replay without redispatch.
The repository checkout gate remains the required completion boundary.
