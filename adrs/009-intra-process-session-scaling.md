# ADR-009: Intra-Process Session Scaling

**Status:** Partially superseded by [ADR-012](012-meadow-direct-consumer-protocol.md); empirical scaling evidence retained
**Date:** 2026-04-05  

ADR-012 supersedes the pre-created pool, content-derived affinity, and
per-session-only concurrency clauses for Meadow direct mode. The measured
single-process scaling curve and same-session cancellation observation remain
historical evidence. Direct mode creates explicitly identified sessions on
demand and conservatively serializes prompts globally until a bounded live
probe establishes safe cross-session cancellation isolation.

## Context

Meadow orchestrates multiple concurrent agents (PM, developer, tester,
reviewer, and potentially subagents). Each agent needs its own ACP session
to maintain isolated conversation context. The proxy currently runs a single
copilot-language-server process with sessions multiplexed over one stdio
connection.

Two open questions needed empirical answers before committing to a workload
management policy:

1. Can a single language server process handle N parallel sessions?
2. Should Meadow scale by adding processes or by adding sessions?

### Empirical evidence

Experiments were run on two environments with a 688-token response (code
review of a Python script echoed back verbatim). All responses were
deterministic: 2752 chars every time, enabling clean tok/s measurement.
1 token ≈ 4 characters throughout.

**Local environment** (dev machine, direct internet): gpt-4.1, binary
v1.457.1, multiple runs per session count (3-6 runs each).

**Target environment** (corporate network, proxied): gpt-4.1 + gpt-4o
mixed, binary v1.442.0, single runs per session count.

The two environments exhibit proportional scaling with a consistent ~2.5x
speed factor: the target environment produces tokens at ~40% of the local
rate, but the scaling curve shape is the same.

**Local scaling curve (gpt-4.1, multi-run averages):**

| Sessions | Per-prompt tok/s | Aggregate tok/s | Mean latency (s) |
|---------:|:----------------:|:---------------:|:----------------:|
|        1 |       170        |       170       |       4.2        |
|        2 |       169        |       307       |       4.1        |
|        4 |       152        |       509       |       4.7        |
|        8 |       118        |       580       |       6.2        |
|       12 |       119        |      1272       |       5.8        |
|       16 |       113        |      1320       |       6.2        |
|       20 |       110        |      1655       |       6.4        |
|       24 |        97        |      1988       |       7.1        |
|       32 |        81        |      2041       |       8.6        |

**Target environment scaling (gpt-4.1 + gpt-4o mixed, single runs):**

| Sessions | Wall time (s) | Throughput (p/s) | Est. agg tok/s |
|---------:|:-------------:|:----------------:|:--------------:|
|        1 |     ~10.0     |       0.10       |       69       |
|        8 |     ~11.6     |       0.69       |      475       |
|       16 |     ~12.0     |       1.33       |      915       |
|       24 |     ~13.3     |       1.80       |      1238      |
|       32 |     13.54     |       2.36       |      1624      |
|       40 |     13.95     |       2.87       |      1975      |
|       48 |     13.98     |       3.43       |      2360      |
|       64 |     14.36     |       4.46       |      3069      |

**Key observations:**

- **There is no hard throughput ceiling.** Earlier results at 12-16 sessions
  appeared to show a plateau, but extending to 32 (local) and 64 (target)
  revealed continued scaling. The backend does not impose a fixed rate limit
  — it spreads compute across concurrent requests.
- **The tradeoff is aggregate throughput vs per-agent latency.** Adding
  sessions increases total token production but degrades each individual
  agent's generation rate. On the local environment, per-prompt tok/s drops
  from 170 (solo) to 81 (32 sessions) — a 52% degradation. Mean latency
  doubles from 4.2s to 8.6s.
- **Wall time grows sub-linearly.** On the target environment, wall time is
  nearly flat from 32 to 64 sessions (~14s). The backend serves more
  concurrent requests within the same time window, each slightly slower.
- **The 2.5x environment factor is consistent.** At every session count, the
  target environment's per-prompt latency is ~2.5x the local environment.
  This factor likely reflects corporate proxy overhead (TLS inspection,
  geographic routing, content filtering) rather than a different backend
  tier.
- **The bottleneck is the Copilot backend, not the local process.** The
  language server is a thin async relay. Inter-process experiments (2-4
  separate processes) yielded comparable throughput to the same number of
  sessions on one process.
- **Same-session concurrent prompts cause server-side cancellation.** Two
  prompts to the same session result in `"Operation cancelled by user"` on
  the earlier prompt. One prompt at a time per session is a hard constraint.
- **Rate limiting is per-account, not per-model.** Mixed-model experiments
  (gpt-4.1 + claude-haiku-4.5 locally, gpt-4.1 + gpt-4o on target) show
  that per-prompt tok/s is unchanged whether the other sessions use the same
  model or a different one. At 8 total sessions locally: gpt-4.1 solo =
  118 tok/s, gpt-4.1 in mixed (4 of 8) = 122 tok/s. Mixing models does
  not unlock additional throughput.

### Scaling model

The empirical data fits two functions parameterized per environment
(R² ≥ 0.94 local, ≥ 0.99 target):

