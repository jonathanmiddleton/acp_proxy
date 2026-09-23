"""
Concurrency probe for copilot-language-server (ACP mode).

Investigates two scaling dimensions:
1. Intra-process: parallel sessions within a single language server process.
2. Inter-process: multiple language server processes running simultaneously.

Talks directly to copilot-language-server over stdio (no proxy in between).
Each test case creates the specified number of processes and sessions, fires
prompts in parallel, and measures latency, success/failure, and any errors.

Usage:
    python probe_concurrency.py --config configs/default.json
    python probe_concurrency.py --config configs/default.json --tests baseline_sequential intra_parallel_sessions_2
    python probe_concurrency.py --config configs/default.json --binary /path/to/copilot-language-server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from typing import Any

from acp_harness import AcpProcess, PromptResult

logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


class TestLogger:
    """Writes a verbatim log file for the experiment run."""

    def __init__(self, tag: str) -> None:
        os.makedirs(LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(LOG_DIR, f"concurrency_{ts}_{tag}.log")
        self._f = open(self.path, "w")

    def write(self, text: str) -> None:
        self._f.write(text + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def find_binary(cli_binary: str | None) -> str:
    """Resolve the copilot-language-server binary path."""
    if cli_binary:
        if not os.path.isfile(cli_binary):
            print(f"ERROR: Binary not found: {cli_binary}")
            sys.exit(1)
        return cli_binary

    # Try importing from the proxy's discovery module
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
        from acp_proxy.discovery import find_binary as _find

        binary = _find()
        if binary:
            return binary
    except ImportError:
        pass

    print("ERROR: Could not find copilot-language-server. Use --binary.")
    sys.exit(1)


def load_config(path: str) -> dict[str, Any]:
    """Load and validate a config file."""
    with open(path) as f:
        cfg = json.load(f)
    assert "tests" in cfg, "Config must have a 'tests' key"
    assert "prompt" in cfg, "Config must have a 'prompt' key"
    return cfg


# --- Test execution ---


async def run_baseline_sequential(
    binary: str,
    model: str,
    prompt_text: str,
    warmup_prompt: str | None,
    total_prompts: int,
    prompt_timeout: float,
    test_logger: TestLogger,
) -> dict[str, Any]:
    """Baseline: sequential prompts on one session, one process."""
    proc = AcpProcess(binary, label="baseline")
    cwd = os.getcwd()
    await proc.start(cwd)

    try:
        session_id = await proc.create_session(cwd, model)

        # Warmup
        if warmup_prompt:
            test_logger.write("  Warmup prompt...")
            warmup = await proc.prompt(
                session_id, warmup_prompt, timeout=prompt_timeout
            )
            test_logger.write(
                f"  Warmup: {warmup.elapsed_s:.2f}s, "
                f"stop={warmup.stop_reason}, "
                f"len={len(warmup.text)}"
            )

        results: list[PromptResult] = []
        for i in range(total_prompts):
            # Each prompt on a fresh session to avoid context accumulation
            sid = await proc.create_session(cwd, model)
            r = await proc.prompt(sid, prompt_text, timeout=prompt_timeout)
            results.append(r)
            test_logger.write(
                f"  Prompt {i + 1}/{total_prompts}: "
                f"{r.elapsed_s:.2f}s, stop={r.stop_reason}, "
                f"len={len(r.text)}, error={r.error}"
            )

        return _summarize_results(results)
    finally:
        await proc.stop()


async def run_intra_process(
    binary: str,
    model: str,
    prompt_text: str,
    warmup_prompt: str | None,
    sessions_per_process: int,
    parallel_prompts_per_session: int,
    prompt_timeout: float,
    test_logger: TestLogger,
    models: list[str] | None = None,
) -> dict[str, Any]:
    """Intra-process: parallel sessions (and optionally parallel prompts) on one process.

    If models is provided, sessions are assigned models round-robin from the
    list. Otherwise all sessions use the single model argument.
    """
    proc = AcpProcess(binary, label="intra")
    cwd = os.getcwd()
    await proc.start(cwd)

    try:
        # Create all sessions, assigning models round-robin if provided
        session_ids: list[str] = []
        session_models: list[str] = []
        for i in range(sessions_per_process):
            m = models[i % len(models)] if models else model
            sid = await proc.create_session(cwd, m)
            session_ids.append(sid)
            session_models.append(m)
            test_logger.write(
                f"  Created session {i + 1}/{sessions_per_process}: {sid} (model={m})"
            )

        # Warmup each session
        if warmup_prompt:
            test_logger.write("  Warming up sessions...")
            warmup_tasks = [
                proc.prompt(sid, warmup_prompt, timeout=prompt_timeout)
                for sid in session_ids
            ]
            warmup_results = await asyncio.gather(*warmup_tasks, return_exceptions=True)
            for i, wr in enumerate(warmup_results):
                if isinstance(wr, Exception):
                    test_logger.write(f"  Warmup session {i}: ERROR {wr}")
                else:
                    test_logger.write(
                        f"  Warmup session {i} ({session_models[i]}): "
                        f"{wr.elapsed_s:.2f}s, stop={wr.stop_reason}"
                    )

        # Fire prompts in parallel
        test_logger.write(
            f"  Firing {sessions_per_process} sessions x "
            f"{parallel_prompts_per_session} prompts = "
            f"{sessions_per_process * parallel_prompts_per_session} total..."
        )

        tasks = []
        task_models: list[str] = []
        for idx, sid in enumerate(session_ids):
            for _ in range(parallel_prompts_per_session):
                tasks.append(proc.prompt(sid, prompt_text, timeout=prompt_timeout))
                task_models.append(session_models[idx])

        t0 = time.monotonic()
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        wall_time = time.monotonic() - t0

        results: list[PromptResult] = []
        for i, r in enumerate(raw_results):
            if isinstance(r, Exception):
                test_logger.write(f"  Result {i}: EXCEPTION {r}")
                results.append(
                    PromptResult(
                        session_id="unknown",
                        text="",
                        stop_reason="exception",
                        elapsed_s=0.0,
                        model=task_models[i],
                        error=str(r),
                    )
                )
            else:
                r.model = task_models[i]
                test_logger.write(
                    f"  Result {i} ({task_models[i]}, session {r.session_id[:8]}): "
                    f"{r.elapsed_s:.2f}s, stop={r.stop_reason}, "
                    f"len={len(r.text)}, error={r.error}"
                )
                results.append(r)

        summary = _summarize_results(results)
        summary["wall_time_s"] = wall_time
        return summary
    finally:
        await proc.stop()


async def run_inter_process(
    binary: str,
    model: str,
    prompt_text: str,
    warmup_prompt: str | None,
    num_processes: int,
    sessions_per_process: int,
    parallel_prompts_per_session: int,
    prompt_timeout: float,
    test_logger: TestLogger,
) -> dict[str, Any]:
    """Inter-process: multiple language server processes, each with sessions, all prompted in parallel."""
    cwd = os.getcwd()
    processes: list[AcpProcess] = []

    try:
        # Start all processes
        test_logger.write(f"  Starting {num_processes} processes...")
        for i in range(num_processes):
            proc = AcpProcess(binary, label=f"proc-{i}")
            await proc.start(cwd)
            processes.append(proc)
            test_logger.write(
                f"  Process {i} started: {proc.agent_name} v{proc.agent_version}"
            )

        # Create sessions on each process
        all_sessions: list[tuple[AcpProcess, str]] = []
        for i, proc in enumerate(processes):
            for j in range(sessions_per_process):
                sid = await proc.create_session(cwd, model)
                all_sessions.append((proc, sid))
                test_logger.write(f"  Process {i}, session {j}: {sid}")

        # Warmup
        if warmup_prompt:
            test_logger.write("  Warming up all sessions...")
            warmup_tasks = [
                proc.prompt(sid, warmup_prompt, timeout=prompt_timeout)
                for proc, sid in all_sessions
            ]
            warmup_results = await asyncio.gather(*warmup_tasks, return_exceptions=True)
            for i, wr in enumerate(warmup_results):
                if isinstance(wr, Exception):
                    test_logger.write(f"  Warmup {i}: ERROR {wr}")
                else:
                    test_logger.write(
                        f"  Warmup {i}: {wr.elapsed_s:.2f}s, stop={wr.stop_reason}"
                    )

        # Fire all prompts in parallel
        total = len(all_sessions) * parallel_prompts_per_session
        test_logger.write(
            f"  Firing {len(all_sessions)} sessions x "
            f"{parallel_prompts_per_session} prompts = {total} total "
            f"across {num_processes} processes..."
        )

        tasks = []
        for proc, sid in all_sessions:
            for _ in range(parallel_prompts_per_session):
                tasks.append(proc.prompt(sid, prompt_text, timeout=prompt_timeout))

        t0 = time.monotonic()
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        wall_time = time.monotonic() - t0

        results: list[PromptResult] = []
        for i, r in enumerate(raw_results):
            if isinstance(r, Exception):
                test_logger.write(f"  Result {i}: EXCEPTION {r}")
                results.append(
                    PromptResult(
                        session_id="unknown",
                        text="",
                        stop_reason="exception",
                        elapsed_s=0.0,
                        error=str(r),
                    )
                )
            else:
                test_logger.write(
                    f"  Result {i} (proc {r.session_id[:4]}, session {r.session_id[:8]}): "
                    f"{r.elapsed_s:.2f}s, stop={r.stop_reason}, "
                    f"len={len(r.text)}, error={r.error}"
                )
                results.append(r)

        summary = _summarize_results(results)
        summary["wall_time_s"] = wall_time
        summary["num_processes"] = num_processes
        return summary
    finally:
        for proc in processes:
            await proc.stop()


def _summarize_results(results: list[PromptResult]) -> dict[str, Any]:
    """Compute summary statistics from a list of prompt results."""
    if not results:
        return {"count": 0}

    successes = [r for r in results if r.error is None]
    failures = [r for r in results if r.error is not None]
    elapsed_values = [r.elapsed_s for r in successes]

    summary: dict[str, Any] = {
        "count": len(results),
        "successes": len(successes),
        "failures": len(failures),
        "failure_errors": [r.error for r in failures],
        "stop_reasons": {},
    }

    for r in results:
        reason = r.stop_reason
        summary["stop_reasons"][reason] = summary["stop_reasons"].get(reason, 0) + 1

    if elapsed_values:
        elapsed_values.sort()
        summary["latency_min_s"] = elapsed_values[0]
        summary["latency_max_s"] = elapsed_values[-1]
        summary["latency_mean_s"] = sum(elapsed_values) / len(elapsed_values)
        summary["latency_median_s"] = elapsed_values[len(elapsed_values) // 2]
        # Throughput: prompts completed per second of wall time
        # (for parallel tests, wall_time_s is added separately)
    else:
        summary["latency_min_s"] = None
        summary["latency_max_s"] = None
        summary["latency_mean_s"] = None
        summary["latency_median_s"] = None

    # Include per-result detail (full text stored for token analysis)
    summary["per_result"] = [
        {
            "session_id": r.session_id,
            "model": r.model,
            "elapsed_s": r.elapsed_s,
            "stop_reason": r.stop_reason,
            "text_len": len(r.text),
            "text": r.text,
            "error": r.error,
            "num_updates": len(r.updates),
        }
        for r in results
    ]

    # Per-model breakdown (only if multiple models present)
    models_seen = {r.model for r in results if r.model}
    if len(models_seen) > 1:
        by_model: dict[str, dict[str, Any]] = {}
        for m in sorted(models_seen):
            m_results = [r for r in successes if r.model == m]
            m_elapsed = [r.elapsed_s for r in m_results]
            by_model[m] = {
                "count": len(m_results),
                "latency_mean_s": (
                    sum(m_elapsed) / len(m_elapsed) if m_elapsed else None
                ),
                "latency_min_s": min(m_elapsed) if m_elapsed else None,
                "latency_max_s": max(m_elapsed) if m_elapsed else None,
            }
        summary["by_model"] = by_model

    return summary


def compute_baseline_summary(all_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract a baseline summary from experiment results.

    Identifies solo and concurrent results from the propagated test config
    fields (``_sessions``, ``_parallel``, ``_is_sequential``) rather than
    parsing label names, so this works with any config.

    Returns a dict with:
    - ``solo_latency_s``: mean latency for a single-session, single-prompt result
    - ``solo_tok_s``: estimated tokens/s from response text length
    - ``n8_latency_s``: mean latency for the 8-session result (if present)
    - ``scaling_factor_8``: n8_latency / solo_latency — the primary scalar

    The scaling factor normalizes out absolute environment speed and isolates
    the concurrency degradation coefficient.  1.0 = no degradation.
    """
    summary: dict[str, Any] = {}

    solo_result = None
    n8_result = None

    for r in all_results:
        mean = r.get("latency_mean_s")
        if mean is None:
            continue

        sessions = r.get("_sessions", 0)
        parallel = r.get("_parallel", 1)
        is_seq = r.get("_is_sequential", False)

        # Solo: 1 session, 1 parallel prompt, non-sequential (single prompt).
        # For sequential baselines (total_prompts > 1), still usable as p0
        # since each prompt runs alone.
        is_solo = sessions == 1 and parallel == 1
        if is_solo and solo_result is None:
            solo_result = r

        # 8-session concurrent
        if sessions == 8 and parallel == 1 and not is_seq:
            n8_result = r

    if solo_result and solo_result.get("latency_mean_s"):
        p0_latency = solo_result["latency_mean_s"]
        summary["solo_label"] = solo_result.get("test_label")
        summary["solo_latency_s"] = round(p0_latency, 2)

        # Estimate tok/s from text length if available
        per_result = solo_result.get("per_result", [])
        if per_result:
            text_lens = [pr["text_len"] for pr in per_result if pr.get("text_len")]
            if text_lens:
                avg_chars = sum(text_lens) / len(text_lens)
                summary["solo_tok_s"] = round((avg_chars / 4) / p0_latency, 1)

        if n8_result and n8_result.get("latency_mean_s"):
            n8_latency = n8_result["latency_mean_s"]
            summary["n8_label"] = n8_result.get("test_label")
            summary["n8_latency_s"] = round(n8_latency, 2)
            summary["scaling_factor_8"] = round(n8_latency / p0_latency, 2)

    return summary


