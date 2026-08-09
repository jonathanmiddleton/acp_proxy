# ADR-007: The ACP Server Owns Tools — Do Not Inject or Override

**Status:** Accepted; direct callback policy amended by [ADR-012](012-meadow-direct-consumer-protocol.md)
**Date:** 2026-04-03  

## Context

The proxy sits between two systems that each want to provide tools to the
model: OpenCode and the copilot-language-server (ACP server). Understanding
which system actually controls tool execution was critical to the proxy's
design.

### Empirical evidence

**The ACP server executes tools internally.** Asked the model (via proxy, no
OpenAI tools in request) to create a file at `/tmp/acp_test_hello.txt`. The
LSP executed the shell command itself — `tool_call_update` notification
returned with stdout and the file appeared on disk. Zero `Agent request:`
lines in the proxy log. No `fs/*` or `terminal/*` callbacks were made to the
proxy. The ACP server ran the full agent loop: tool selection, execution, and
result integration.

**ACP `session/prompt` has no `tools` parameter.** Confirmed by the ACP spec
and two independent open-source implementations (Go: `HEUDavid/acp-openai-proxy`;
Rust: `Dilaz/acp-openai-bridge`). Both drop OpenAI `tools` from requests.
The Rust bridge's `translator.rs` explicitly filters to text-only
`ContentBlock`. `session/prompt` accepts `ContentBlock[]` (text, resource,
image) — no structured tool definitions.

**Three different tool surfaces depending on the client path.** Same model
(GPT-4.1), same prompt, three paths:

| Client path | Tools reported |
|---|---|
| OpenCode → native Copilot (OAuth) | OpenCode's tools: bash, read, edit, write, glob, grep, task, etc. + custom hello_world |
| OpenCode → ACP proxy → LSP | LSP's tools: bash, view, create, edit, web_fetch, skill, sql, grep, glob, task, + github-mcp-server-* |
| JetBrains IDE → LSP | IDE's tools: insert_edit_into_file, create_file, run_in_terminal, read_file, grep_search, etc. |

Each client provides its own tools to the model. When going through ACP, the
LSP's tools replace whatever the client intended.

**OpenCode's custom tool (hello_world) only appeared in the native OAuth
path.** This proves the native connection sends OpenCode's tool definitions
directly to Copilot's API as structured function definitions, bypassing the
LSP. Through the proxy, custom tools are invisible.

**`session/set_config_option` returns "Method not found."** There is no
known ACP method to configure which tools the server exposes.

## Decision

Accept that the ACP server owns tool definitions and execution. Do not
attempt to inject, override, or suppress tools at the protocol level.

Shape model behavior via **system prompt injection** (ADR-003) instead. The
first turn of each session can instruct the model to ignore the ACP server's
tools, use specific tools, or follow particular tool-calling patterns. This
was empirically verified to work — injecting "you have no tools" caused the
model to report no tools, despite the ACP server still providing them.

The proxy handles agent callbacks (permission requests, filesystem operations,
terminal operations) defensively, because the ACP server may execute tools
regardless of system prompt instructions.

## Rationale

- **Protocol constraint.** ACP's design separates the client (prompt provider)
  from the agent (tool provider + executor). This is not a bug or limitation
  to work around — it's the architectural intent. The agent owns tools.
- **No viable override mechanism.** No ACP method exists to configure tool
  visibility. No initialization capability flag was found to disable tools.
  The only empirically verified control surface is the system prompt.
- **System prompt control is sufficient.** For the proxy's purpose (routing
  OpenCode's LLM calls through Copilot), the model needs to follow OpenCode's
  text-based tool-calling format, not the ACP server's structured tools. The
  system prompt achieves this.
- **Defensive callback handling is cheap insurance.** Even with system prompt
  suppression, the model might occasionally trigger ACP tool execution. The
  proxy already handles permission, filesystem, and terminal callbacks. Keeping
  these handlers active costs nothing and prevents hangs.

## Consequences

- **OpenCode's tools work via text, not structured function calling.** Through
  the proxy, tool calls are embedded in the model's text response (following
  OpenCode's system prompt format) and parsed by OpenCode. This is less
  reliable than structured function calling but is the only available
  mechanism.
- **Custom OpenCode tools are invisible to the ACP server.** Tools defined in
  `.opencode/tools/` exist in OpenCode's system prompt but cannot be called
  as structured functions through ACP. They work only if the model follows
  the text-based format.
- **The ACP server may execute its own tools unexpectedly.** If the system
  prompt fails to suppress tool usage (model confusion, prompt length limits),
  the ACP server will execute tools in the workspace directory. The `cwd`
  parameter in `session/new` bounds the filesystem scope, but terminal
  execution is not sandboxed.
- **MCP servers remain a future option.** The ACP spec's `session/new` accepts
  MCP server configuration, which could extend the tool surface. This is
  currently blocked by enterprise policy (MCP not permitted in the target
  environment's Copilot plugin configuration).