```
aggregate_tok_s(N)   = a * N^b
per_prompt_tok_s(N)  = p0 / (1 + k * N)
mean_latency_s(N, T) = T / per_prompt_tok_s(N)
                      = T * (1 + k * N) / p0
```

where N = concurrent sessions and T = tokens per response.

| Parameter | Local | Target | Meaning                                      |
|-----------|------:|-------:|----------------------------------------------|
| a         |   173 |     76 | Base aggregate throughput (tok/s)            |
| b         | 0.737 |  0.895 | Scaling exponent (closer to 1 = more linear) |
| p0        |   170 |     68 | Solo per-prompt generation rate (tok/s)      |
| k         | 0.033 |  0.008 | Per-session degradation coefficient          |

The target environment has a slower baseline (p0 is 40% of local) but
scales more efficiently (b = 0.895 vs 0.737, k is 4x smaller). This is
consistent with a proxy pipeline that adds fixed per-request overhead —
the overhead becomes a smaller fraction of total time as generation time
grows with concurrency.

**Estimated mean latency for a 688-token response:**

| Sessions | Local (s) | Target (s) |
|---------:|:---------:|:----------:|
|        1 |    4.2    |    10.2    |
|        4 |    4.6    |    10.5    |
|        8 |    5.1    |    10.8    |
|       16 |    6.2    |    11.5    |
|       24 |    7.2    |    12.1    |
|       32 |    8.3    |    12.8    |
|       48 |   10.4    |    14.1    |
|       64 |   12.5    |    15.4    |

## Decision

Scale by adding sessions within a single process. Size the session pool
based on the acceptable per-agent latency, not a throughput ceiling.

Specifically:

1. **Single copilot-language-server process** — the proxy spawns and manages
   exactly one language server. No process pool. Inter-process scaling
   provides no throughput advantage.
2. **Pre-created session pool** — at startup, create N sessions (where N is
   the expected agent concurrency). Session creation takes ~2s each, so a
   pool of N sessions adds ~2N seconds to startup but eliminates per-agent
   session creation latency.
3. **Per-session concurrency lock** — enforce one-prompt-at-a-time per session
   with an asyncio semaphore. Concurrent prompts cause cancellation, so this
   must be enforced client-side.
4. **Session affinity per agent** — bind each agent to a dedicated session for
   the duration of its conversation. Sessions accumulate context, so switching
   mid-conversation loses history.
5. **Size for latency, not throughput.** The system scales continuously — there
   is no hard throughput ceiling. Use the scaling model to estimate per-agent
   latency at a given concurrency:

   `mean_latency_s = T * (1 + k * N) / p0`

   For Meadow's typical 4-8 concurrent agents (PM, developer, tester,
   reviewer, plus occasional subagents), 8-16 sessions keeps per-agent
   latency under ~12s on the target environment. For workflows with more
   subagents, 32-64 sessions are viable — on the target environment,
   latency grows only 25% (10.2s → 12.8s) from 1 to 32 sessions thanks
   to the low degradation coefficient.

## Rationale

- **Empirical, not theoretical.** The scaling curve was measured across two
  environments with consistent functional form. Extended to 64 sessions on
  the target environment with no errors or failures.
- **Predictable scaling.** The power-law and hyperbolic models fit with high
  R² (≥ 0.94), enabling capacity planning from the parameters rather than
  requiring new experiments for each session count.
- **No hard ceiling simplifies planning.** The session pool can be sized to
  the workload without worrying about a throughput cliff. The tradeoff is
  smooth: more agents = proportionally slower per-agent response.
- **Startup cost matters.** Each additional process adds ~3-5s to startup.
  A single process with a pre-created session pool starts faster and is
  simpler to manage.
- **Simplicity.** One process, one transport, one read loop. No process pool
  management, no inter-process message routing, no split-brain failure modes.

## Consequences

- **Session accumulation** remains an unsolved problem (see ADR-002). At
  higher session counts, the language server holds more state in memory.
  Periodic process restart may be needed as a cleanup strategy. At 64
  sessions, no memory or stability issues were observed, but longer runs
  are needed to validate.
- **Per-agent latency is the binding constraint** but is more favorable
  than initially expected. On the target environment, latency degrades
  slowly: ~10s at 1 session, ~13s at 32, ~15s at 64. Meadow workflows
  can tolerate 32+ concurrent agents with modest per-agent impact.
- **The scaling model enables capacity planning.** Given environment
  parameters (p0, k), latency for any concurrency and response size can
  be estimated without new experiments:
  `mean_latency_s = T * (1 + k * N) / p0`. New environments need only
  a solo baseline run and one concurrent run to calibrate p0 and k.
- **Per-session concurrency guard needed.** The proxy must prevent two
  prompts from hitting the same ACP session simultaneously (causes
  cancellation). A per-session asyncio semaphore is sufficient. Session
  mapping itself is already handled by ADR-002's first-message hashing —
  each OpenCode subsession (including hierarchical parent/child sessions)
  naturally gets its own ACP session because each has a distinct first
  user message.
- **Model diversity is not a scaling lever.** All models share the same
  backend budget per account. Assigning agents to different models should
  be driven by capability needs, not throughput optimization.
