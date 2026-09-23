"""
Probe: Manufactured CoT via synthetic conversation transcript.

Instead of multi-turn injection, we pack a complete synthetic conversation
into a single prompt. The model sees what looks like:
  1. A user question
  2. Its own partial response (cut off mid-thought)
  3. A user nudge asking it to continue

The entire thing arrives as one text block via session/prompt. The model
should interpret it as a resumed conversation where it was interrupted
and needs to continue its analysis.

The goal is to find the framing that most closely resembles native
conversation formatting so the model seamlessly continues the thought.

Usage:
    Requires the proxy running: acp-proxy --port 8765

    python tmp/probe_cot_painting.py [--model MODEL] [--port PORT]
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

PROXY_BASE = "http://127.0.0.1:{port}/v1"

QUESTION = (
    "What are the tradeoffs between using a stateful session model "
    "versus a stateless replay model for proxying LLM conversations?"
)


def send_completion(
    client: httpx.Client,
    model: str,
    messages: list[dict],
    base_url: str,
) -> str:
    resp = client.post(
        f"{base_url}/chat/completions",
        json={"model": model, "messages": messages, "stream": False},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Synthetic partial responses — the "painting"
#
# Each variant provides a truncated assistant response that looks like the
# model was mid-thought when it got cut off. The quality of continuation
# depends on how convincingly this reads as the model's own voice, abruptly
# interrupted.
# ---------------------------------------------------------------------------

PARTIAL_RESPONSE_A = """\
Here's my assessment of the key tradeoffs:

**Stateful Session Model**
- Maintains conversation history server-side
- Each new message is just the delta — efficient for long conversations
- But session state can be lost on server crash or restart

**Stateless Replay Model**
- Client sends full history with every request
- Server reconstructs context each time — no state to lose
- But replaying long histories is expensive and slow

However, there's a deeper issue I haven't addressed yet. The failure modes are fundamentally different, and this matters more than the performance characteristics. When a stateful session"""

PARTIAL_RESPONSE_B = """\
Let me think through this carefully.

The obvious tradeoffs are well-known: stateful is faster per-turn but fragile, stateless is resilient but redundant. But the interesting question is what happens at the boundaries — the edge cases that determine which model actually works in production.

Consider what happens when"""

PARTIAL_RESPONSE_C = """\
The surface-level comparison is straightforward, but I think the real tradeoffs emerge when you consider:

1. **Failure recovery** — what happens when state is lost?
2. **Concurrency** — what happens with parallel requests?
3. **Context integrity** — how do you know the model sees what you think it sees?