async def run_test(
    test_cfg: dict[str, Any],
    binary: str,
    model: str,
    prompt_text: str,
    warmup_prompt: str | None,
    prompt_timeout: float,
    test_logger: TestLogger,
) -> dict[str, Any]:
    """Dispatch a single test case based on its mode."""
    label = test_cfg["label"]
    mode = test_cfg["mode"]
    test_logger.write(f"\n{'=' * 60}")
    test_logger.write(f"TEST: {label}")
    test_logger.write(f"  Description: {test_cfg.get('description', '')}")
    test_logger.write(f"  Mode: {mode}")
    test_logger.write(f"  Processes: {test_cfg.get('num_processes', 1)}")
    test_logger.write(f"  Sessions/process: {test_cfg.get('sessions_per_process', 1)}")
    test_logger.write(
        f"  Parallel prompts/session: {test_cfg.get('parallel_prompts_per_session', 1)}"
    )
    # Per-test model override: tests can specify "models" list for mixed-model runs
    test_models = test_cfg.get("models")
    if test_models:
        test_logger.write(f"  Models: {test_models}")
    test_logger.write(f"{'=' * 60}")

    t0 = time.monotonic()

    # A sequential baseline is an intra_process test with 1 session, 1 parallel
    # prompt, and total_prompts > 1. Identified by config, not label name.
    is_sequential = (
        mode == "intra_process"
        and test_cfg.get("sessions_per_process", 1) == 1
        and test_cfg.get("parallel_prompts_per_session", 1) == 1
        and test_cfg.get("total_prompts", 1) > 1
    )

    # For sequential baseline and tests without models list, use the
    # single model. For mixed-model tests, the first model in the list
    # is used for sequential baselines.
    effective_model = test_models[0] if test_models else model

    if is_sequential:
        result = await run_baseline_sequential(
            binary=binary,
            model=effective_model,
            prompt_text=prompt_text,
            warmup_prompt=warmup_prompt,
            total_prompts=test_cfg.get("total_prompts", 3),
            prompt_timeout=prompt_timeout,
            test_logger=test_logger,
        )
    elif mode == "intra_process":
        result = await run_intra_process(
            binary=binary,
            model=effective_model,
            prompt_text=prompt_text,
            warmup_prompt=warmup_prompt,
            sessions_per_process=test_cfg.get("sessions_per_process", 1),
            parallel_prompts_per_session=test_cfg.get(
                "parallel_prompts_per_session", 1
            ),
            prompt_timeout=prompt_timeout,
            test_logger=test_logger,
            models=test_models,
        )
    elif mode == "inter_process":
        result = await run_inter_process(
            binary=binary,
            model=effective_model,
            prompt_text=prompt_text,
            warmup_prompt=warmup_prompt,
            num_processes=test_cfg.get("num_processes", 1),
            sessions_per_process=test_cfg.get("sessions_per_process", 1),
            parallel_prompts_per_session=test_cfg.get(
                "parallel_prompts_per_session", 1
            ),
            prompt_timeout=prompt_timeout,
            test_logger=test_logger,
        )
    else:
        raise ValueError(f"Unknown test mode: {mode}")

    test_time = time.monotonic() - t0
    result["test_label"] = label
    result["test_total_time_s"] = test_time
    # Propagate test config into result for baseline computation.
    result["_sessions"] = test_cfg.get("sessions_per_process", 1)
    result["_parallel"] = test_cfg.get("parallel_prompts_per_session", 1)
    result["_total_prompts"] = test_cfg.get("total_prompts", 1)
    result["_is_sequential"] = is_sequential

    test_logger.write("\n  Summary:")
    test_logger.write(f"    Successes: {result['successes']}/{result['count']}")
    test_logger.write(f"    Failures:  {result['failures']}/{result['count']}")
    if result.get("latency_mean_s") is not None:
        test_logger.write(f"    Latency mean:   {result['latency_mean_s']:.2f}s")
        test_logger.write(f"    Latency median: {result['latency_median_s']:.2f}s")
        test_logger.write(
            f"    Latency range:  {result['latency_min_s']:.2f}s - {result['latency_max_s']:.2f}s"
        )
    if "wall_time_s" in result:
        test_logger.write(f"    Wall time:      {result['wall_time_s']:.2f}s")
        if result["successes"] > 0:
            throughput = result["successes"] / result["wall_time_s"]
            test_logger.write(f"    Throughput:     {throughput:.2f} prompts/s")
            result["throughput_prompts_per_s"] = throughput
    if result["failure_errors"]:
        test_logger.write(f"    Errors: {result['failure_errors']}")
    test_logger.write(f"    Stop reasons: {result['stop_reasons']}")
    if "by_model" in result:
        test_logger.write("    Per-model breakdown:")
        for m, stats in result["by_model"].items():
            test_logger.write(
                f"      {m}: n={stats['count']}, "
                f"mean={stats['latency_mean_s']:.2f}s, "
                f"range={stats['latency_min_s']:.2f}-{stats['latency_max_s']:.2f}s"
            )
    test_logger.write(f"    Total test time: {test_time:.2f}s")

    return result


