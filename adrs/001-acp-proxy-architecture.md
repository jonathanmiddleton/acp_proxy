# ADR-001: Route OpenCode Through ACP Proxy to copilot-language-server

**Status:** Accepted for `opencode-legacy`; Meadow-direct topology superseded by [ADR-012](012-meadow-direct-consumer-protocol.md)
**Date:** 2026-04-03  

## Context

The target environment is a restricted enterprise setting where the JetBrains
Copilot plugin is the only sanctioned path to GitHub Copilot. Direct OAuth
device authorization is not available. The goal is to make Copilot models
(GPT-4.1, GPT-4o, Claude Sonnet 4, Gemini 2.5 Pro) accessible to Meadow's
multi-agent orchestrator.

Three architectural paths were evaluated:

**Path A — ACP bridge bypasses OpenCode entirely.** Meadow talks directly to an
ACP client that manages the copilot-language-server. This decouples from
OpenCode but exposes weak Copilot-hosted models directly to Meadow's complex
agent protocols without OpenCode's scaffolding (tool execution, conversation
management, structured output).

**Path B — OpenCode on target, Copilot as provider.** Meadow talks to OpenCode
(unchanged), OpenCode routes LLM calls through Copilot's native provider
integration. Requires native OAuth, which is not available in the target
environment.

**Path C — OpenCode with ACP proxy as provider transport.** A Python proxy
sits between OpenCode and the copilot-language-server. It exposes an
OpenAI-compatible HTTP endpoint (`/v1/chat/completions`, `/v1/models`) on
localhost. OpenCode treats it as a standard `openai-compatible` provider via
`opencode.json`. The proxy translates requests to ACP protocol
(NDJSON/JSON-RPC 2.0 over stdio) and manages the language server subprocess.

## Decision

**Path C.** OpenCode runs on the target machine (stock `npm` install, v1.3.13).
The ACP proxy runs as a local Python service. OpenCode's `opencode.json`
configures the proxy as a `copilot` provider with `baseURL:
http://localhost:8765/v1`.

```
Meadow  →  OpenCode  →  ACP Proxy (localhost:8765)  →  copilot-language-server --acp --stdio  →  Copilot backend
```

## Rationale

- **Policy compliance.** All Copilot traffic flows through the sanctioned
  copilot-language-server binary. No direct Copilot API access.
- **OpenCode as scaffolding.** OpenCode handles tool execution, conversation
  management, and structured output. The models on the target environment are
  weaker (Jonathan observed "pretty poor quality output" across all available
  models). OpenCode's agent loop constrains them.
- **Minimal Meadow changes.** Meadow already talks to OpenCode. The proxy is
  invisible to Meadow — it's just another OpenCode provider.
- **Stock OpenCode.** No source builds or custom forks required. The target
  environment's Artifactory serves the stock npm package.

## Consequences

- **Two agent runtimes in the pipeline.** The copilot-language-server runs its
  own agent loop with its own tools (see ADR-007). OpenCode also runs an agent
  loop. These collide — the model sees both tool sets but can only call the
  LSP's tools. This is managed by stripping OpenCode's context and injecting a
  system prompt (see ADR-003, ADR-004).
- **Session overhead.** Each new ACP session takes ~2 seconds to create.
  Acceptable for interactive use but may bottleneck Meadow's multi-agent
  orchestration.
- **No transparent tool relay.** The proxy cannot forward OpenCode's tool
  definitions through ACP (see ADR-007). Tool-calling works via OpenCode's
  text-based tool format in the system prompt, not via structured function
  calling.

## Alternatives Rejected

- **Path A** rejected because weak models facing Meadow's protocols directly
  (without OpenCode's scaffolding) would be unreliable. Too much decoupling
  work for uncertain payoff.
- **Path B (native OAuth)** rejected because the target environment's policy
  does not permit GitHub OAuth device authorization.
- **Direct Copilot backend API** rejected as a policy gray area. The language
  server is the sanctioned intermediary.