Let me work through each of these. For failure recovery, the stateful model has a critical weakness that"""


# ---------------------------------------------------------------------------
# Framing variants — how we present the synthetic conversation
# ---------------------------------------------------------------------------


def build_variants() -> list[tuple[str, list[dict]]]:
    """Build the message arrays for each variant.

    Each variant is a single user message containing the full synthetic
    transcript. The proxy will extract this as the last user message and
    send it verbatim to the ACP session.
    """
    variants = []

    # --- Variant 1: Markdown conversation with role headers ---
    for label, partial in [
        ("truncated_deep_a", PARTIAL_RESPONSE_A),
        ("truncated_deep_b", PARTIAL_RESPONSE_B),
        ("truncated_deep_c", PARTIAL_RESPONSE_C),
    ]:
        transcript = (
            f"User: {QUESTION}\n\n"
            f"Assistant: {partial}\n\n"
            f"User: It looks like you got cut off. Please continue your thoughts."
        )
        variants.append((label, [{"role": "user", "content": transcript}]))

    # --- Variant 2: Chat-ML style delimiters ---
    for label, partial in [
        ("chatml_deep_a", PARTIAL_RESPONSE_A),
    ]:
        transcript = (
            f"<|im_start|>user\n{QUESTION}<|im_end|>\n"
            f"<|im_start|>assistant\n{partial}<|im_end|>\n"
            f"<|im_start|>user\nIt looks like you got cut off. "
            f"Please continue your thoughts.<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        variants.append((label, [{"role": "user", "content": transcript}]))

    # --- Variant 3: OpenAI-native role labels ---
    # Simulate what the model sees in its own training data
    for label, partial in [
        ("native_roles_a", PARTIAL_RESPONSE_A),
    ]:
        messages = [
            {"role": "user", "content": QUESTION},
            {"role": "assistant", "content": partial},
            {
                "role": "user",
                "content": "It looks like you got cut off. Please continue your thoughts.",
            },
        ]
        variants.append((label, messages))

    # --- Variant 4: Bare transcript (no role labels) ---
    for label, partial in [
        ("bare_a", PARTIAL_RESPONSE_A),
    ]:
        transcript = (
            f"{QUESTION}\n\n"
            f"---\n\n"
            f"{partial}\n\n"
            f"---\n\n"
            f"It looks like you got cut off. Please continue your thoughts."
        )
        variants.append((label, [{"role": "user", "content": transcript}]))

    # --- Variant 5: System-framed ---
    for label, partial in [
        ("system_framed_a", PARTIAL_RESPONSE_A),
    ]:
        transcript = (
            f"The following is a conversation that was interrupted. "
            f"Please continue the assistant's response from where it left off.\n\n"
            f"User: {QUESTION}\n\n"
            f"Assistant: {partial}"
        )
        variants.append((label, [{"role": "user", "content": transcript}]))

    return variants


def run_baseline(client: httpx.Client, model: str, base_url: str) -> dict:
    print("\n=== BASELINE ===")
    messages = [{"role": "user", "content": QUESTION}]
    t0 = time.monotonic()
    response = send_completion(client, model, messages, base_url)
    elapsed = time.monotonic() - t0
    print(f"  {len(response)} chars, {elapsed:.1f}s")
    print(f"  Preview: {response[:250]}...")
    return {
        "variant": "baseline",
        "response": response,
        "len": len(response),
        "time_s": round(elapsed, 1),
    }


def run_variant(
    client: httpx.Client, model: str, base_url: str, label: str, messages: list[dict]
) -> dict:
    print(f"\n=== {label} ===")
    t0 = time.monotonic()
    response = send_completion(client, model, messages, base_url)
    elapsed = time.monotonic() - t0
    print(f"  {len(response)} chars, {elapsed:.1f}s")
    print(f"  Preview: {response[:250]}...")

    # Check if the model continued the thought vs started fresh
    # Heuristic: does the response start with connective language?
    starts_fresh = any(
        response.strip().lower().startswith(w)
        for w in ["here's", "the ", "let me", "sure", "great question", "certainly"]
    )
    continues = any(
        response.strip().lower().startswith(w)
        for w in [
            "is lost",
            "crashes",
            "fails",
            "the model",  # continuing mid-sentence
            "...",
            "—",
            "–",  # continuation markers
            "session",
            "state",  # topic continuation
        ]
    )

    continuity = "CONTINUES" if continues else ("FRESH" if starts_fresh else "UNCLEAR")
    print(f"  Continuity: {continuity}")

    return {
        "variant": label,
        "response": response,
        "len": len(response),
        "time_s": round(elapsed, 1),
        "continuity_heuristic": continuity,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="CoT painting probe")
    parser.add_argument("--model", default="gpt-4.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--variants", nargs="*", default=None)
    args = parser.parse_args()

    base_url = PROXY_BASE.format(port=args.port)
    http = httpx.Client()

    try:
        resp = http.get(f"{base_url}/models", timeout=5.0)
        resp.raise_for_status()
        available = [m["id"] for m in resp.json()["data"]]
        print(f"Models: {available}")
    except httpx.ConnectError:
        print(f"Cannot connect to proxy at {base_url}. Start: acp-proxy --port 8765")
        sys.exit(1)

    model = args.model
    print(f"Model: {model}")
    print(f"Question: {QUESTION}")

    results = [run_baseline(http, model, base_url)]

    all_variants = build_variants()
    if args.variants:
        all_variants = [
            (label, messages)
            for label, messages in all_variants
            if label in args.variants
        ]

    for label, messages in all_variants:
        results.append(run_variant(http, model, base_url, label, messages))

    output = os.path.join("logs", "cot_painting_results.json")
    os.makedirs("logs", exist_ok=True)
    with open(output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults → {output}")

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for r in results:
        cont = r.get("continuity_heuristic", "N/A")
        print(f"  {r['variant']:30s}  {r['len']:5d} chars  {r['time_s']:5.1f}s  {cont}")

    http.close()


if __name__ == "__main__":
    main()
