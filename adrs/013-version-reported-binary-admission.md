# ADR-013: Version-Reported Language-Server Admission

**Status:** Accepted  
**Date:** 2026-08-10  
**Supersedes:** ADR-006 path-based compatibility restrictions

## Context

ADR-006 correctly established strict, bounded `copilot-language-server
--version` probing and deterministic highest-version selection. It also made
IDE product and release directory names part of compatibility admission.

That path policy does not track the executable contract. In a Windows 11 ARM
development VM, a running binary was observed at a PyCharm 2026.2
`win32-arm64` path while reporting language-server version 1.527.5. The primary
Windows target is x64 and the primary macOS development environment is ARM64.
Production discovery only knew a fixed IDE release set and its filesystem
pattern assumed `win32-x64`. Adding each environment, IDE release, or
architecture to those lists would preserve the same failure for the next
plugin layout despite having direct version evidence from the executable.

The earlier ADR already records the inverse evidence: an admitted-looking IDE
path contained language server 1.457.1, which lacked required behavior. IDE
release paths therefore produce both false rejections and false confidence.

## Decision

Compatibility admission is based on the candidate executable's evidence:

1. The path must resolve to an existing executable file.
2. The executable must emit exactly `MAJOR.MINOR.PATCH` for `--version` through
   the credential-free, bounded, timed probe.
3. The reported language-server version must meet the global 1.523.3 floor.
4. When multiple candidates qualify, the highest semantic version wins, with
   canonical path as the stable tie-break.

IDE product, IDE release, user-home location, plugin subdirectory layout, and
bundled architecture are not compatibility evidence and are not admission
conditions.

Auto-discovery still needs bounded places to enumerate candidates:

- Running process queries identify candidates by the platform executable name.
- Filesystem discovery recursively searches below the platform JetBrains data
  root for that executable name instead of constructing product-, release-,
  layout-, or architecture-specific paths.
- Explicit `--binary` continues to bypass enumeration only; it passes through
  the same executable and version admission.

The filename and JetBrains data root identify candidates. They do not establish
compatibility; the reported language-server version does.

Version admission is the shared executable boundary, not a substitute for
consumer-specific capability acknowledgement. In the Windows 11 ARM VM, the
admitted 1.527.5 server initialized and advertised models but returned
JSON-RPC method-not-found for `session/set_config_option`. Meadow direct must
therefore continue to prove that required method behavior before HTTP
readiness, as ADR-012 requires. Legacy admission does not require that direct-
only capability.

## Consequences

- New JetBrains IDE releases, products, plugin layouts, and architectures do
  not require source or test changes when they bundle an admitted server.
- A named running binary outside a JetBrains path may be probed and admitted.
  The probe remains credential-free, output-bounded, and time-bounded before
  any normal child launch can inherit credentials.
- Filesystem discovery may inspect more directories below the JetBrains data
  root. It does not follow directory symlinks and only probes named executable
  candidates.
- Tests create candidates below temporary directories and vary reported server
  versions. They do not encode IDE release paths as compatibility behavior.
- A version-admitted executable may still fail a consumer-specific capability
  proof. That failure remains explicit and is not converted into a path rule or
  a claim that an IDE release determines protocol behavior.
