#!/usr/bin/env python3
"""Evaluate Atlas Native AI against a labelled, self-authored regression corpus.

This is a deterministic parser regression check, not an independent real-user or
cross-system trading-quality benchmark. No network calls or external model are used.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "native_ai_prompt_corpus.json"


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("native_ai_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Native AI module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _close(actual, expected):
    try:
        return abs(float(actual) - float(expected)) < 1e-9
    except (TypeError, ValueError, OverflowError):
        return False


def check_case(compose, case):
    try:
        actual = compose(prompt=case["prompt"], name="Native AI corpus", **(case.get("kwargs") or {}))
    except Exception as exc:
        return [f"raised {type(exc).__name__}: {exc}"]
    expected = case.get("expect") or {}
    warnings = " | ".join(actual.get("validation_warnings") or [])
    failures = []
    for key, want in expected.items():
        if key == "symbol":
            got = (actual.get("symbols") or [None])[0]
        elif key in {"warns", "no_warns"}:
            continue
        else:
            got = actual.get(key)
        passed = _close(got, want) if isinstance(want, float) else got == want
        if not passed:
            failures.append(f"{key}: expected {want!r}, got {got!r}")
    for marker in expected.get("warns", []):
        if marker not in warnings:
            failures.append(f"missing warning containing {marker!r}")
    for marker in expected.get("no_warns", []):
        if marker in warnings:
            failures.append(f"unexpected warning containing {marker!r}")
    return failures


def evaluate(module_path: Path):
    module = _load(module_path)
    cases = json.loads(CORPUS.read_text(encoding="utf-8"))
    results = {case["id"]: check_case(module.compose_strategy_spec, case) for case in cases}
    failed = sum(len(items) for items in results.values())
    total = sum(len(case.get("expect", {})) for case in cases)
    return {
        "cases": len(cases),
        "cases_passed": sum(not items for items in results.values()),
        "expectations": total,
        "expectations_met": total - failed,
        "failures": {key: value for key, value in results.items() if value},
        "benchmark_scope": "self-authored regression corpus; not an independent benchmark",
        "network_calls": 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--module-path", type=Path, default=ROOT / "app" / "native_ai.py")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(args.module_path)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"cases passed: {report['cases_passed']}/{report['cases']}")
        print(f"expectations met: {report['expectations_met']}/{report['expectations']}")
        for case_id, failures in report["failures"].items():
            print(f"FAIL {case_id}: " + "; ".join(failures))
        print(report["benchmark_scope"])
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
