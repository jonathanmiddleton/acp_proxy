# CoT Injection via Packed Prompts

Exploits the ACP proxy's stateful sessions and last-user-message extraction
(ADR-004) to inject synthetic chain-of-thought reflections that measurably
improve model output quality.

## Core idea

ACP sessions accumulate turns, and every `session/prompt` is implicitly a user
turn. The proxy extracts only the last user message from OpenCode's request.
This means the "last user message" can contain a synthetic conversation
transcript with embedded role labels — the model interprets the plain-text
labels as conversational structure even though the protocol delivers it as a
single user turn. This is prompt injection by design.

The winning approach (phase 3) uses two HTTP requests:

1. **R1** — normal question, captures the model's unassisted response.
2. **R2** — a packed user message containing:
   - A brief closing remark ending the prior exchange
   - A synthetic partial assistant turn (truncated mid-clause) naming a
     failure category
   - A synthetic user nudge ("You got cut off. Please continue.")

The model seamlessly continues from the truncation point with revised output.

## Experiment phases

| Phase | Script | Approach | Status |
|-------|--------|----------|--------|
| 1 | `probe_cot_injection.py` | Multi-turn injection (one turn per HTTP request) | Superseded — every prompt is a user turn, can't inject assistant turns via protocol |
| 2 | `probe_cot_painting.py` | Single-blob transcript (full synthetic conversation in one message) | Superseded — worked but too close to a normal prompt |
| 3 | `probe_cot_v3.py` | Two-request packed injection (qualitative) | Active — depth steering and reversal resistance confirmed |
| 3+ | `probe_code_correctness.py` | Quantitative code correctness with N-iteration support | Active — statistical results with specificity gradient |

## Running experiments

All scripts talk to the ACP proxy's OpenAI-compatible endpoint. Start the
proxy first:

```bash
acp-proxy --system-prompt "You are a helpful assistant."
```

Then run a probe with a config file:

```bash
# Qualitative (depth steering / reversal)
python probe_cot_v3.py --config configs/first_run.json
python probe_cot_v3.py --config configs/reversal.json

# Quantitative (code correctness, single iteration)
python probe_code_correctness.py --config configs/code_eval_expr_targeted.json

# Statistical (N=5 iterations)
python probe_code_correctness.py --config configs/code_eval_expr_statistical.json -n 5
```

Common flags: `--model gpt-4.1`, `--port 8765`, `--variants <label>` (run
only specific injection variants).

Logs are written to `logs/` as paired `.log` (verbatim transcript) and `.json`
(structured results) files, timestamped per run.

## Key results

### Qualitative: depth steering works, reversal is resisted

- Truncating mid-clause produces seamless continuation (model starts with
  ellipsis and picks up exactly where the synthetic turn was cut).
- The more specific the truncation point, the more seamless the join.
- GPT-4.1 refuses to reverse factual positions even when injection achieves
  syntactic continuation — controls *form* but not *substance*.

### Quantitative: category-level injection is the sweet spot

Task: implement `eval_expr(expr: str) -> float` (expression evaluator, 23
tests). GPT-4.1 hits a deterministic `nonlocal` scoping bug 100% of the time
(0/23 baseline).

| Variant | Specificity | R2 mean (N=5) | Recovery |
|---------|-------------|---------------|----------|
| `generic_quality` | Generic | 0.0/23 | 0% |
| `category_scoping_mutation_edges` | Category + extras | 14.2/23 | ~62% |
| `category_scoping` | Category | 23.0/23 | 100% |
| `exact_bug` | Exact | 23.0/23 | 100% |

The specificity gradient: generic reflection does nothing, category-level
("variable scoping in closures") achieves full recovery, adding too many
concerns dilutes the signal, and naming the exact bug also works but requires
knowing it upfront.

## Configs

All configs live in `configs/` and follow the same schema:

```json
{
  "question": "...",
  "injections": [
    {
      "label": "variant_name",
      "closing": "Thanks.",
      "partial_assistant": "But wait, I should reconsider...",
      "nudge": "You got cut off. Please continue where you left off."
    }
  ]
}
```

| Config | Purpose |
|--------|---------|
| `first_run.json` | Qualitative depth steering (3 injection directions) |
| `reversal.json` | Reversal resistance (3 reversal attempts) |
| `code_correctness.json` | merge_intervals task (baseline — too easy) |
| `code_eval_expr.json` | eval_expr with generic injections |
| `code_eval_expr_targeted.json` | eval_expr with bug-specific injections |
| `code_eval_expr_statistical.json` | eval_expr specificity gradient, 5 variants for N-iteration runs |

## Directory structure

```
configs/          JSON experiment configs
docs/             Journal entry (gitignored working notes)
logs/             Per-run output (gitignored)
  cot_v3/         Qualitative probe logs
  code_correctness/  Quantitative probe logs
probe_cot_injection.py   Phase 1 (historical)
probe_cot_painting.py    Phase 2 (historical)
probe_cot_v3.py          Phase 3 qualitative
probe_code_correctness.py  Quantitative with N-iteration support
```

## Caveats

- Role labels embedded in content can trigger early termination in some LLMs
  during authoring. Store injection formats in config files, not inline prose.
- Experiments target `gpt-4.1` to match the target environment's model set.
- Earlier probes (phases 1-2) have hardcoded variants; later probes are
  config-driven.
