# ADR-003: System Prompt Injection as Primary Control Surface

**Status:** Accepted for `opencode-legacy`; superseded for direct prompt ownership by [ADR-012](012-meadow-direct-consumer-protocol.md)
**Date:** 2026-04-03  

## Context

The copilot-language-server in ACP mode runs a full agent loop: it provides
its own tool definitions to the model, executes tools internally, and wraps
requests with its own system prompt. The proxy has no direct access to the
model — all communication is mediated by the ACP server.

When OpenCode connects through the proxy, two agent runtimes collide:
- OpenCode sends a ~15K char system prompt describing its tools (bash, read,
  edit, write, etc.) and expects the model to return tool calls as text.
- The ACP server provides its own tools as structured function definitions
  (bash, view, create, edit, web_fetch, report_intent, skill, sql, grep, glob,
  task, plus GitHub MCP server tools).
- The model sees both but can only call the ACP server's tools.

This caused mode desync (OpenCode build mode vs LSP plan mode), tool name
mismatch, custom tools being invisible, and file operations failing due to
differing tool semantics.

### Empirical evidence

**System prompt injection overrides ACP tool reporting.** Tested with a prompt
containing "You do not have any tools available." The model reported: "I have
no tools available." Without the injection (control test), the model listed the
full ACP tool surface including GitHub MCP server tools.

**The injection point is the first turn of an ACP session.** The proxy sends
the system prompt as the first `session/prompt` before any user content. The
ACP server's own tool definitions still exist but the model's behavior is
shaped by the injected instructions.

**OpenCode's system prompt is stripped.** The proxy extracts only the last user
message (see ADR-004), discarding OpenCode's system prompt and tool
definitions. The injected system prompt replaces them.

## Decision

The proxy accepts a `--system-prompt PATH` CLI flag. The file contents are
injected as the first turn in every new ACP session. This is the primary
mechanism for controlling model behavior, replacing both OpenCode's system
prompt and the ACP server's default instructions.

The system prompt can:
- Override or suppress the ACP server's tool definitions
- Set the agent mode (build, plan, ask)
- Provide workspace context (coding standards, architecture)
- Shape output format and tool-calling conventions

## Rationale

- **Only verified legacy prompt control surface.** On the older language-server
  binary used for the original experiment, `session/set_config_option`
  returned "Method not found." The currently admitted 1.523.3+ surface uses
  that method for exact model configuration, but supplies no evidence that it
  controls tool visibility, system prompts, or general model behavior.
  First-turn injection remains the only empirically verified legacy mechanism
  for those prompt-level effects.
- **Decouples proxy from OpenCode's prompt.** OpenCode's elaborate system
  prompt (describing its own tools, modes, coding standards) is designed for
  direct model access. Through the proxy, it creates conflicts. Stripping it
  and replacing with a purpose-built prompt eliminates the collision.
- **Per-deployment customization.** Different environments (dev vs target) can
  use different system prompts without code changes. The prompt file is
  external configuration.

## Consequences

- **System prompt design is critical.** The injected prompt is the only
  guidance the model receives. A poorly designed prompt degrades all
  conversations. This is a high-leverage configuration surface.
- **ACP server tools still exist.** The model is instructed not to use them,
  but the ACP server still provides them. A sufficiently complex or confused
  model response may still trigger ACP tool execution. The proxy handles agent
  callbacks (permissions, fs, terminal) defensively for this reason.
- **Mode control must be in the prompt.** OpenCode communicates mode via
  `<system-reminder>` tags in message content. Since only the last user
  message is forwarded, these tags are stripped. Mode must be statically set
  in the system prompt or dynamically injected per-session (not yet
  implemented).
- **No structured output control.** The ACP protocol has no equivalent of
  OpenAI's `response_format` or Meadow's `format.schema`. Output structure
  must be requested via prose in the system prompt.