def print_summary_table(all_results: list[dict[str, Any]]) -> None:
    """Print a concise summary table to stdout."""
    print(f"\n{'=' * 90}")
    print("CONCURRENCY EXPERIMENT RESULTS")
    print(f"{'=' * 90}")
    print(
        f"{'Test':<35} {'OK/N':>6} {'Mean(s)':>8} {'Med(s)':>8} "
        f"{'Min(s)':>8} {'Max(s)':>8} {'Wall(s)':>8} {'Tput':>8}"
    )
    print("-" * 90)

    for r in all_results:
        label = r.get("test_label", "?")
        ok_n = f"{r['successes']}/{r['count']}"
        mean = (
            f"{r['latency_mean_s']:.2f}"
            if r.get("latency_mean_s") is not None
            else "n/a"
        )
        med = (
            f"{r['latency_median_s']:.2f}"
            if r.get("latency_median_s") is not None
            else "n/a"
        )
        mn = (
            f"{r['latency_min_s']:.2f}" if r.get("latency_min_s") is not None else "n/a"
        )
        mx = (
            f"{r['latency_max_s']:.2f}" if r.get("latency_max_s") is not None else "n/a"
        )
        wall = f"{r['wall_time_s']:.2f}" if "wall_time_s" in r else "n/a"
        tput = (
            f"{r['throughput_prompts_per_s']:.2f}"
            if "throughput_prompts_per_s" in r
            else "n/a"
        )
        print(
            f"{label:<35} {ok_n:>6} {mean:>8} {med:>8} {mn:>8} {mx:>8} {wall:>8} {tput:>8}"
        )

    print(f"{'=' * 90}")


