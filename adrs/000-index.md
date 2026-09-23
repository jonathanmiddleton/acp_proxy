# ADR Index

This index tracks all Architecture Decision Records in this repository.
See [GUIDE.md](GUIDE.md) for how and when to write ADRs.

## Bridge architecture and protocol bridging

- [ADR-001: Route OpenCode Through ACP Proxy to copilot-language-server](001-acp-proxy-architecture.md) — historical; adapter removed by ADR-018
- [ADR-003: System Prompt Injection as Primary Control Surface](003-system-prompt-injection.md) — historical; adapter removed by ADR-018
- [ADR-004: Extract Only the Last User Message for ACP Sessions](004-last-user-message-extraction.md) — historical; adapter removed by ADR-018
- [ADR-007: The ACP Server Owns Tools — Do Not Inject or Override](007-tool-ownership.md) — amended for direct callbacks
- [ADR-011: Context Injection — Proxy Responsibilities and Consumer Boundary](011-context-injection-boundary.md) — historical; adapter removed by ADR-018
- [ADR-012: Authenticated Meadow-Direct Consumer Protocol](012-meadow-direct-consumer-protocol.md) — exact catalog-session acknowledgement before readiness
- [ADR-014: Correlate Direct Session State Without Retaining Unsupported State](014-correlate-direct-session-state.md) — bounded state-update correlation without unused retained state
- [ADR-015: Order Direct Model Binding at RPC Settlement](015-order-direct-model-binding-transitions.md) — admit exact prior/target state during unresolved selection; enforce target at the ordered response boundary
- [ADR-016: Opt-In Raw ACP Event Capture](016-opt-in-raw-acp-event-capture.md) — separate ordered diagnostic artifacts with explicit lifetime and failure reporting

## Session and conversation management

- [ADR-002: Session-per-Conversation via First-Message Hash](002-session-per-conversation.md) — historical; adapter removed by ADR-018
- [ADR-009: Intra-Process Session Scaling](009-intra-process-session-scaling.md) — partially superseded; empirical evidence retained

## Binary lifecycle and deployment

- [ADR-006: Version-Bounded JetBrains Binary Discovery](006-binary-discovery.md) — historical path policy; version probing and deterministic selection retained
- [ADR-013: Version-Reported Language-Server Admission](013-version-reported-binary-admission.md) — reported server version is compatibility evidence; IDE releases are enumeration details
- [ADR-008: Proxy as Substrate — Installable Command, cwd as Workspace](008-proxy-as-substrate.md) — amended by direct startup policy

## Testing and quality

- [ADR-005: Fail-Loud Testing — No Skips](005-fail-loud-testing.md)
- [ADR-017: Change-Relative Typing and a Complete Checkout Gate](017-change-relative-typing-and-checkout-gate.md)

- [ADR-018: Remove the Deprecated OpenAI-Compatible Adapter](018-remove-openai-compatible-adapter.md) — direct ACP remains the sole consumer contract

- [ADR-019: Meadow Bridge Product Identity](019-meadow-bridge-product-identity.md) — coordinated breaking product/package/command rename; direct wire contract retained
