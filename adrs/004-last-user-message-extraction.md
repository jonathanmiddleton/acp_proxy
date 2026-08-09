# ADR-004: Extract Only the Last User Message for ACP Sessions

**Status:** Accepted for `opencode-legacy`; superseded for direct requests by [ADR-012](012-meadow-direct-consumer-protocol.md)
**Date:** 2026-04-03  

## Context

OpenCode follows the standard OpenAI chat completions pattern: every request
includes the **full conversation history** as a messages array. This is correct
for stateless APIs where the server has no memory — the client must replay
everything.

ACP sessions are stateful. The copilot-language-server accumulates context
across `session/prompt` calls within the same session. Each prompt adds a
turn to the session's internal history.

When the proxy forwarded the full message array (flattened to text) into a
reused ACP session, the model received every prior message twice: once from
the ACP session's accumulated state, and once from the replayed history in
the current prompt.

### Empirical evidence

**Duplication confirmed.** Turn 1: "My name is Alice." Turn 2: OpenCode
replayed [Alice message, assistant reply, new question]. The ACP session
already had the Alice exchange. Model responded: "you've told me your name
**twice**."

**The duplication compounded.** Each subsequent turn added another full copy of
all prior messages. By turn N, the model saw approximately N copies of the
early messages. This wasted context window, confused the model, and caused
stale context to dominate recent instructions.

**Stale context caused session poisoning.** When an error occurred mid-
conversation, the duplicated stale context dominated the model's attention.
The model acted on old context from prior turns rather than the current
instruction, creating a feedback loop where the session became unusable.

## Decision

The proxy extracts only the **last user message** from OpenCode's messages
array and sends it as the sole content in the `session/prompt` call. All
prior messages (system prompts, earlier user turns, assistant responses) are
discarded.

Two static helpers in `client.py`:
- `extract_last_user_message(messages)` — returns the content of the final
  user-role message (the new turn).
- `extract_first_user_message(messages)` — returns the content of the first
  user-role message (used as the conversation anchor for session identification;
  see ADR-002).

## Rationale

- **ACP sessions already have the history.** The session accumulates all prior
  turns. Replaying them is redundant and harmful.
- **Eliminates the duplication root cause.** Rather than trying to diff the
  replayed history against what the session has seen, which would be complex
  and fragile, we simply send only what's new.
- **OpenCode's system prompt is stripped for three reasons:**
  1. **Collision.** It describes OpenCode's own tools and capabilities, which
     conflict with the ACP server's tool definitions (see ADR-003 and ADR-007).
  2. **Invalid information.** The prompt describes an environment (OpenCode's
     native tool surface, direct API access, specific behavioral contracts)
     that does not exist when the model operates through the ACP agent
     runtime. Injecting it would actively mislead the model.
  3. **Size.** At ~7K+ tokens, it consumes context window that is better
     used for project-specific information. Through Copilot's backend —
     which injects its own system prompt, safety policies, and tool
     definitions — the overhead is compounded.

## Consequences

- **OpenCode's system prompt is invisible to the model.** Any instructions
  OpenCode embeds in system messages (mode switches, tool descriptions, coding
  standards) are discarded. The injected system prompt (ADR-003) must cover
  all necessary guidance.
- **`<system-reminder>` tags are lost.** OpenCode communicates mode (build vs
  plan) and context updates via `<system-reminder>` tags embedded in message
  content. Since only the last user message is forwarded, these tags from
  earlier messages are stripped. Mode control must be handled through the
  system prompt.
- **Assistant turn context is implicit.** The model doesn't receive explicit
  assistant messages from OpenCode — it relies on its own ACP session memory.
  If the ACP session is lost or recreated, the model loses all prior context.
- **Robust against OpenCode changes.** The proxy doesn't depend on the
  structure or content of OpenCode's system prompt. Changes to OpenCode's
  prompt engineering don't affect the proxy.
