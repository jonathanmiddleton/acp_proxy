# Coding Standards

Read this document before making any changes to the codebase. These standards
are binding and reflect the realities of building on partially documented,
externally controlled interfaces.

Meadow Bridge's ADRs remain
authoritative for its protocols, resource ownership, and failure policy.
See [ADR-017](adrs/017-change-relative-typing-and-checkout-gate.md) for the
adoption decision and migration boundary.

## Python and Static Types

- **Python 3.11+** is required. Keep syntax and APIs compatible with that floor.
- **Type hints everywhere.** Annotate function signatures, return types, and
  non-trivial local values. Public methods and classes need docstrings.
- **Pydantic** owns HTTP request/response validation. Internal semantic values
  use immutable dataclasses or other invariant-owning types.
- **Validate with Ruff** before committing. `ruff check .` must pass; the
  repository pins its rule selection in `pyproject.toml`.

### Change-relative static type validation

Every maintained Python change is validated by Mypy in strict mode and Pyrefly.
The required completion command is:

```bash
python3 scripts/checkout_gate.py
```

The gate synchronizes the locked development environment before invoking
`scripts/typecheck_change.py`. The validator resolves the merge base of `HEAD`
and the canonical local ref `refs/heads/main`, independent of the current
branch's upstream. It includes committed branch changes, staged and unstaged
edits, and untracked Python files. Both checkers compare structured diagnostics
in cohesive affected scopes at the base and in the working tree, requiring:

- no new checker diagnostics; and
- no retained diagnostic inside a declaration changed by the work.

Unchanged diagnostics outside changed declarations do not enlarge the task.
This is a migration rule, not a claim that the entire existing repository is
already strict-type clean. Comparison is transient: do not add diagnostic
baselines, per-file allowlists, debt counts, or suppression budgets. Do not
obtain a pass with `Any`, ignores, unchecked casts, or weaker checker settings.
Validate and narrow dynamic external values at admission before they enter
semantic code.

The maintained scope includes `src/`, `tests/`, `scripts/`, and experiment
Python. Source changes also validate maintained consumers. For focused work,
run `python3 scripts/typecheck_change.py` after each bounded edit. Its
`--base <revision>` option deliberately inspects a different boundary; it does
not replace the complete checkout gate, which always uses `refs/heads/main`.

### Checkout completion

`python3 scripts/checkout_gate.py --list` lists the commands without executing
them. The complete gate owns locked dependency synchronization, change-relative
typing, all tests including live integration, repository-wide Ruff, and a final
content-sensitive checkout-preservation check. Tests run serially so the gate
does not multiply live Copilot sessions by the machine's CPU count.

Each command retains a full log and reports failure independently. A missing
tool, binary, credential, or failed check is a failure; a focused or partial
run is not completion. The gate must not rewrite tracked or untracked work,
change HEAD, or switch branches. Its disposable logs and ignored caches are
outside that source-state contract. Update `uv.lock` intentionally when
changing dependencies; validation uses `uv sync --locked --extra dev`.

## Type-Directed Semantic Design

For changes involving identity, state, ordering, causality, concurrency, or
durable facts, name the invariants and non-invariants first. Invalid states and
unauthorized decisions must be inexpressible through the supported public API.

1. **Encode states and authority.** Represent facts, decisions, projections,
   effects, failures, and material phases as distinct immutable nominal values.
   Model alternatives as closed unions of variant-specific types and handle
   them exhaustively with `assert_never`. The owner controls construction;
   adapters admit external facts and materialize returned effects.
2. **Make transitions structural.** Express governed behavior as a total or
   explicitly failing transition over typed inputs. Validation, preparation,
   execution, and settlement consume only their permitted predecessor states.
   Live and recovery adapters call the same owning transition; projections,
   diagnostics, and effect receipts cannot become alternate semantic inputs.
3. **Keep identity and retained state irreducible.** Prefer an existing domain
   identity. New identities expose checked construction, equality, hashing,
   and required boundary serialization, without unrelated primitive behavior.
   Before adding a durable field, cursor, mirror, or cached relation, show two
   legal histories that existing facts cannot distinguish but that require
   different behavior. Otherwise derive it once from the owner.
4. **Prove residual behavior.** Types do not prove purity, actual I/O, resource
   settlement, or behavioral equivalence. Use property and integration evidence
   for those obligations. Remove replaced constructors and alternate decision
   paths in the same cutover, within the authorized contract.

Keep a functional semantic core and thin imperative shells for I/O and resource
ownership. Mutable process handles, queues, task registries, and lifecycle
resources belong to their explicit owners; they are not mutable semantic
payloads shared across modules. [ADR-018](adrs/018-remove-openai-compatible-adapter.md)
removes the deprecated OpenCode adapter; the direct ACP contract remains binding.

