# ADR-002: Session-per-Conversation via First-Message Hash

**Status:** Accepted for `opencode-legacy`; superseded for direct identity by [ADR-012](012-meadow-direct-consumer-protocol.md)
**Date:** 2026-04-03  

## Context

ACP sessions are stateful — the copilot-language-server maintains full
conversation history within a session. Each `session/prompt` adds a turn;
context accumulates. New sessions are isolated.

OpenCode follows the standard OpenAI pattern: it sends the **full message
history** with every request (messages 1,2,3... then 1,2,3,4...). It provides
no session identifier, thread ID, or conversation key in the request body.
This was confirmed by logging all extra fields in incoming HTTP requests —
only standard OpenAI fields are present.

The proxy must map OpenCode's stateless full-replay requests to ACP's stateful
sessions. Two problems had to be solved:

1. **Which ACP session does a request belong to?** Multiple conversations can
   be active simultaneously (e.g., a coding conversation and a title
   generator).
2. **What content to send?** If the full replayed history is sent to a session
   that already has prior turns, the model sees everything twice.

### Empirical evidence

**Context duplication confirmed.** Turn 1: "My name is Alice." Turn 2: full
history replayed into the same session. Model responded: "you've told me your
name **twice**." Every request after the first doubled the context.

**Concurrent session collision confirmed.** OpenCode sends parallel requests
for the conversation and a title generator. When both hit the same ACP session,
the server cancelled one — producing "Operation cancelled by user" errors.

**Session state isolation confirmed.** BANANA7742 test: told a session a secret
code, retrieved it from the same session (success), failed to retrieve it from
a new session (correctly isolated).

## Decision

Sessions are keyed by `(model_id, sha256(first_user_message))`.

- OpenCode replays the full message history with every request, so the first
  user message is **stable** across all turns of a conversation.
- Different conversations have different first messages, so they get different
  sessions.
- The title generator sends a different first message (a summarization
  request), so it gets its own session — no collision with the main
  conversation.
- Model ID is included in the key because different models require separate
  ACP sessions (model is set at session creation via `session/set_model`).

Only the **last user message** is sent to the ACP session (see ADR-004),
avoiding context duplication.

### Single-message collision avoidance

When a request contains exactly one message (`len(messages) == 1`) and the
hash key already maps to a session that has received at least one prompt,
the proxy treats it as a **new conversation** and creates a fresh session,
evicting the old key mapping.

Multi-message requests (`len(messages) > 1`) always reuse the existing
session — they are continuations with replayed history and the first message
serves as a stable anchor.

This prevents scripts, repeated agent invocations, and any caller sending
the same initial prompt from colliding into a single ACP session.

## Rationale

- **No session identifier available.** OpenCode sends no thread/session ID.
  The first user message is the only stable conversation anchor in the
  request payload.
- **Single-message requests are a reliable new-conversation signal.**
  OpenCode always replays the full history for continuations, so
  `len(messages) > 1` reliably indicates a continuation. A single-message
  request hitting an existing active session is a new conversation, not a
  follow-up.
- **Concurrent safety.** Title generator and conversation are naturally
  separated because their first messages differ. This eliminated the
  "Operation cancelled" errors without any special-case logic.

## Consequences

- **Session accumulation.** Old sessions are never explicitly closed (the ACP
  spec has no `session/close` method). They accumulate in the language server's
  internal state. The server may have internal TTL — this needs empirical
  testing. If not, periodic process restart is the cleanup strategy.
- **Evicted sessions are orphaned.** When a single-message collision creates
  a new session, the previous session for that key is no longer reachable
  through the proxy's routing table. It remains in the language server's
  memory but receives no further prompts. This contributes to session
  accumulation.
- **Session creation latency.** Each new conversation incurs ~2s for ACP
  session creation. This is a one-time cost per conversation, not per turn.

## Revision History

| Date       | Change                                                                                                                                                                              |
|------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 2026-04-07 | Use full SHA-256 hash (removed 16-char truncation). Added single-message collision avoidance: `len(messages)==1` with an active session creates a fresh session instead of reusing. |
