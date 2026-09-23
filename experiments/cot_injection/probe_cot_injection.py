"""
Probe: Can we inject synthetic chain-of-thought via the proxy HTTP endpoint?

This test goes through the full pipeline:
  OpenCode-style HTTP request → ACP Proxy → copilot-language-server → Copilot

The proxy extracts only the last user message (ADR-004) and sends it to
the stateful ACP session (ADR-002). Session history accumulates server-side.

The injection strategy:
  1. Request 1: messages=[user: question]
     → proxy sends question to ACP, model responds
  2. Request 2: messages=[user: question, assistant: <real_response>,
                          user: <synthetic_reflection>]
     → proxy extracts the reflection as "last user message", sends to
       same ACP session (same first-user-message hash)
  3. Request 3: messages=[user: question, assistant: <real_response>,
                          user: <reflection>, assistant: <reflection_response>,
                          user: <nudge>]
     → proxy extracts the nudge, model continues with deeper reasoning

The model sees a conversation that looks like:
  Turn 1: user question → model answer
  Turn 2: "But wait, I should reconsider..." → model reflects
  Turn 3: "Please continue" → model gives revised assessment

We compare baseline (single turn) vs injected (three turns) to see if
the reflection injection produces qualitatively different output.

Usage:
    Requires the proxy to be running: acp-proxy --port 8765

    python tmp/probe_cot_injection.py [--model MODEL] [--port PORT]
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
    "versus a stateless replay model for proxying LLM conversations? "
    "Give a brief assessment."
)

NUDGE = "Please continue what you were thinking."

REFLECTION_VARIANTS: list[tuple[str, str]] = [
    (
        "user_framed_reflection",
        "But wait, let me reconsider. I should think about this more "
        "carefully before giving my final assessment. There are several "
        "aspects I may have overlooked:",
    ),
    (
        "explicit_continue",
        "You were about to reconsider some aspects of your answer. "
        "What did you overlook?",
    ),
    (
        "socratic_push",
        "That's a reasonable first pass, but I think you're missing some "
        "important tradeoffs. What about failure modes? What happens when "
        "session state is lost?",
    ),
    (
        "prefilled_assistant",
        "[Continuing my analysis]\n\n"
        "Actually, I realize I didn't fully address the failure modes. "
        "Let me reconsider the edge cases:",
    ),
]


def send_completion(
    client: httpx.Client,
    model: str,
    messages: list[dict],
    base_url: str,
) -> str:
    """Send a chat completion request and return the response text."""
    resp = client.post(
        f"{base_url}/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def run_baseline(
    client: httpx.Client,
    model: str,
    base_url: str,
) -> dict:
    """Single-turn baseline: ask the question, get one response."""
    print("\n=== BASELINE (single turn) ===")
    messages = [{"role": "user", "content": QUESTION}]

    t0 = time.monotonic()
    response = send_completion(client, model, messages, base_url)
    elapsed = time.monotonic() - t0

    print(f"  Response: {len(response)} chars, {elapsed:.1f}s")
    print(f"  Preview: {response[:200]}...")
    return {
        "variant": "baseline",
        "response": response,
        "response_len": len(response),
        "time_s": round(elapsed, 1),
    }


def run_variant(
    client: httpx.Client,
    model: str,
    base_url: str,
    label: str,
    injection: str,
) -> dict:
    """Three-turn CoT injection variant."""
    print(f"\n=== Variant: {label} ===")

    # --- Turn 1: Ask the question ---
    messages_t1 = [{"role": "user", "content": QUESTION}]
    t0 = time.monotonic()
    response_1 = send_completion(client, model, messages_t1, base_url)
    t1 = time.monotonic()
    print(f"  Turn 1 (initial): {len(response_1)} chars, {t1 - t0:.1f}s")

    # --- Turn 2: Inject reflection as a "user" message ---
    # The proxy extracts only the last user message. The ACP session
    # already has turn 1 in its history. We replay the full conversation
    # (OpenCode-style) with the injection as the new user turn.
    messages_t2 = [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": response_1},
        {"role": "user", "content": injection},
    ]
    t2 = time.monotonic()
    response_2 = send_completion(client, model, messages_t2, base_url)
    t3 = time.monotonic()
    print(f"  Turn 2 (after injection): {len(response_2)} chars, {t3 - t2:.1f}s")

    # --- Turn 3: Nudge to continue ---
    messages_t3 = [
        {"role": "user", "content": QUESTION},
        {"role": "assistant", "content": response_1},
        {"role": "user", "content": injection},
        {"role": "assistant", "content": response_2},
        {"role": "user", "content": NUDGE},
    ]
    t4 = time.monotonic()
    response_3 = send_completion(client, model, messages_t3, base_url)
    t5 = time.monotonic()
    print(f"  Turn 3 (after nudge): {len(response_3)} chars, {t5 - t4:.1f}s")

    total_time = t5 - t0
    total_output = len(response_1) + len(response_2) + len(response_3)

    return {
        "variant": label,
        "injection": injection,
        "turn_1": {
            "response": response_1,
            "len": len(response_1),
            "time_s": round(t1 - t0, 1),
        },
        "turn_2": {
            "response": response_2,
            "len": len(response_2),
            "time_s": round(t3 - t2, 1),
        },
        "turn_3": {
            "response": response_3,
            "len": len(response_3),
            "time_s": round(t5 - t4, 1),
        },
        "total_output_len": total_output,
        "total_time_s": round(total_time, 1),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Probe CoT injection through the ACP proxy HTTP endpoint"
    )
    parser.add_argument(
        "--model", default="gpt-4.1", help="Model ID (default: gpt-4.1)"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="Proxy port (default: 8765)"
    )
    parser.add_argument(
        "--variants",
        nargs="*",
        default=None,
        help="Specific variant labels to run (default: all)",
    )
    args = parser.parse_args()

    base_url = PROXY_BASE.format(port=args.port)
    model = args.model

    # Verify proxy is reachable
    http = httpx.Client()
    try:
        resp = http.get(f"{base_url}/models", timeout=5.0)
        resp.raise_for_status()
        available = [m["id"] for m in resp.json()["data"]]
        print(f"Proxy reachable. Models: {available}")
        if model not in available:
            print(f"WARNING: model '{model}' not in available models")
    except httpx.ConnectError:
        print(f"ERROR: Cannot connect to proxy at {base_url}")
        print("Start the proxy first: acp-proxy --port 8765")
        sys.exit(1)

    print(f"Model: {model}")
    print(f"Question: {QUESTION}")

    results = []

    # Baseline
    baseline = run_baseline(http, model, base_url)
    results.append(baseline)

    # Variants
    variants = REFLECTION_VARIANTS
    if args.variants:
        variants = [(label, text) for label, text in variants if label in args.variants]

    for label, injection in variants:
        result = run_variant(http, model, base_url, label, injection)
        results.append(result)

    # Write results
    output_path = os.path.join("logs", "cot_probe_results.json")
    os.makedirs("logs", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults written to {output_path}")

    # Summary
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for r in results:
        v = r["variant"]
        if v == "baseline":
            print(f"  {v}: {r['response_len']} chars, {r['time_s']}s")
        else:
            print(
                f"  {v}: {r['total_output_len']} chars total "
                f"(t1={r['turn_1']['len']}, t2={r['turn_2']['len']}, "
                f"t3={r['turn_3']['len']}), {r['total_time_s']}s"
            )

    http.close()


if __name__ == "__main__":
    main()