## Module and Code Organisation

- Gather code that changes for the same reasons behind one owning contract.
  Functions have one responsibility; collaborating modules form cohesive
  packages with a deliberate public surface.
- Production package code lives in `src/meadow_bridge/`; maintained tests live in
  `tests/`, validation tools in `scripts/`, and experiments in `experiments/`.
  The standalone `src/acp_probe.py` and `src/acp_validate.py` diagnostics are
  not production imports.
- Follow the module ownership table in `AGENTS.md`. Consumers call the owner's
  supported API rather than reconstructing its decisions or reading internals.
- Read relevant ADRs before design changes. A contradictory decision requires
  a new superseding ADR; passing tests do not authorize a silent deviation.

### Inadmissible design practices

- **Validation outside construction.** Value invariants belong to checked
  constructors, `__post_init__`, or Pydantic validators, not caller discipline.
- **Bare mappings across semantic boundaries.** Raw JSON mappings may exist
  in wire decoders/encoders; normalize them into typed values before crossing
  into semantic code. A dataclass that only renames an open bag of fields does
  not establish a domain model.
- **Redundant semantic state.** Do not retain both a fact and a second writable
  representation of a value derivable from it, or add agreement checks in
  place of a single owner and derivation.
- **Composite construction outside its owner.** Consumers use the owner's
  checked factories rather than assembling its authoritative raw fields.
- **Sum types squeezed into records.** Give disjoint states distinct immutable
  variants carrying exactly their applicable payload. Absence, rejection, and
  corruption are different states; flags plus optional fields must not permit
  incoherent combinations.
- **Ad hoc I/O outside the owning seam.** Transport, authentication, discovery,
  and HTTP policy stay with their declared owners. Orchestration consumes typed
  results instead of duplicating their parsing, storage, or resource policy.

## Failure Philosophy: Surface Problems, Don't Absorb Them

This project operates at boundaries between documented protocols and actual
implementations that may diverge from those protocols. In this territory,
silent fallbacks are more dangerous than loud failures. A system that appears
to work but is quietly degraded is harder to fix than one that fails visibly.

### Maturity-Dependent Resilience

Not all code paths deserve the same failure treatment. The policy depends on
how well-understood the interface is:

| Interface maturity | Policy |
|---|---|
| **Unexplored / undocumented** | Fail loudly. No fallbacks. Surface the unexpected behavior immediately so it can be investigated and understood before building further. |
| **Explored but variable** | Fail with clear diagnostics. Retry only for known-transient causes (network, rate limits). Do not retry structural failures. |
| **Well-understood, known-fragile** | Resilient with bounded retries and logging. The failure mode is documented and the recovery path is verified. |

Code paths move through these tiers as understanding deepens. A fallback is
earned by understanding — not assumed by default.

### Never Mask Capability Failures

If a system requires a capability (model selection, file access, terminal
execution) and that capability silently fails, the system is broken — not
resilient. Silent degradation builds a foundation of false assumptions that
become expensive to unwind later.

When a required capability is unavailable:
- Raise or return an error. Do not substitute a default.
- Log what was attempted, what was expected, and what actually happened.
- Let the caller decide whether to proceed, not the callee.

### Distinguish Structural Failures from Transient Failures

Transient failures (network timeouts, rate limits, brief unavailability) are
retried with capped backoff. These are expected and the recovery path is
well-understood.

Structural failures (unexpected response shapes, missing capabilities,
protocol mismatches) are not retried. They indicate a broken assumption
that needs investigation, not repetition.

## Root Cause Resolution

When something fails:
1. Find the true root cause. Do not fix symptoms.
2. If the root cause is in an external system outside our control, document
   the finding and find the actual working path. Do not build a workaround
   that masks the gap.
3. If a workaround is genuinely necessary, it must be explicitly marked
   with a comment explaining what it works around and under what conditions
   it should be revisited.

## Error Handling

- **Fail fast on unexpected response shapes.** When extracting data from
  external responses, do not provide default values that allow silent
  continuation. If the expected structure is not present, raise with a
  message describing what was expected vs. what was received.

- **Log evidence before raising.** Before raising, log at DEBUG level the
  actual response shape so the developer has diagnostic information without
  needing to reproduce the failure.

- **No silent exception swallowing.** Every `except` block must either
  re-raise, log at WARNING or ERROR, or have an explicit comment explaining
  why the exception is expected and safe to ignore.

## Logging