async def async_main(args: argparse.Namespace) -> None:
    """Async entry point."""
    cfg = load_config(args.config)
    binary = find_binary(args.binary)
    model = args.model or cfg.get("model", "gpt-4.1")
    prompt_text = cfg["prompt"]
    warmup_prompt = cfg.get("warmup_prompt")
    prompt_timeout = cfg.get("prompt_timeout_s", 120.0)

    tag = args.tag or os.path.splitext(os.path.basename(args.config))[0]
    test_logger = TestLogger(tag)

    print(f"Binary: {binary}")
    print(f"Model: {model}")
    print(f"Prompt: {prompt_text[:60]}...")
    print(f"Log: {test_logger.path}")

    test_logger.write(f"Concurrency Experiment — {datetime.now().isoformat()}")
    test_logger.write(f"Binary: {binary}")
    test_logger.write(f"Model: {model}")
    test_logger.write(f"Prompt: {prompt_text}")
    test_logger.write(f"Warmup: {warmup_prompt}")
    test_logger.write(f"Timeout: {prompt_timeout}s")

    tests = cfg["tests"]
    if args.tests:
        tests = [t for t in tests if t["label"] in args.tests]
        if not tests:
            print(
                f"ERROR: No matching tests. Available: {[t['label'] for t in cfg['tests']]}"
            )
            sys.exit(1)

    all_results: list[dict[str, Any]] = []
    for test_cfg in tests:
        print(f"\nRunning: {test_cfg['label']}...")
        try:
            result = await run_test(
                test_cfg=test_cfg,
                binary=binary,
                model=model,
                prompt_text=prompt_text,
                warmup_prompt=warmup_prompt,
                prompt_timeout=prompt_timeout,
                test_logger=test_logger,
            )
            all_results.append(result)
            print(
                f"  -> {result['successes']}/{result['count']} succeeded, "
                f"mean={result.get('latency_mean_s', 'n/a')}s"
            )
        except Exception as e:
            print(f"  -> TEST FAILED: {e}")
            test_logger.write(f"\n  TEST EXCEPTION: {e}")
            all_results.append(
                {
                    "test_label": test_cfg["label"],
                    "count": 0,
                    "successes": 0,
                    "failures": 0,
                    "failure_errors": [str(e)],
                    "stop_reasons": {},
                    "latency_mean_s": None,
                    "latency_median_s": None,
                    "latency_min_s": None,
                    "latency_max_s": None,
                    "error": str(e),
                }
            )

    # Summary table
    print_summary_table(all_results)

    # Baseline scalar
    host = args.host or socket.gethostname()
    baseline = compute_baseline_summary(all_results)
    baseline["host"] = host

    if baseline.get("scaling_factor_8") is not None:
        print(f"\n--- Baseline ({host}) ---")
        print(
            f"Solo latency: {baseline['solo_latency_s']}s"
            + (
                f"  ({baseline['solo_tok_s']} tok/s)"
                if "solo_tok_s" in baseline
                else ""
            )
        )
        print(f"N=8 latency:  {baseline['n8_latency_s']}s")
        print(f"Scaling factor (N=8): {baseline['scaling_factor_8']}")
    elif baseline.get("solo_latency_s") is not None:
        print(f"\n--- Baseline ({host}) ---")
        print(
            f"Solo latency: {baseline['solo_latency_s']}s"
            + (
                f"  ({baseline['solo_tok_s']} tok/s)"
                if "solo_tok_s" in baseline
                else ""
            )
        )
        print("(No 8-session result found — scaling factor not computed)")
    else:
        print(f"\n--- Baseline ({host}) ---")
        print("(No solo result found — baseline not computed)")

    # Write JSON results
    json_path = test_logger.path.replace(".log", ".json")
    with open(json_path, "w") as f:
        json.dump(
            {
                "config": cfg,
                "binary": binary,
                "model": model,
                "host": host,
                "timestamp": datetime.now().isoformat(),
                "results": all_results,
                "baseline": baseline,
            },
            f,
            indent=2,
        )
    print(f"\nJSON results: {json_path}")
    print(f"Detailed log: {test_logger.path}")
    test_logger.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Concurrency probe for copilot-language-server"
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to experiment config JSON",
    )
    parser.add_argument(
        "--binary",
        default=None,
        help="Path to copilot-language-server binary (auto-discovered if omitted)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model ID (default: from config or gpt-4.1)",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Tag for log filenames (default: config filename stem)",
    )
    parser.add_argument(
        "--tests",
        nargs="+",
        default=None,
        help="Run only these test labels (default: all)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Host identifier for baseline tracking (default: hostname)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
