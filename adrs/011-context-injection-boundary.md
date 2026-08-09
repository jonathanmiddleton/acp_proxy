# ADR-011: Context Injection — Proxy Responsibilities and Consumer Boundary

**Status:** Accepted for `opencode-legacy`; superseded for direct prompt ownership by [ADR-012](012-meadow-direct-consumer-protocol.md)
**Date:** 2026-04-07  
**Related ADRs:** [ADR-003](003-system-prompt-injection.md), [ADR-004](004-last-user-message-extraction.md), [ADR-012](012-meadow-direct-consumer-protocol.md)

## Context

The documented two-agent-runtime collision means that through the legacy ACP path,
the model receives zero project context — no `AGENTS.md`, no coding
standards, no repo structure information. The Copilot plugin injects
`AGENTS.md` automatically, but the LSP in ACP mode does not.

OpenCode's system prompt is stripped (ADR-004) for two reasons beyond
collision avoidance:
- **It contains invalid information.** OpenCode's prompt describes tools,
  capabilities, and behavioral expectations that do not apply when the
  model is operating through the ACP agent runtime. Injecting it would
  actively mislead the model about its environment.
- **It is large (~7K+ tokens).** This is appropriate for models with
  expansive context windows via direct API access, but through Copilot's
  backend — where the provider injects its own system prompt, safety
  policies, and tool definitions — the additional overhead consumes context
  that should be available for project-specific information.

With the OpenCode prompt stripped, the model has no awareness of what it's
working on unless context is injected through the proxy.

Target environment testing with gpt-4.1 (2026-04-07) confirmed the gap
empirically:
- Through the Copilot plugin: agent reads `AGENTS.md`, uses project context
  to build working scripts.
- Through the proxy: agent has no project context, produces generic output.

The same testing revealed a behavioral model for gpt-4.1 that informs
what the proxy should and should not attempt:
- The model **uses** available context to fulfill tasks.
- The model **does not treat guidance as normative** — it reads instructions
  as descriptive, not prescriptive.
- The model **does not self-initiate auxiliary tasks** — no test runs, no
  journal updates, no error recovery without explicit prompting.
- The model **does not generalize instructions to self** — agrees that
  "everyone should maintain the journal" but does not include itself in
  "everyone."

These observations establish a clear boundary between what the proxy can
achieve (making context available) and what requires consumer-level
orchestration (enforcing compliance with directives).

## Decision

### What the proxy owns

The proxy is responsible for **making project context available** to the
model. It does this by composing a system prompt from two sources:

1. **Explicit system prompt** (`--system-prompt` flag) — a file provided by
   the caller. When present, this comes first in the composed prompt. The
   consumer (Meadow, a script, a human) owns this content entirely.

2. **Workspace context files** — markdown files discovered in the workspace
   `cwd`. The proxy scans for a configurable list of files and appends
   their contents to the system prompt.

   Default scan list: `["AGENTS.md", "CLAUDE.md", "COPILOT-INSTRUCTIONS.md"]`

   Configurable via `~/.acp_proxy/config.json`:
   ```json
   {
     "context_files": ["AGENTS.md", "CLAUDE.md", "CODING_STANDARDS.md"]
   }
   ```

   The order in the list determines injection order. Files that don't exist
   are silently skipped — a generous default list works across different
   repos without requiring per-repo configuration.

Composition order: explicit `--system-prompt` first, then context files in
list order. The explicit prompt takes positional priority (models weight
earlier content more heavily).

The proxy also reports **estimated token usage** at INFO level:
- Per-prompt estimates (system prompt + user message)
- Per-session accumulation
- Clear labeling that these are estimates (tokenization is approximated at
  ~4 characters per token, and Copilot's backend injects additional context
  that we cannot observe or measure)

### What the proxy does NOT own

The proxy does not attempt to enforce behavioral compliance. Specifically:

**Directive compliance** — ensuring the model runs tests, recovers from
errors, follows coding standards, or doesn't declare premature success.
These require:
- Observation of agent actions and outcomes
- Feedback loops (detect failure → prompt retry)
- Verification gates (did tests pass before declaring done?)

This is orchestration logic. It belongs in the consumer (Meadow's agent
framework), not in a transport proxy.

**Reflective behaviors** — maintaining `journal.md`, distilling
observations, recording what worked and what didn't. The model does not
self-initiate reflection (empirically confirmed). Triggering reflection
requires an external system-level tick, which is a scheduler concern, not
a proxy concern.

**Curation and skill extraction** — compressing journal entries, extracting
reusable heuristics, maintaining ADRs. This requires a separate observer
with its own context and objectives. It operates on a different cadence
(periodic, not per-request) and may need different model capabilities.

### Boundary summary

| Concern | Owner | Mechanism |
|---------|-------|-----------|
| Project context availability | Proxy | System prompt injection from workspace files |
| Explicit instructions | Consumer | `--system-prompt` flag content |
| Token usage visibility | Proxy | Estimated usage logged per prompt and session |
| Directive compliance | Consumer | Orchestration, feedback loops, verification |
| Reflection / journaling | Consumer + system scheduler | External trigger, not proxy-initiated |
| Curation / skill extraction | System-level observer | Periodic process, separate from request path |

## Consequences

- **The proxy is a context delivery mechanism, not a behavior enforcement
  system.** It makes information available; it does not verify that the
  model acts on it. Consumers that need compliance must implement their
  own observation and feedback loops.
- **Context file discovery is workspace-scoped.** The proxy reads files
  from `cwd` only. It does not traverse parent directories, resolve
  monorepo structures, or fetch remote content.
- **Token estimates are inherently approximate.** The proxy cannot know
  what Copilot's backend injects (its own system prompt, safety policies,
  tool definitions). Reported estimates cover only what the proxy sends.
  Actual token consumption will be higher.
- **The default scan list covers common conventions.** `AGENTS.md`,
  `CLAUDE.md`, and `COPILOT-INSTRUCTIONS.md` are scanned by default.
  Files that don't exist are silently skipped, so a generous default is
  safe — repos that use different conventions simply have no matching
  files and get no injection. Users can customize via config.

## Rejected Alternatives

1. **Proxy enforces directive compliance via prompt engineering.** Adding
   "you must run tests" to the system prompt does not work — gpt-4.1 treats
   such instructions as descriptive, not prescriptive. Enforcement requires
   observing outcomes, which is an orchestration concern.

2. **Proxy triggers periodic reflection.** A transport proxy has no concept
   of task lifecycle or session duration. It processes requests; it does not
   schedule background work. Reflection triggers belong in a system-level
   scheduler.

3. **Proxy scans for all markdown files in the workspace.** Injecting
   everything risks context window exhaustion and includes irrelevant
   content (READMEs, changelogs, documentation). A curated list is safer.

4. **Auto-injection replaces `--system-prompt` entirely.** The explicit
   prompt is the consumer's control surface. Removing it would force all
   instructions into markdown files in the workspace, which is less
   flexible for programmatic callers like Meadow.