- Use the `logging` module. No `print()` in source files under `src/`.
- Use lazy formatting: `logger.debug("value: %s", val)`, not f-strings.
- Do not log secrets, tokens, or user-identifying information.

| Level | Use for |
|---|---|
| `DEBUG` | Message content, response shapes, protocol-level detail |
| `INFO` | Lifecycle events, configuration changes, startup/shutdown |
| `WARNING` | Capability gaps, unexpected-but-handled conditions |
| `ERROR` | Connection failures, unexpected crashes, protocol violations |

## LLM Response Integrity

Never truncate, slice, or summarize LLM responses in logs, stored results,
or experiment output. Every response must be preserved verbatim and in full.

This project is in an exploratory phase where every response is potential
evidence for understanding ACP behavior, debugging protocol issues, or
performing later analysis (token throughput, content verification, CoT
detection). Truncated data cannot be recovered — the cost of re-running an
experiment to get the data you threw away is always higher than the cost of
storing a few extra kilobytes.

Specifically:
- **Experiment JSON output** must store the complete response text, not a
  preview or truncation.
- **Log files** must include verbatim request and response content.
- **Proxy logging** may summarize at INFO level for readability, but must
  log the full content at DEBUG level.

Log bloat is not a concern at this stage. Disk is cheap; lost evidence is
expensive.

## Testing

- Non-trivial changes require appropriate verification. Identify construction
  guarantees, existing behavioral coverage, and residual uncertainty before
  adding permanent tests. Ordinary documentation changes do not need tests.
- Defect fixes require a reproducer that fails before the fix and passes after.
  An existing test or temporary probe can supply that development evidence.
- Integration tests should cover real protocol interactions where feasible.
- Tests verify behavior, not implementation details.
- No hardcoded paths, user IDs, or environment-specific values in tests.
- Prefer property-based tests for meaningful laws and boundaries.
- Minimize mocks; use them only at genuine I/O boundaries. Exercise actual
  implementations and assert outcomes rather than internal call sequences.
- Do not mask infrastructure or upstream failures with test retries, weaker
  assertions, or repair heuristics. Fix or surface the actual cause.

### Test retention

Correctness begins with owner algorithms, explicit immutable inputs, strong
types, and construction-enforced invariants. Tests address residual behavioral
uncertainty. Name the actual type or algorithm and invariant when relying on a
construction guarantee; an unsupported claim of correctness is not evidence.

- Retain tests with distinct, materially plausible defect-detection value beyond
  construction, types, and stronger existing coverage. Independent laws, real
  admission boundaries, and still-possible regressions justify retention.
- Review tests added or affected by a change and remove low-value scaffolding
  before completion. Do not retain constructor-argument echoes, incidental
  field rosters, constant mirrors, or duplicated implementation logic merely
  because they were written during development.
- Put temporary reproducers under ignored `tmp/test-probes/<task>/`, outside
  normal collection, and run them explicitly. Promote them only after a
  deliberate retention decision; do not move maintained tests to scratch to
  bypass review.
- Use actual typed construction in tests. Inspect constructors, protocols, and
  consumers before edits; supply complete valid resources from the outset.
  Run the change-relative validator after bounded maintained-test edits.
- Retirement must preserve required behavior and meaningful observations.
  Coverage overlap alone does not prove redundancy; do not weaken assertions,
  hide failures, or narrow the checkout gate to obtain a pass.
- Explain retention or consolidation by behavioral family in the change
  description. No separate per-test ledger is required.

### No Skips

Tests must not use `skip`, `skipif`, `importorskip`, or `pytest.skip()` to bypass
an obligation. `tests/conftest.py` makes collection-time and runtime skips fail
an otherwise successful pytest run, including focused runs. A test that cannot run is
asserting that the environment is misconfigured — that assertion should
surface as a failure, not be silently suppressed.

Skipping masks environment problems. A test suite that passes with half its
tests skipped looks green but proves nothing. The painful short-term path
(failing loudly when the environment is wrong) is the correct long-term path
(environments get fixed and stay fixed because breakage is visible).

When a test depends on an external resource (binary, service, credential):
- The test should fail with a clear message explaining what is missing.
- Use a fixture that asserts the resource exists and returns it.
- Do not provide a "success path" that avoids the dependency.

## Concurrency and Resources

- Use `async/await` for runtime orchestration; no blocking I/O in the event loop.
- Keep child processes, tasks, queues, callbacks, and shutdown settlement with
  their declared owners. Async code can interleave at every `await`; do not
  assume a single event loop makes a multi-step transition atomic.
- Preserve bounded resource lifetimes, ordered terminal signaling, and explicit
  failure reporting required by the relevant Meadow Bridge ADRs.
