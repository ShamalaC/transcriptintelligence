#!/usr/bin/env python3
"""
Eval harness for Transcript Intelligence tool functions.

Tests each deterministic tool (classify_call_type, classify_category,
analyze_sentiment, extract_issues, route_escalation) against ground-truth
cases in eval_cases.json. Designed to run in CI as a regression gate.

Usage:
    python scripts/eval/eval_runner.py              # all tools
    python scripts/eval/eval_runner.py --tool classify_call_type
    python scripts/eval/eval_runner.py --fail-fast
"""
import sys, os, json, argparse, time
from typing import Any
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Path setup: import tool implementations directly from Lambda source
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "lambda", "shared", "python"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "lambda", "agent_actions"))

# agent_actions/handler.py references env vars only in save_record (DynamoDB).
# The 5 stateless tools don't touch any env var, so no mocking needed.
os.environ.setdefault("RECORDS_TABLE",    "ti-records-eval")
os.environ.setdefault("LINEAGE_TABLE",    "ti-lineage-eval")
os.environ.setdefault("ESCALATION_TOPIC", "")
os.environ.setdefault("MODEL_SONNET",     "eval-mode")

import handler as agent_handler  # noqa: E402 (after sys.path setup)

# ---------------------------------------------------------------------------
# Tool dispatch table (stateless tools only — save_record requires DynamoDB)
# ---------------------------------------------------------------------------
TOOL_FNS = {
    "classify_call_type": lambda inp: agent_handler.classify_call_type(**inp),
    "classify_category":  lambda inp: agent_handler.classify_category(**inp),
    "analyze_sentiment":  lambda inp: agent_handler.analyze_sentiment(**inp),
    "extract_issues":     lambda inp: agent_handler.extract_issues(**inp),
    "route_escalation":   lambda inp: agent_handler.route_escalation(**inp),
}

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    tool:    str
    case_id: str
    passed:  bool
    note:    str = ""
    actual:  dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    error:   str = ""
    latency_ms: float = 0.0


def _check(actual: dict, expected: dict) -> tuple[bool, str]:
    """
    Validates actual output against expected spec.

    Expected keys use dot-notation for nested access:
        "issues[0].severity" -> actual["issues"][0]["severity"]
    """
    failures = []
    for key, exp_val in expected.items():
        if "[" in key:
            # e.g. "issues[0].severity"
            parts = key.replace("]", "").replace("[", ".").split(".")
            actual_val = actual
            for p in parts:
                if isinstance(actual_val, list):
                    actual_val = actual_val[int(p)]
                else:
                    actual_val = actual_val.get(p)
        else:
            actual_val = actual.get(key)

        if actual_val != exp_val:
            failures.append(f"{key}: expected={exp_val!r} actual={actual_val!r}")

    return (len(failures) == 0), "; ".join(failures)


def run_case(tool: str, case: dict) -> CaseResult:
    fn     = TOOL_FNS[tool]
    inp    = case["input"]
    exp    = case["expected"]
    note   = case.get("note", "")
    cid    = case["id"]

    t0 = time.perf_counter()
    try:
        actual = fn(inp)
    except Exception as exc:
        return CaseResult(tool=tool, case_id=cid, passed=False,
                          note=note, expected=exp, error=str(exc))
    latency_ms = (time.perf_counter() - t0) * 1000

    passed, fail_msg = _check(actual, exp)
    return CaseResult(tool=tool, case_id=cid, passed=passed,
                      note=note, actual=actual, expected=exp,
                      error=fail_msg if not passed else "",
                      latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _color(text: str, code: str) -> str:
    """ANSI colour — skipped when stdout is not a tty."""
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def print_report(results: list[CaseResult]) -> bool:
    """Prints human-readable report. Returns True if all passed."""
    by_tool: dict[str, list[CaseResult]] = {}
    for r in results:
        by_tool.setdefault(r.tool, []).append(r)

    all_passed = True
    total_pass = total_fail = 0

    for tool, tool_results in by_tool.items():
        n_pass = sum(1 for r in tool_results if r.passed)
        n_fail = len(tool_results) - n_pass
        total_pass += n_pass
        total_fail += n_fail
        bar_color = "32" if n_fail == 0 else "31"
        print(f"\n{_color(tool, '1')}  {_color(f'{n_pass}/{len(tool_results)} passed', bar_color)}")

        for r in tool_results:
            icon    = _color("✓", "32") if r.passed else _color("✗", "31")
            latency = f"{r.latency_ms:.1f}ms"
            print(f"  {icon} [{r.case_id}] {r.note or '':<55} {latency}")
            if not r.passed:
                all_passed = False
                if r.error:
                    print(f"       {_color('FAIL', '31')}: {r.error}")
                if r.actual:
                    print(f"       actual  : {json.dumps(r.actual, default=str)}")
                    print(f"       expected: {json.dumps(r.expected, default=str)}")

    total = total_pass + total_fail
    accuracy = total_pass / total * 100 if total else 0
    verdict  = _color("ALL PASS", "32") if all_passed else _color("FAILURES DETECTED", "31")
    print(f"\n{'─'*60}")
    print(f"Result : {verdict}")
    print(f"Score  : {total_pass}/{total} ({accuracy:.0f}%)")
    print(f"{'─'*60}")

    return all_passed


def save_report(results: list[CaseResult], path: str) -> None:
    """Writes JSON report suitable for CI artifact upload or Braintrust ingestion."""
    report = {
        "summary": {
            "total":  len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
        },
        "cases": [
            {
                "tool":       r.tool,
                "case_id":    r.case_id,
                "passed":     r.passed,
                "note":       r.note,
                "actual":     r.actual,
                "expected":   r.expected,
                "error":      r.error,
                "latency_ms": round(r.latency_ms, 2),
            }
            for r in results
        ],
    }
    with open(path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nJSON report written to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="TI tool eval runner")
    parser.add_argument("--tool",       help="Run only this tool")
    parser.add_argument("--fail-fast",  action="store_true", help="Exit on first failure")
    parser.add_argument("--json-out",   default="", help="Write JSON report to this path")
    args = parser.parse_args()

    cases_path = os.path.join(os.path.dirname(__file__), "eval_cases.json")
    with open(cases_path) as f:
        all_cases: dict[str, list[dict]] = json.load(f)

    if args.tool:
        if args.tool not in TOOL_FNS:
            print(f"Unknown tool '{args.tool}'. Available: {list(TOOL_FNS)}")
            return 1
        all_cases = {args.tool: all_cases[args.tool]}

    results: list[CaseResult] = []
    for tool, cases in all_cases.items():
        if tool not in TOOL_FNS:
            continue
        for case in cases:
            r = run_case(tool, case)
            results.append(r)
            if args.fail_fast and not r.passed:
                print(f"[FAIL-FAST] {r.case_id}: {r.error}")
                return 1

    all_passed = print_report(results)

    if args.json_out:
        save_report(results, args.json_out)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
