"""
Probe: Does CoT injection improve code correctness?

Asks the model to implement merge_intervals, captures the code,
runs it against a test suite, then injects a reflection and captures
the revised code. Compares pass rates.

Usage:
    Requires the proxy running: acp-proxy --port 8765
    python tmp/probe_code_correctness.py --config tmp/configs/code_correctness.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import TypedDict

import httpx

PROXY_BASE = "http://127.0.0.1:{port}/v1"


class IterationResult(TypedDict):
    iteration: int
    variant: str
    r1_passed: int
    r2_passed: int | None
    total: int
    delta: int | None

# --- Test suites keyed by config filename stem ---

MERGE_INTERVALS_TESTS: list[tuple[str, list, object]] = [
    ("basic_overlap", [[1, 3], [2, 6], [8, 10], [15, 18]], [[1, 6], [8, 10], [15, 18]]),
    ("no_overlap", [[1, 2], [3, 4], [5, 6]], [[1, 2], [3, 4], [5, 6]]),
    ("all_overlap", [[1, 4], [2, 5], [3, 6]], [[1, 6]]),
    ("empty_input", [], []),
    ("single_interval", [[1, 5]], [[1, 5]]),
    ("touching_boundaries", [[1, 2], [2, 3]], [[1, 3]]),
    ("contained_interval", [[1, 10], [3, 5]], [[1, 10]]),
    ("unsorted_input", [[3, 4], [1, 2], [5, 6]], [[1, 2], [3, 4], [5, 6]]),
    ("duplicate_intervals", [[1, 3], [1, 3]], [[1, 3]]),
    ("single_point_intervals", [[1, 1], [2, 2]], [[1, 1], [2, 2]]),
    ("single_point_touching", [[1, 1], [1, 2]], [[1, 2]]),
    ("reverse_sorted", [[5, 6], [3, 4], [1, 2]], [[1, 2], [3, 4], [5, 6]]),
    ("complex_overlap", [[1, 4], [0, 4]], [[0, 4]]),
    ("many_overlapping", [[1, 2], [1, 3], [1, 4], [1, 5]], [[1, 5]]),
    ("negative_numbers", [[-3, -1], [-2, 0], [1, 3]], [[-3, 0], [1, 3]]),
]

# eval_expr tests use (label, input_str, expected_float_or_exception)
# "ValueError" as expected means we expect ValueError to be raised
EVAL_EXPR_TESTS: list[tuple[str, str, object]] = [
    ("simple_add", "1+2", 3.0),
    ("simple_sub", "5-3", 2.0),
    ("simple_mul", "3*4", 12.0),
    ("simple_div", "10/4", 2.5),
    ("precedence_mul_add", "2+3*4", 14.0),
    ("precedence_div_sub", "10-6/3", 8.0),
    ("parens_basic", "(2+3)*4", 20.0),
    ("nested_parens", "((1+2))*3", 9.0),
    ("deep_parens", "((2+3)*(4-1))", 15.0),
    ("whitespace", " 2 + 3 * 4 ", 14.0),
    ("decimal_numbers", "1.5+2.5", 4.0),
    ("unary_minus_start", "-3+5", 2.0),
    ("unary_minus_after_op", "2*-3", -6.0),
    ("unary_minus_after_paren", "(-3+5)*2", 4.0),
    ("division_by_zero", "1/0", "ValueError"),
    ("empty_string", "", "ValueError"),
    ("single_number", "42", 42.0),
    ("complex_expression", "3+4*2/(1-5)", 1.0),
    ("multiple_operations", "2+3+4+5", 14.0),
    ("chained_multiply", "2*3*4", 24.0),
    ("subtraction_chain", "10-3-2", 5.0),
    ("paren_unary", "-(3+2)", -5.0),
    ("double_negative", "--5", 5.0),
]

# Map config stem to (function_name, test_cases)
# Prefix matching: code_eval_expr_targeted matches code_eval_expr
TEST_SUITES = {
    "code_correctness": ("merge_intervals", MERGE_INTERVALS_TESTS),
    "code_eval_expr": ("eval_expr", EVAL_EXPR_TESTS),
}


def resolve_test_suite(config_stem: str) -> tuple[str, list]:
    """Find the test suite for a config, with prefix matching."""
    if config_stem in TEST_SUITES:
        return TEST_SUITES[config_stem]
    # Try prefix match
    for key in sorted(TEST_SUITES.keys(), key=len, reverse=True):
        if config_stem.startswith(key):
            return TEST_SUITES[key]
    raise SystemExit(
        f"No test suite for config '{config_stem}'. Available: {list(TEST_SUITES.keys())}"
    )


def extract_python_code(response: str) -> str:
    """Extract Python code from a model response.

    Tries in order:
    1. Fenced code block (```python ... ```)
    2. Fenced code block (``` ... ```)
    3. Lines that look like Python (start with def, if, for, return, etc.)
    4. The entire response as a fallback
    """
    # Try fenced python block
    m = re.search(r"```python\s*\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Try any fenced block
    m = re.search(r"```\s*\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Try to find a function definition
    lines = response.split("\n")
    code_lines = []
    in_function = False
    for line in lines:
        if line.strip().startswith("def "):
            in_function = True
        if in_function:
            code_lines.append(line)
            # Stop if we hit a blank line after getting some code
            if line.strip() == "" and len(code_lines) > 3:
                # Check if the next non-blank line is not indented
                # (indicating end of function)
                pass

    if code_lines:
        return "\n".join(code_lines).strip()

    return response.strip()


def run_tests(code: str, fn_name: str, test_cases: list[tuple]) -> list[dict]:
    """Execute the extracted code and run all test cases against it.

    Returns a list of {label, passed, input, expected, actual, error} dicts.
    """
    import copy

    results = []

    # Try to compile and exec the code
    namespace = {}
    try:
        exec(code, namespace)
    except Exception as e:
        for label, inp, expected in test_cases:
            results.append(
                {
                    "label": label,
                    "passed": False,
                    "input": inp,
                    "expected": expected,
                    "actual": None,
                    "error": f"compile error: {e}",
                }
            )
        return results

    fn = namespace.get(fn_name)
    if fn is None:
        for label, inp, expected in test_cases:
            results.append(
                {
                    "label": label,
                    "passed": False,
                    "input": inp,
                    "expected": expected,
                    "actual": None,
                    "error": f"function '{fn_name}' not found in code",
                }
            )
        return results

    for label, inp, expected in test_cases:
        try:
            actual = fn(copy.deepcopy(inp))
            if expected == "ValueError":
                # Expected an exception but didn't get one
                results.append(
                    {
                        "label": label,
                        "passed": False,
                        "input": inp,
                        "expected": "ValueError",
                        "actual": actual,
                        "error": "expected ValueError but got a result",
                    }
                )
            else:
                # For floats, use approximate comparison
                if isinstance(expected, float) and isinstance(actual, (int, float)):
                    passed = abs(float(actual) - expected) < 1e-9
                else:
                    passed = actual == expected
                results.append(
                    {
                        "label": label,
                        "passed": passed,
                        "input": inp,
                        "expected": expected,
                        "actual": actual,
                        "error": None,
                    }
                )
        except ValueError:
            if expected == "ValueError":
                results.append(
                    {
                        "label": label,
                        "passed": True,
                        "input": inp,
                        "expected": "ValueError",
                        "actual": "ValueError raised",
                        "error": None,
                    }
                )
            else:
                results.append(
                    {
                        "label": label,
                        "passed": False,
                        "input": inp,
                        "expected": expected,
                        "actual": None,
                        "error": "unexpected ValueError",
                    }
                )
        except ZeroDivisionError:
            if expected == "ValueError":
                # Close enough — raised an error, just wrong type
                results.append(
                    {
                        "label": label,
                        "passed": False,
                        "input": inp,
                        "expected": "ValueError",
                        "actual": "ZeroDivisionError",
                        "error": "raised ZeroDivisionError instead of ValueError",
                    }
                )
            else:
                results.append(
                    {
                        "label": label,
                        "passed": False,
                        "input": inp,
                        "expected": expected,
                        "actual": None,
                        "error": "ZeroDivisionError",
                    }
                )
        except Exception as e:
            results.append(
                {
                    "label": label,
                    "passed": False,
                    "input": inp,
                    "expected": expected,
                    "actual": None,
                    "error": str(e),
                }
            )

    return results

    # Find the function
    fn = namespace.get("merge_intervals")
    if fn is None:
        for label, inp, expected in test_cases:
            results.append(
                {
                    "label": label,
                    "passed": False,
                    "input": inp,
                    "expected": expected,
                    "actual": None,
                    "error": "function 'merge_intervals' not found in code",
                }
            )
        return results

    # Run each test
    for label, inp, expected in test_cases:
        try:
            # Deep copy input to avoid mutation
            import copy

            actual = fn(copy.deepcopy(inp))
            passed = actual == expected
            results.append(
                {
                    "label": label,
                    "passed": passed,
                    "input": inp,
                    "expected": expected,
                    "actual": actual,
                    "error": None,
                }
            )
        except Exception as e:
            results.append(
                {
                    "label": label,
                    "passed": False,
                    "input": inp,
                    "expected": expected,
                    "actual": None,
                    "error": str(e),
                }
            )

    return results


class TestLogger:
    """Logs every interaction verbatim to a timestamped file."""

    def __init__(self, log_dir: str, tag: str) -> None:
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(log_dir, f"code_{ts}_{tag}.log")
        self._f = open(self.path, "w")

    def write(self, text: str) -> None:
        self._f.write(text + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


def send_completion(
    client: httpx.Client, model: str, messages: list[dict], base_url: str
) -> str:
    resp = client.post(
        f"{base_url}/chat/completions",
        json={"model": model, "messages": messages, "stream": False},
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def log_test_results(logger: TestLogger, label: str, results: list[dict]):
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    logger.write(f"  Test results: {passed}/{total} passed")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        logger.write(f"    [{status}] {r['label']}")
        if not r["passed"]:
            logger.write(f"           input:    {r['input']}")
            logger.write(f"           expected: {r['expected']}")
            logger.write(f"           actual:   {r['actual']}")
            if r["error"]:
                logger.write(f"           error:    {r['error']}")
    logger.write("")


def run_single_iteration(
    http: httpx.Client,
    model: str,
    base_url: str,
    question: str,
    fn_name: str,
    test_cases: list[tuple[str, object, object]],
    injections: list[dict[str, str]],
    iteration: int,
    logger: TestLogger,
) -> list[IterationResult]:
    """Run one full iteration: baseline + all injection variants.

    Each iteration prefixes the question with a unique tag to force a fresh
    ACP session (different first-user-message hash per iteration).
    """
    results: list[IterationResult] = []

    # --- Baseline (R1 only, no injection) ---
    # Each variant gets a unique prefix to force a distinct ACP session
    q_baseline = f"[run {iteration}.baseline] {question}"
    logger.write(f"--- iteration {iteration} | baseline ---")
    messages = [{"role": "user", "content": q_baseline}]

    t0 = time.monotonic()
    response = send_completion(http, model, messages, base_url)
    elapsed = time.monotonic() - t0

    logger.write(f"Response ({len(response)} chars, {elapsed:.1f}s):")
    logger.write(">>>")
    logger.write(response)
    logger.write("<<<")

    code = extract_python_code(response)
    tr = run_tests(code, fn_name, test_cases)
    log_test_results(logger, f"i{iteration}_baseline", tr)
    passed = sum(1 for r in tr if r["passed"])

    results.append(
        {
            "iteration": iteration,
            "variant": "baseline",
            "r1_passed": passed,
            "r2_passed": None,
            "total": len(test_cases),
            "delta": None,
        }
    )

    # --- Injection variants ---
    for inj in injections:
        label = inj["label"]
        is_control = not inj.get("partial_assistant")

        # Unique prefix per variant per iteration = isolated ACP session
        q = f"[run {iteration}.{label}] {question}"

        logger.write(f"--- iteration {iteration} | {label} ---")

        # R1: fresh question in its own session
        messages_r1 = [{"role": "user", "content": q}]
        t0 = time.monotonic()
        response_1 = send_completion(http, model, messages_r1, base_url)
        t1 = time.monotonic()

        logger.write(f"R1 ({len(response_1)} chars, {t1 - t0:.1f}s):")
        logger.write(">>>")
        logger.write(response_1)
        logger.write("<<<")

        code_1 = extract_python_code(response_1)
        tr1 = run_tests(code_1, fn_name, test_cases)
        log_test_results(logger, f"i{iteration}_{label}_r1", tr1)
        r1_passed = sum(1 for r in tr1 if r["passed"])

        if is_control:
            # no_injection control: no R2
            results.append(
                {
                    "iteration": iteration,
                    "variant": label,
                    "r1_passed": r1_passed,
                    "r2_passed": None,
                    "total": len(test_cases),
                    "delta": None,
                }
            )
            continue

        # R2: packed injection
        packed = f"{inj['closing']}\n\nAssistant: {inj['partial_assistant']}\n\nUser: {inj['nudge']}"
        messages_r2 = [
            {"role": "user", "content": q},
            {"role": "assistant", "content": response_1},
            {"role": "user", "content": packed},
        ]

        logger.write("Packed injection:")
        logger.write(">>>")
        logger.write(packed)
        logger.write("<<<")

        t2 = time.monotonic()
        response_2 = send_completion(http, model, messages_r2, base_url)
        t3 = time.monotonic()

        logger.write(f"R2 ({len(response_2)} chars, {t3 - t2:.1f}s):")
        logger.write(">>>")
        logger.write(response_2)
        logger.write("<<<")

        code_2 = extract_python_code(response_2)
        tr2 = run_tests(code_2, fn_name, test_cases)
        log_test_results(logger, f"i{iteration}_{label}_r2", tr2)
        r2_passed = sum(1 for r in tr2 if r["passed"])

        delta = r2_passed - r1_passed
        results.append(
            {
                "iteration": iteration,
                "variant": label,
                "r1_passed": r1_passed,
                "r2_passed": r2_passed,
                "total": len(test_cases),
                "delta": delta,
            }
        )

    return results


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Code correctness probe")
    parser.add_argument("--config", required=True)
    parser.add_argument("--model", default="gpt-4.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--variants", nargs="*", default=None)
    parser.add_argument("-n", type=int, default=1, help="Number of iterations")
    args = parser.parse_args()

    cfg = load_config(args.config)
    question = cfg["question"]
    config_stem = os.path.splitext(os.path.basename(args.config))[0]
    tag = args.tag or config_stem

    fn_name, test_cases = resolve_test_suite(config_stem)

    base_url = PROXY_BASE.format(port=args.port)
    log_dir = os.path.join("logs", "code_correctness")
    logger = TestLogger(log_dir, tag)

    http = httpx.Client()
    try:
        resp = http.get(f"{base_url}/models", timeout=5.0)
        resp.raise_for_status()
    except httpx.ConnectError:
        print(f"Cannot connect to proxy at {base_url}")
        sys.exit(1)

    model = args.model
    n = args.n
    logger.write("=== Code Correctness Probe ===")
    logger.write(f"Timestamp: {datetime.now().isoformat()}")
    logger.write(f"Config: {args.config}")
    logger.write(f"Model: {model}")
    logger.write(f"Iterations: {n}")
    logger.write(f"Question: {question}")
    logger.write(f"Function: {fn_name}")
    logger.write(f"Test cases: {len(test_cases)}")
    logger.write("")

    injections = cfg["injections"]
    if args.variants:
        injections = [i for i in injections if i["label"] in args.variants]

    all_results = []
    for i in range(1, n + 1):
        print(f"\n{'=' * 60}")
        print(f"ITERATION {i}/{n}")
        print(f"{'=' * 60}")
        logger.write(f"\n{'=' * 60}")
        logger.write(f"ITERATION {i}/{n}")
        logger.write(f"{'=' * 60}")

        iter_results = run_single_iteration(
            http,
            model,
            base_url,
            question,
            fn_name,
            test_cases,
            injections,
            i,
            logger,
        )
        all_results.extend(iter_results)

        # Print iteration summary
        for r in iter_results:
            v = r["variant"]
            if r["r2_passed"] is None:
                print(f"  {v:30s}  R1={r['r1_passed']}/{r['total']}")
            else:
                d = r["delta"]
                assert d is not None
                tag_str = "IMPROVED" if d > 0 else ("SAME" if d == 0 else "REGRESSED")
                print(
                    f"  {v:30s}  R1={r['r1_passed']}/{r['total']}  R2={r['r2_passed']}/{r['total']}  delta={d:+d} {tag_str}"
                )

    # --- Aggregate statistics ---
    json_path = logger.path.replace(".log", ".json")
    with open(json_path, "w") as f:
        json.dump(all_results, f, indent=2)

    # Compute per-variant statistics
    from collections import defaultdict

    stats: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {
            "r1_scores": [],
            "r2_scores": [],
            "deltas": [],
        }
    )
    for r in all_results:
        v = r["variant"]
        stats[v]["r1_scores"].append(r["r1_passed"])
        if r["r2_passed"] is not None:
            stats[v]["r2_scores"].append(r["r2_passed"])
            assert r["delta"] is not None
            stats[v]["deltas"].append(r["delta"])

    total = len(test_cases)
    logger.write(f"\n{'=' * 72}")
    logger.write(f"AGGREGATE STATISTICS ({n} iterations, {total} tests each)")
    logger.write(f"{'=' * 72}")
    print(f"\n{'=' * 72}")
    print(f"AGGREGATE STATISTICS ({n} iterations, {total} tests each)")
    print(f"{'=' * 72}")

    for v in sorted(stats.keys()):
        s = stats[v]
        r1 = s["r1_scores"]
        r1_mean = sum(r1) / len(r1)
        r1_min, r1_max = min(r1), max(r1)

        line = f"  {v:30s}  R1: mean={r1_mean:.1f}/{total} min={r1_min} max={r1_max}"

        if s["r2_scores"]:
            r2 = s["r2_scores"]
            r2_mean = sum(r2) / len(r2)
            r2_min, r2_max = min(r2), max(r2)
            deltas = s["deltas"]
            d_mean = sum(deltas) / len(deltas)
            improved = sum(1 for d in deltas if d > 0)
            same = sum(1 for d in deltas if d == 0)
            regressed = sum(1 for d in deltas if d < 0)
            line += (
                f"  R2: mean={r2_mean:.1f}/{total} min={r2_min} max={r2_max}"
                f"  delta: mean={d_mean:+.1f}  improved={improved} same={same} regressed={regressed}"
            )

        logger.write(line)
        print(line)

    logger.close()
    print(f"\nLog: {logger.path}")
    print(f"JSON: {json_path}")
    http.close()


if __name__ == "__main__":
    main()
