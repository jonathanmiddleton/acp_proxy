# ADR-019: Meadow Bridge product identity

**Status:** Accepted  
**Date:** 2026-09-23

## Context

The service's sole consumer contract is now Meadow's direct ACP integration.
A coordinated breaking rename can establish its owned names without aliases
because no active users require old configuration or command compatibility.

## Decision

The product is Meadow Bridge, the distribution and command are `meadow-bridge`,
and the Python package is `meadow_bridge`, starting at version 0.3.0. Owned
configuration is `~/.meadow_bridge/config.json`; owned environment names use
`MEADOW_BRIDGE_`. Meadow and workflow-algebra pin reachable source commits in
that order, including their lockfiles. No local path override is published.

The Git repository remains at its existing `acp_proxy.git` URL until a separate
hosted rename. Upstream Copilot/ACP names, OAuth stores and token variables are
unchanged. Preserve the current ACP transport, `/meadow/v1`, `meadow-acp-direct`,
wire `consumer_mode` and `proxy_version` fields, model binding, reconciliation
and deny-only direct callbacks. These wire names remain part of v1; they are
not a second product alias. Native IDE transport is a later implementation.

This supersedes ADR-008's product/package/command spelling and updates the
owned names used by the remaining ADRs without rewriting historical evidence.
The candidate on `diagnostics/native-ide-smoke` remains separate design work.

## Consequences

Consumers must use the new names. Old commands, packages, environment prefixes
and configuration paths are not read or installed. Public documentation describes
the actual ACP integration; it makes no native IDE tool-execution claim.

## Rejected alternatives

Aliases, configuration fallback and dual packaging would preserve old contracts
without users. A GitHub repository rename is a separate hosting operation and
must not be invented in installable source URLs.
