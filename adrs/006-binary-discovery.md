# ADR-006: Version-Bounded JetBrains Binary Discovery

**Status:** Partially superseded by ADR-013; version probing and deterministic
selection remain accepted
**Date:** 2026-04-03  

> **Supersession note (2026-08-10):** ADR-013 removes the IDE product/release,
> home-directory, plugin-layout, and architecture allowlist from compatibility
> admission. This ADR remains the historical record for the global version
> floor, bounded probe, and deterministic selection policy.

## Context

The copilot-language-server binary is bundled with JetBrains IDE plugins, not
installed independently. Multiple JetBrains IDEs can run simultaneously on the
same machine (IntelliJ, PyCharm, WebStorm, etc.), each with its own copy of
the binary at a different version. The target environment confirmed this —
multiple `copilot-language-server` processes were running from different IDE
installations.

### Empirical evidence

**Wrong binary selection caused hangs on target.** The initial `find_binary()`
grabbed the first matching process from `ps` output. On the target machine,
this was an incompatible version from a different IDE (not IntelliJ 2025.3).
Integration tests hung because the binary's ACP behavior differed from what
the proxy expected.

**Case sensitivity mismatch.** The discovery pattern initially used
`IntellijIdea` (lowercase j) but the actual macOS filesystem directory is
`IntelliJIdea` (capital J). macOS HFS+ is case-insensitive, so glob-based
file search worked, but the regex for `ps`-based validation was
case-sensitive and rejected the correct binary.

**Binary path is user-dependent.** The path includes the OS username (SOEID on
the target environment). Hardcoding any user-specific path component would
break on every other machine.

**IDE release paths do not prove language-server compatibility.** A supported
IntelliJ IDEA 2025.3 installation was observed with language server 1.457.1,
which returned JSON-RPC method-not-found for `session/set_config_option`.
Language server 1.523.3 exposed the required method and exact complete
`configOptions` acknowledgement used by Meadow direct mode.

The official Darwin ARM64 language server 1.518.3 subsequently exposed the
same exact `session/set_config_option` acknowledgement: the complete four-option
collection reported the requested `gpt-5.3-codex` current value. Two bounded
same-session prompt turns then completed with `end_turn` and preserved a nonce.
This is sufficient to lower the version admission floor without weakening the
consumer-specific readiness proof.

## Decision

`discovery.py` is the single source of truth for binary resolution. It:

1. **Validates three path properties for auto-discovery.** A candidate must:
   - Be under the current user's home directory.
   - Contain one supported IDE/version directory as a path component:
     `IntelliJIdea2025.3`, `IntelliJIdea2026.1`, `PyCharm2025.3`, or
     `PyCharm2026.1`.
   - Have the correct binary filename (`copilot-language-server` on Unix,
     `copilot-language-server.exe` on Windows).

   No assumptions are made about the intermediate directory structure
   between the home directory and the IDE directory. Deployment layouts
   vary across environments — the Windows target uses
   `.../copilot-agent/bin/copilot-language-server` (no `native/` or
   architecture directory), while macOS uses
   `.../copilot-agent/native/darwin-arm64/copilot-language-server`.

2. **Admits a strict language-server version globally.** Every selected
   executable, including explicit `--binary` and programmatic `run()` inputs,
   must exist, be executable, emit exactly `MAJOR.MINOR.PATCH` for `--version`,
   and report at least 1.518.3. The floor applies to both Meadow direct and the
   deprecated OpenCode legacy mode. Booleans, numbers, decorated/malformed
   strings, failed or excessive output, and below-floor versions fail closed.
   The probe uses a credential-free environment, bounded output retention, and
   a timeout before the executable can be admitted.
3. **Collects all auto-discovery candidates before choosing.** On Unix, scans
   `ps` output.
   On Windows, uses PowerShell (`Get-Process`) with a wmic fallback. Each
   candidate's full path is validated against the three-property check.
   Incompatible binaries (other IDEs, other versions, other users) are
   rejected with a warning log.
   Filesystem candidates from the expected JetBrains plugin directories are
   combined with process candidates. The highest admitted semantic version is
   selected, with lexicographically smallest canonical path as a stable
   tie-break. Process order, filesystem order, and source precedence cannot
   select an older candidate.
4. **Explicit `--binary` selection.** The CLI accepts an explicit path for
   environments where auto-discovery fails. It bypasses the JetBrains path and
   candidate-selection rules only; executable and minimum-version admission
   remain mandatory.

Both `__main__.py` and `test_integration.py` import from `discovery.py`. No
duplicated discovery logic.

## Rationale

- **Version specificity prevents silent incompatibility.** ACP behavior can
  differ between binary versions. The proxy was developed and tested against
  specific JetBrains plugin families and versions. Accepting arbitrary
  versions risks silent behavioral differences.
- **Single source of truth.** Before this change, `__main__.py` and
  `test_integration.py` had separate discovery logic that diverged. Extracting
  to a shared module eliminated the inconsistency.
- **Process and filesystem discovery provide one candidate set.** A running
  older process cannot mask a newer supported plugin binary on disk.
- **Rejecting incompatible binaries is the correct failure mode.** Finding *a*
  binary is not sufficient — it must be the *right* binary. The target
  environment proved this when the wrong binary was selected and the proxy
  hung.

## Consequences

- **Only an enumerated IDE/version set is supported.** A new JetBrains product
  or release fails discovery until it is explicitly added and tested.
- **Filesystem fallback assumes a specific directory structure.** The
  `find_binary_from_jetbrains()` path uses the full expected layout
  (including `native/{arch}/`). If the actual layout differs (as on the
  Windows target), the filesystem fallback won't find the binary. Process
  discovery will still work since it uses the relaxed three-property check.
  The `--binary` flag is the escape hatch.
- **An IDE need not be running for filesystem discovery.** The plugin must be
  installed in an expected layout; otherwise an explicit version-admitted path
  is required.
- **A path-shaped binary is not sufficient.** Startup and integration tests
  fail before child use when the selected executable cannot prove the global
  1.518.3 floor. Meadow direct adds a behavioral `session/set_config_option`
  proof before HTTP readiness because a version string alone is not capability
  acknowledgement.

## Revision History

| Date | Change |
|------|--------|
| 2026-04-07 | Relaxed path matching from exact full-path regex to three-property check (home dir, IDE dir component, binary name). Added Windows process discovery via PowerShell + wmic fallback. Confirmed Windows target uses a different directory layout (`bin/` instead of `native/{arch}/`). |
| 2026-08-09 | Aligned the decision with production discovery for IntelliJ IDEA and PyCharm 2025.3/2026.1 paths. Arbitrary versions and products remain fail-loud. |
| 2026-08-09 | Established the global 1.523.3 language-server floor, strict credential-free bounded version probing for all entry points, and deterministic highest-version selection across the combined process/filesystem candidate set. Explicit paths bypass discovery only. |
| 2026-08-10 | Lowered the global floor to 1.518.3 after exact model acknowledgement and bounded two-turn continuity were observed on that release. Version admission remains an early filter; direct readiness still requires the same exact per-session capability proof. |
