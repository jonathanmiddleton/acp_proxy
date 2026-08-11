# ADR Index

This index tracks all Architecture Decision Records in this repository.
See [GUIDE.md](GUIDE.md) for how and when to write ADRs.

## Proxy architecture and protocol bridging

- [ADR-001: Route OpenCode Through ACP Proxy to copilot-language-server](001-acp-proxy-architecture.md) — legacy mode only
- [ADR-003: System Prompt Injection as Primary Control Surface](003-system-prompt-injection.md) — legacy mode only
- [ADR-004: Extract Only the Last User Message for ACP Sessions](004-last-user-message-extraction.md) — legacy mode only
- [ADR-007: The ACP Server Owns Tools — Do Not Inject or Override](007-tool-ownership.md) — amended for direct callbacks
- [ADR-011: Context Injection — Proxy Responsibilities and Consumer Boundary](011-context-injection-boundary.md) — legacy mode only
- [ADR-012: Authenticated Meadow-Direct Consumer Protocol](012-meadow-direct-consumer-protocol.md) — exact catalog-session acknowledgement before readiness
- [ADR-014: Correlate Direct Session State Without Retaining Unsupported State](014-correlate-direct-session-state.md) — bounded state-update correlation without unused retained state

## Session and conversation management

- [ADR-002: Session-per-Conversation via First-Message Hash](002-session-per-conversation.md) — legacy mode only
- [ADR-009: Intra-Process Session Scaling](009-intra-process-session-scaling.md) — partially superseded; empirical evidence retained

## Binary lifecycle and deployment

- [ADR-006: Version-Bounded JetBrains Binary Discovery](006-binary-discovery.md) — historical path policy; version probing and deterministic selection retained
- [ADR-013: Version-Reported Language-Server Admission](013-version-reported-binary-admission.md) — reported server version is compatibility evidence; IDE releases are enumeration details
- [ADR-008: Proxy as Substrate — Installable Command, cwd as Workspace](008-proxy-as-substrate.md) — amended by direct startup policy

## Testing and quality

- [ADR-005: Fail-Loud Testing — No Skips](005-fail-loud-testing.md)
