# ADR-017: Change-Relative Typing and a Complete Checkout Gate

**Status:** Accepted

**Date:** 2026-09-23

**Related ADRs:** [ADR-005](005-fail-loud-testing.md)

> **2026-09-23 amendment:** [ADR-018](018-remove-openai-compatible-adapter.md)
> removes the deprecated OpenAI-compatible adapter and consumer-mode selection.
> Historical observations below are retained; only remaining direct ACP
> provisions govern the current service.

## Context

ACP Proxy is about to evolve alongside Meadow. Its standards require type
annotations, but the checkout has no type-checker or lint completion command.
Existing annotations do not establish that consumers agree with those types.
A blanket strict-clean migration would mix protocol changes and historical
cleanup into the tooling adoption.

Meadow already has a change-relative validator and a checkout gate that retain
full command logs and detect source mutations during validation. The proxy can
reuse those algorithms while adapting its canonical branch and `src/` layout.
Its Python 3.11 floor, live Copilot tests, and no-skips rule remain binding.

## Decision

Port `scripts/checkout_gate.py` and `scripts/typecheck_change.py` from Meadow.
`python3 scripts/checkout_gate.py` is the required completion command. It runs
locked development-environment synchronization, change-relative Mypy/Pyrefly,
the entire test suite, Ruff, and content-sensitive checkout preservation.
Every step reports its own outcome and retains complete output. The proxy has
no Meadow prototype suite. Its tests run serially to bound live Copilot load.

The typing validator compares the merge base of `HEAD` and `refs/heads/main`
with the working tree, including staged, unstaged, and untracked work. Mypy
runs in strict mode; Pyrefly mirrors Meadow's legacy migration preset with
`no-any-return` errors. A change must introduce no new diagnostics and leave
none in declarations it changes. Old diagnostics outside the affected surface
are compared transiently, never recorded in a maintained suppression baseline.
Source changes check maintained consumers as well as the source package.

Each comparison side resolves `src/` imports from its own snapshot. Old
snapshots without Pyrefly configuration receive the initial checking rules;
configured snapshots retain their own settings so configuration changes remain
observable. Checker caches are disposable acceleration, not diagnostic authority.
They must not hide same-size or timestamp-preserving edits or compare against
the current editable installation in place of the archived source.

The coding standards also adopt Meadow's invariant-owning immutable values,
explicit semantic states, single ownership of decisions, and test-retention
principles. ACP-specific ADRs still govern protocols and resource lifetimes.
Adoption does not remove the deprecated OpenCode contract or upgrade Python.

`tests/conftest.py` enforces ADR-005 for collection and runtime skips. A suite
with passing tests and skipped obligations cannot return success. The complete
gate still requires the sanctioned language server and usable authentication.

## Consequences

- New work receives mechanically checked typing obligations without requiring
  an unrelated repository-wide rewrite. This does not certify all legacy code
  as strict-type clean; touched declarations must improve to that standard.
- One command provides integrated evidence and fails visibly when prerequisites
  are unavailable. Partial suites remain development feedback.
- Gate and validator tests use real temporary Git repositories and process
  boundaries. Real checker regressions cover snapshot isolation, affected
  declarations, configuration adoption, and cache behavior.
- A clone must retain the canonical local `main` ref and install `uv`.
  Intentional dependency changes update `uv.lock` before the gate runs.

## Rejected Alternatives

- **Annotations without execution:** do not detect inconsistent callers or
  accidental `Any` returns.
- **A checked-in debt baseline or per-file exclusions:** can outlive the code
  they excuse and hide newly touched violations.
- **Only whole-repository strict cleanliness:** makes adoption depend on a
  broad semantic migration unrelated to the immediate evolution work.
- **Copy Meadow-specific policies unchanged:** its prototype suites, branch
  name, Python floor, and runtime architecture are not ACP Proxy's contracts.
- **Treat unit-only validation or skipped integrations as completion:** violates
  ADR-005 and removes the actual external boundary from the evidence.
