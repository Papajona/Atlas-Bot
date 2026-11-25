#!/usr/bin/env python3
"""Evaluate Atlas Native AI with a labelled corpus and auditable benchmark metrics.

The built-in corpus is synthetic regression coverage, not evidence of real-user quality.
Custom datasets are read locally; prompts are never copied into the report or sent over
the network. A real-user dataset must declare consent and anonymisation in its metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "native_ai_prompt_corpus.json"
ALLOWED_TIMEFRAMES = {"5m", "15m", "30m", "1h", "4h", "1d"}
ALLOWED_ASSETS = {"crypto", "forex", "commodity"}
ALLOWED_DIRECTIONS = {"LONG", "SHORT", "BOTH", "AUTO"}
ALLOWED_FAMILIES = {"TREND", "MOMENTUM", "BREAKOUT", "MEAN_REVERSION", "ENSEMBLE"}
EXPECTED_FIELDS = {
    "asset", "symbol", "strategy_family", "direction", "timeframes",
    "risk_per_trade", "stop_loss_atr", "take_profit_rr", "sessions",
}
WARNING_FIELDS = {"warns", "no_warns"}


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("native_ai_under_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Native AI module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _close(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return False
    try:
        left, right = float(actual), float(expected)
        return math.isfinite(left) and math.isfinite(right) and abs(left - right) < 1e-9
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_expected_label(field: str, value: Any) -> bool:
    """Validate corpus labels against the public AIStrategySpec field types/domains."""
    if field == "asset":
        return isinstance(value, str) and value in ALLOWED_ASSETS
    if field == "symbol":
        return isinstance(value, str) and 0 < len(value.strip()) <= 80
    if field == "strategy_family":
        return isinstance(value, str) and value in ALLOWED_FAMILIES
    if field == "direction":
        return isinstance(value, str) and value in ALLOWED_DIRECTIONS
    if field == "timeframes":
        return (
            isinstance(value, list)
            and 1 <= len(value) <= 5
            and all(isinstance(item, str) and item in ALLOWED_TIMEFRAMES for item in value)
            and len(set(value)) == len(value)
        )
    if field in {"risk_per_trade", "stop_loss_atr", "take_profit_rr"}:
        if value is None and field != "risk_per_trade":
            return True
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            if not math.isfinite(value):
                return False
        except (OverflowError, TypeError):
            return False
        if field == "risk_per_trade":
            return 0 < value <= 0.05
        return 0 < value <= 10
    if field == "sessions":
        return (
            isinstance(value, list)
            and len(value) <= 8
            and all(isinstance(item, str) and item.strip() for item in value)
        )
    return False


def _load_dataset(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    raw_bytes = path.read_bytes()
    fingerprint = hashlib.sha256(raw_bytes).hexdigest()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Dataset must be valid UTF-8 JSON: {exc}") from exc

    if isinstance(payload, list):
        cases = payload
        if path.resolve() == CORPUS.resolve():
            metadata = {
                "dataset_type": "self_authored_synthetic",
                "source": "tests/fixtures/native_ai_prompt_corpus.json",
                "provenance_status": "repository_fixture",
            }
        else:
            metadata = {
                "dataset_type": "user_supplied_unverified",
                "source": "user-supplied JSON array",
                "provenance_status": "not_attested",
            }
    elif isinstance(payload, dict):
        if payload.get("schema_version") != 1:
            raise ValueError("Dataset wrapper must declare schema_version=1.")
        metadata = payload.get("metadata")
        cases = payload.get("cases")
        if not isinstance(metadata, dict):
            raise ValueError("Dataset wrapper metadata must be an object.")
        dataset_type = metadata.get("dataset_type")
        source = metadata.get("source")
        if not isinstance(dataset_type, str) or dataset_type not in {"synthetic", "real_user_consented", "public_benchmark"}:
            raise ValueError("metadata.dataset_type must be synthetic, real_user_consented, or public_benchmark.")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("metadata.source must identify the dataset source.")
        if dataset_type == "real_user_consented":
            if metadata.get("consent_confirmed") is not True or metadata.get("anonymized") is not True:
                raise ValueError("Real-user datasets require consent_confirmed=true and anonymized=true.")
            metadata = {**metadata, "provenance_status": "submitter_attested"}
        elif dataset_type == "public_benchmark":
            if not isinstance(metadata.get("license"), str) or not metadata["license"].strip():
                raise ValueError("Public benchmark datasets require a non-empty metadata.license.")
            metadata = {**metadata, "provenance_status": "source_and_license_submitter_attested"}
        else:
            metadata = {**metadata, "provenance_status": "submitter_declared_synthetic"}
    else:
        raise ValueError("Dataset must be a JSON array or a schema_version=1 object.")

    if not isinstance(cases, list) or not cases:
        raise ValueError("Dataset must contain at least one case.")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"Case at index {index} must be an object.")
        case_id, prompt, expected = case.get("id"), case.get("prompt"), case.get("expect")
        if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", case_id):
            raise ValueError(f"Case at index {index} needs a safe ASCII id of at most 128 characters.")
        if case_id in seen:
            raise ValueError(f"Duplicate case id: {case_id}")
        seen.add(case_id)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 5000:
            raise ValueError(f"Case {case_id} needs a non-empty prompt of at most 5000 characters.")
        if not isinstance(expected, dict):
            raise ValueError(f"Case {case_id} needs an expect object.")
        kwargs = case.get("kwargs", {})
        if not isinstance(kwargs, dict):
            raise ValueError(f"Case {case_id} kwargs must be an object.")
        unknown = set(expected) - EXPECTED_FIELDS - WARNING_FIELDS
        if unknown:
            raise ValueError(f"Case {case_id} has unsupported expected fields: {sorted(unknown)}")
        for marker_field in WARNING_FIELDS:
            markers = expected.get(marker_field, [])
            if not isinstance(markers, list) or any(not isinstance(x, str) or not x for x in markers):
                raise ValueError(f"Case {case_id} {marker_field} must be a list of non-empty strings.")
        for field, value in expected.items():
            if field not in WARNING_FIELDS and not _valid_expected_label(field, value):
                raise ValueError(f"Case {case_id} has an invalid expected label for {field}.")
        has_field_label = any(key not in WARNING_FIELDS for key in expected)
        has_warning_label = any(bool(expected.get(key)) for key in WARNING_FIELDS)
        if not has_field_label and not has_warning_label:
            raise ValueError(
                f"Case {case_id} must include at least one scored field or non-empty warning assertion."
            )
    return cases, metadata, fingerprint


def _unwrap_prediction(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("strategy")
    return nested if isinstance(nested, dict) else value


def _valid_condition_list(value: Any, *, minimum: int, maximum: int) -> bool:
    """Reject empty/blank condition fields before Pydantic error aggregation.

    Pydantic's nested model errors can be reported at a parent/root location;
    checking each public condition collection here ensures a malformed exit list
    cannot be hidden by a separate entry-list validation error.
    """
    from app.strategy_ai import ALLOWED_TIMEFRAMES

    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        return False
    allowed = {"type", "rule", "timeframe"}
    for item in value:
        if not isinstance(item, dict) or set(item) - allowed:
            return False
        kind = item.get("type")
        rule = item.get("rule")
        timeframe = item.get("timeframe")
        if not isinstance(kind, str) or not 2 <= len(kind.strip()) <= 60:
            return False
        if not isinstance(rule, str) or not 3 <= len(rule.strip()) <= 500:
            return False
        if timeframe is not None and (
            not isinstance(timeframe, str) or timeframe not in ALLOWED_TIMEFRAMES
        ):
            return False
    return True


def _contract_failures(spec: dict[str, Any] | None) -> list[str]:
    """Validate both systems against the exact application output schema.

    Only safe field labels are reported; raw values and validation inputs are
    deliberately excluded so private strategy content cannot leak into reports.
    """
    if spec is None:
        return ["output was not a strategy object"]
    from pydantic import ValidationError
    from app.strategy_ai import AIStrategySpec

    condition_labels = {
        "entry_conditions": (1, 12, "entry conditions violate the Atlas strategy output contract"),
        "exit_conditions": (1, 8, "exit conditions violate the Atlas strategy output contract"),
    }
    condition_failures = [
        message
        for field, (minimum, maximum, message) in condition_labels.items()
        if not _valid_condition_list(spec.get(field), minimum=minimum, maximum=maximum)
    ]
    try:
        AIStrategySpec.model_validate(spec)
        return condition_failures
    except ValidationError as exc:
        labels = {
            "title": "title",
            "summary": "summary",
            "asset": "asset",
            "symbols": "symbols",
            "timeframes": "timeframes",
            "direction": "direction",
            "strategy_family": "strategy family",
            "entry_conditions": "entry conditions",
            "exit_conditions": "exit conditions",
            "risk_per_trade": "risk_per_trade",
            "stop_loss_atr": "stop_loss_atr",
            "take_profit_rr": "take_profit_rr",
            "sessions": "sessions",
            "assumptions": "assumptions",
            "validation_warnings": "validation warnings",
            "requires_backtest": "requires_backtest",
            "requires_walk_forward": "requires_walk_forward",
            "execution_mode": "execution_mode",
            "implementation_status": "implementation_status",
        }
        fields = {
            str(error.get("loc", ("",))[0])
            for error in exc.errors()
        }
        failures = list(condition_failures)
        for field in sorted(fields):
            if field in condition_labels:
                message = condition_labels[field][2]
                if message not in failures:
                    failures.append(message)
            elif field in labels:
                failures.append(f"{labels[field]} violate the Atlas strategy output contract")
            else:
                failures.append("output includes unsupported fields or invalid structure")
        return failures or ["output violates the Atlas strategy output contract"]

def _score_case(case: dict[str, Any], prediction: Any, *, check_contract: bool = True) -> tuple[list[dict[str, Any]], list[str]]:
    spec = _unwrap_prediction(prediction)
    expected = case["expect"]
    raw_warnings = spec.get("validation_warnings", []) if spec else []
    warnings = " | ".join(raw_warnings) if isinstance(raw_warnings, list) and all(
        isinstance(item, str) for item in raw_warnings
    ) else ""
    checks: list[dict[str, Any]] = []
    failures = _contract_failures(spec) if check_contract else ([] if spec is not None else ["output was not an object"])
    if spec is None:
        for key, wanted in expected.items():
            if key not in WARNING_FIELDS:
                checks.append({"field": key, "passed": False})
        for _ in expected.get("warns", []):
            checks.append({"field": "warning", "passed": False})
        for _ in expected.get("no_warns", []):
            checks.append({"field": "warning", "passed": False})
        return checks, failures

    for key, wanted in expected.items():
        if key in WARNING_FIELDS:
            continue
        symbols = spec.get("symbols")
        actual = (
            symbols[0]
            if key == "symbol" and isinstance(symbols, list) and symbols
            else None if key == "symbol"
            else spec.get(key)
        )
        passed = _close(actual, wanted) if isinstance(wanted, float) else actual == wanted
        checks.append({"field": key, "passed": passed})
        if not passed:
            # Never include caller labels or model output: either can contain prompt text.
            failures.append(f"field mismatch: {key}")
    for marker in expected.get("warns", []):
        passed = marker in warnings
        checks.append({"field": "warning", "passed": passed})
        if not passed:
            failures.append("required warning was not present")
    for marker in expected.get("no_warns", []):
        passed = marker not in warnings
        checks.append({"field": "warning", "passed": passed})
        if not passed:
            failures.append("forbidden warning was present")
    return checks, failures

def _score_predictions(cases: list[dict[str, Any]], predictions: dict[str, Any], *, check_contract: bool = True) -> dict[str, Any]:
    correct_checks = 0
    field_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
    passed_cases = valid_contracts = 0
    failures: dict[str, list[str]] = {}
    for case in cases:
        spec = _unwrap_prediction(predictions.get(case["id"]))
        checks, case_failures = _score_case(case, spec, check_contract=check_contract)
        for check in checks:
            field_stats[check["field"]]["total"] += 1
            if check["passed"]:
                correct_checks += 1
                field_stats[check["field"]]["correct"] += 1
        if spec is not None and not _contract_failures(spec):
            valid_contracts += 1
        if not case_failures:
            passed_cases += 1
        else:
            failures[case["id"]] = case_failures
    expectations = sum(
        len([key for key in case["expect"] if key not in WARNING_FIELDS])
        + len(case["expect"].get("warns", []))
        + len(case["expect"].get("no_warns", []))
        for case in cases
    )
    by_field = {
        field: {
            **counts,
            "accuracy": round(counts["correct"] / counts["total"], 6) if counts["total"] else None,
        }
        for field, counts in sorted(field_stats.items())
    }
    return {
        "cases": len(cases),
        "cases_passed": passed_cases,
        "case_exact_accuracy": round(passed_cases / len(cases), 6),
        "expectations": expectations,
        "expectations_met": correct_checks,
        "expectation_accuracy": round(correct_checks / expectations, 6) if expectations else None,
        "field_accuracy": by_field,
        "output_contract_valid": valid_contracts,
        "output_contract_rate": round(valid_contracts / len(cases), 6),
        "failures": failures,
    }


def _load_comparison(path: Path, fingerprint: str, cases: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read comparator results: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Comparator file must be a schema_version=1 object.")
    system = payload.get("system")
    if not isinstance(system, str) or not system.strip():
        raise ValueError("Comparator results require a non-empty system name.")
    if payload.get("dataset_sha256") != fingerprint:
        raise ValueError("Comparator dataset_sha256 does not match the evaluated prompt corpus.")
    rows = payload.get("predictions")
    if not isinstance(rows, list):
        raise ValueError("Comparator predictions must be a list.")
    predictions: dict[str, Any] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or "output" not in row:
            raise ValueError("Each comparator prediction requires an id and output.")
        if row["id"] in predictions:
            raise ValueError(f"Duplicate comparator prediction id: {row['id']}")
        predictions[row["id"]] = row["output"]
    expected_ids = {case["id"] for case in cases}
    if set(predictions) != expected_ids:
        missing, extra = sorted(expected_ids - set(predictions)), sorted(set(predictions) - expected_ids)
        raise ValueError(f"Comparator case IDs must match exactly; missing={missing}, extra={extra}.")
    return system, predictions


def evaluate(module_path: Path, corpus_path: Path = CORPUS, comparison_path: Path | None = None) -> dict[str, Any]:
    module = _load(module_path)
    cases, metadata, fingerprint = _load_dataset(corpus_path)
    native_predictions: dict[str, Any] = {}
    generation_failures: dict[str, str] = {}
    for case in cases:
        try:
            native_predictions[case["id"]] = module.compose_strategy_spec(
                prompt=case["prompt"], name="Native AI benchmark", **(case.get("kwargs") or {})
            )
        except Exception as exc:
            native_predictions[case["id"]] = None
            # Exception messages may contain submitted prompts or private annotations.
            generation_failures[case["id"]] = type(exc).__name__
    native = _score_predictions(cases, native_predictions)
    report: dict[str, Any] = {
        **native,
        "generation_failures": generation_failures,
        "dataset": {
            "dataset_type": metadata.get("dataset_type"),
            "source": (
                "repository_fixture"
                if corpus_path.resolve() == CORPUS.resolve()
                else "user_supplied_dataset"
            ),
            "provenance_status": metadata.get("provenance_status", "not_attested"),
            "cases": len(cases),
            "sha256": fingerprint,
            "raw_prompts_in_report": False,
        },
        "benchmark_scope": (
            "self-authored synthetic regression corpus; not an independent benchmark"
            if corpus_path.resolve() == CORPUS.resolve()
            else "custom labelled corpus; quality claims are limited to its declared provenance"
        ),
        "network_calls": 0,
        "comparison": {"status": "NOT_RUN", "reason": "No comparator result file was supplied."},
    }
    if comparison_path is not None:
        system, predictions = _load_comparison(comparison_path, fingerprint, cases)
        report["comparison"] = {
            "status": "SCORED",
            "system": system,
            "metrics": _score_predictions(cases, predictions),
        }
    return report

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--module-path", type=Path, default=ROOT / "app" / "native_ai.py")
    parser.add_argument("--dataset", type=Path, default=CORPUS, help="Labelled JSON corpus; custom prompts remain local.")
    parser.add_argument("--comparison-results", type=Path, help="Paired predictions from another system, scored against the same labels.")
    parser.add_argument("--output", type=Path, help="Optional path for the JSON report.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = evaluate(args.module_path, args.dataset, args.comparison_results)
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if args.json:
        print(rendered)
    else:
        print(f"cases passed: {report['cases_passed']}/{report['cases']}")
        print(f"expectations met: {report['expectations_met']}/{report['expectations']}")
        print(f"case exact accuracy: {report['case_exact_accuracy']:.1%}")
        print(f"expectation accuracy: {report['expectation_accuracy']:.1%}" if report["expectation_accuracy"] is not None else "expectation accuracy: n/a")
        print(f"output contract valid: {report['output_contract_valid']}/{report['cases']}")
        print(f"dataset: {report['dataset']['dataset_type']} ({report['dataset']['provenance_status']})")
        print(report["benchmark_scope"])
        for case_id, failures in report["failures"].items():
            print(f"FAIL {case_id}: " + "; ".join(failures))
        if report["comparison"]["status"] == "SCORED":
            paired = report["comparison"]["metrics"]
            print(
                f"comparator {report['comparison']['system']} exact accuracy: "
                f"{paired['case_exact_accuracy']:.1%}"
            )
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
