# ADR-018: Remove the deprecated OpenAI-compatible adapter

> **Native backend supersession:** [ADR-020](020-native-ide-backend.md) replaces
> the ACP-specific transport, callback, binding, and wire provisions below.
> Historical evidence is retained; consult ADR-020 for current ownership.

**Status:** Accepted  
**Date:** 2026-09-23

## Context

The direct Meadow consumer now owns explicit sessions, prompt layers, operation
identity, reconciliation, and settlement. There are no active users requiring
the deprecated OpenCode compatibility surface. Keeping two inbound contracts
would retain unsupported replay and callback authority paths.

## Decision

Remove the OpenAI-compatible `/v1` API, its rejection-only migration routes,
`opencode-legacy` startup, the consumer-mode selector, replay/hash identity,
proxy-authored context injection, adapter configuration, and permissive client
callbacks. The remaining ACP client always applies the existing direct deny
policy and strict response correlation. Shared binary discovery, OAuth handoff,
network proxy settings, diagnostics, and process ownership remain.

Preserve `/meadow/v1`, `meadow-acp-direct`, the v1 wire fields (including
`consumer_mode` and `proxy_version`), exact model binding, generation-pinned
reconciliation, bounded evidence, and direct callback policy. This decision does
not implement the proposed native IDE backend.

ADR-001, ADR-002, ADR-003, ADR-004 and ADR-011 are historical. This supersedes
ADR-007's permissive callback provisions, ADR-008's OpenCode startup provisions,
and ADR-012's dual-mode/deprecation provisions. The rest of their direct ACP
constraints continue to apply. Historical experiments and run evidence remain
unchanged.

## Consequences

There is one inbound contract and no compatibility alias, fallback, adapter or
migration mechanism. Old CLI options fail argument parsing; removed routes
return ordinary not-found responses. Meadow's independent native OpenCode
backend remains owned by Meadow and is unaffected.

## Rejected alternatives

Retaining legacy aliases, dual startup modes or migration guidance would keep
a second live contract without users. Importing the native diagnostic backend
would combine this removal with an unqualified transport and authority change.
