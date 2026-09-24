# ADR-020: Native IDE backend and explicit workspace callbacks

**Status:** Accepted; implementation tracked in the [delivery plan](../docs/native-ide-implementation-plan.md)  
**Date:** 2026-09-23

## Context

The installed Copilot language server exposes an IDE conversation interface
with client tool registration. Native probes demonstrated file creation,
editing, command execution, two-turn continuity, and conversation destruction
without Copilot CLI or MCP servers. ACP's exposed tool path does not provide
the required effects in the supported deployment. Meadow already owns logical
sessions, invocation identity, prompt layers and operation reconciliation.
Those consumer concepts remain useful independently of the backend protocol.

## Decision

Release 0.4 uses only `copilot-language-server --stdio`, with LSP
`Content-Length` framing and the native IDE conversation methods. The Bridge
starts and owns that child; it does not load a binary through FFI. Copilot
continues to own authentication refresh, model access and model invocation.
`GITHUB_COPILOT_ACP_USE_CLI=0` is forced for the child. Native MCP configuration
and catalogs must be empty. No MCP server, CLI fallback, ACP production client,
OpenAI-compatible adapter, or compatibility endpoint is installed.

The HTTP identity becomes `meadow-bridge-direct`, major 2, at `/meadow/v2`.
Health returns `status: ok` and `protocol_major: 2`. Capabilities expose
`bridge_version`, native server identity, advertised model IDs, limits and
explicit execution authority. Only concrete models advertising native agent
scope are admitted; automatic selection aliases cannot preserve exact model
identity. Capabilities no longer expose ACP fields or a consumer
mode. The strict schemas in `direct_protocol.py` define exact wire shapes.
Meadow and workflow-algebra must update their source and lockfile pins in order.

### Sessions and settlement

Creation admits the caller's logical session ID and selected advertised model
without inference. It returns `binding_state: allocated`, a null backend ID,
and the admitted instruction and policy digests. The first prompt creates a
native conversation; later invocations use that same conversation. A native
begin event establishes backend identity even if the turn later fails.
Binding is distinct from successful initial instruction submission. Uncertain
first-turn outcomes are never replayed automatically.

The generation-long operation ledger, content digests, per-session exclusion,
serialized prompt dispatch, deadlines, reconciliation and explicit uncertainty
remain. Native progress, RPC results, callback identities and effect settlement
must agree before reporting completion. A native end event alone is insufficient.
Cancellation settles Bridge-owned command trees and callbacks. Loss revokes
admission before cleanup; it must not publish a successful terminal result while
an owned effect remains unsettled. Retirement destroys a bound conversation;
retiring an allocation invokes no model. Conversation destruction is not a
claim that provider transcripts were deleted.

### Tools and permissions

Separate modules own the immutable permission policy, confirmation decisions,
and workspace effects. Session creation requires the explicit policy
`{"version":1,"mode":"allow_all"}`; its canonical sorted compact JSON SHA-256
is acknowledged. Meadow supplies it at its Bridge adapter boundary. The first
release has no interactive approval path and no role-derived ACL interpreter.
Confirmation and actual invocation both check the admitted session policy.
Copilot may request a confirmation callback; Bridge's returned decision comes
from that explicit policy, not an inferred enterprise approval. Native
confirmation callbacks are optional: observed generic client-tool execution
can omit them even with confirmation metadata and a confirmation request on
the turn. The broad editor-handles-all-confirmation setting also covers native
read/search tools; enabling that larger authority is deferred. Execution-time
policy enforcement never depends on receiving a confirmation callback.

Bridge registers `workspace_create_files`, `workspace_edit_files` and
`workspace_run_command`. Grouped file operations validate path and content
preconditions and report any partial effects. Edits require exact context so
stale model assumptions cannot silently overwrite unrelated contents. Workspace
path admission constrains these file callbacks; it is not an OS sandbox.
Commands run under the declared process-user or container authority, with an
explicit working directory and shell, bounded duration/output, and owned
process-group settlement on POSIX or Job settlement on Windows. Commands are
foreground operations; deliberately detached background services are outside
this supported execution contract. This ownership is not OS confinement. Windows defaults to Windows PowerShell 5.1; PowerShell 7 is optional.
POSIX uses its configured/default shell. Bridge does not parse a private shell
language. Tool contracts and result shapes should remain concise and familiar
to models; specialized Copilot edit wrappers are not part of this interface.

### Evidence and unsupported capabilities

Ordered evidence retains native progress and Bridge callback events with their
original meanings. Typed tool observations explicitly distinguish server and
Bridge scope. Confirmation decisions carry the admitted policy digest. Effect
receipts describe only Bridge callbacks, including output truncation and command
settlement; they do not attest every server-owned effect. Raw native usage is
not normalized into token counts without a qualified mapping.

Cross-session parallel prompts, transparent recovery, provider-native system
roles, native output schemas, and normalized usage remain unsupported. Native
built-in read/search tools remain enabled initially. Replacing their catalog,
using model-trained patch/search interfaces, role-derived policies, and native
compaction/usage continuity are roadmap items requiring separate evidence.

## Supersession and retained obligations

This supersedes ADR-007's exclusive server-tool ownership, ADR-012's ACP wire
and native binding details, ADR-014/015's ACP state/model transition mechanics,
ADR-016's ACP-specific capture envelopes, and the retained-ACP clauses of
ADR-018/019. Their historical observations remain evidence. Authenticated
admission, resource bounds, operation reconciliation, prompt ownership, opt-in
ordered raw capture with visible failure, fail-loud validation, product naming,
and version-reported executable admission remain binding.

ACP-only transport/model-setter tests and standalone ACP diagnostic scripts
are retired with their implementation. Their continuing obligations—framing,
correlation, malformed responses, bounded evidence, callback failures, resource
ownership and interruption—belong to native transport/client tests and the
real native two-turn integration. Completion requires repository gates and
actual platform process evidence, not a renamed test fixture.
