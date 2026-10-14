#!/usr/bin/env python3
"""Provenance-locked chronological OANDA walk-forward evaluation.

This is deliberately not a data-capture tool. It will only evaluate a dataset when its
raw-response/CSV hashes and request identity match a separately archived trusted reference
manifest, and when a complete experiment ledger supplies every variant's annualized Sharpe.
A green execution means the configured promotion gate passed; missing evidence fails closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from app.research_validation import oos_promotion_gate
from app.trading_core import ai_walk_forward_backtest
from scripts.verify_oanda_research_dataset import verify_dataset

IDENTITY_FIELDS = (
    "schema_version",
    "provider",
    "environment",
    "base_url",
    "endpoint",
    "instrument",
    "granularity",
    "price_component",
    "request_parameters",
    "completed_row_count",
    "first_completed_timestamp_utc",
    "last_completed_timestamp_utc",
    "raw_response_sha256",
    "normalized_csv_sha256",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value

def finite_json(value: Any) -> Any:
    """Convert NumPy scalars and non-finite floats into strict JSON-safe primitives."""
    import numpy as np

    if isinstance(value, dict):
        return {str(key): finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(item) for item in value]
    if isinstance(value, np.generic):
        return finite_json(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def verify_reference_identity(dataset_dir: Path, reference_path: Path, expected_rows: int) -> dict[str, Any]:
    actual_path = dataset_dir / "manifest.json"
    if actual_path.resolve() == reference_path.resolve():
        raise ValueError("Reference manifest must be a separately archived trusted copy, not the candidate manifest itself")
    actual = load_json(actual_path)
    reference = load_json(reference_path)
    missing = [key for key in IDENTITY_FIELDS if key not in actual or key not in reference]
    if missing:
        raise ValueError("Dataset/reference manifest missing identity fields: " + ", ".join(missing))
    differences = [key for key in IDENTITY_FIELDS if actual[key] != reference[key]]
    if differences:
        raise ValueError("Candidate dataset does not match the archived original identity fields: " + ", ".join(differences))
    if actual.get("provider") != "OANDA v20" or actual.get("environment") != "practice":
        raise ValueError("Research evidence must originate from OANDA v20 practice, not a live trading account")
    if actual.get("price_component") != "M":
        raise ValueError("The expected dataset must use OANDA mid candles (price=M)")
    request = actual.get("request_parameters")
    if not isinstance(request, dict) or request.get("price") != "M":
        raise ValueError("Manifest request parameters do not prove use of OANDA mid candles")
    if int(actual.get("completed_row_count", -1)) != expected_rows:
        raise ValueError(f"Expected exactly {expected_rows} completed candles")
    if actual.get("row_count_matches_expectation") is False:
        raise ValueError("Capture manifest records a row-count mismatch")
    return {
        "dataset_manifest_sha256": sha256_file(actual_path),
        "trusted_reference_manifest_sha256": sha256_file(reference_path),
        "raw_response_sha256": actual["raw_response_sha256"],
        "normalized_csv_sha256": actual["normalized_csv_sha256"],
        "instrument": actual["instrument"],
        "granularity": actual["granularity"],
        "requested_from": request.get("from"),
        "requested_to": request.get("to"),
        "rows": int(actual["completed_row_count"]),
        "first_timestamp_utc": actual["first_completed_timestamp_utc"],
        "last_timestamp_utc": actual["last_completed_timestamp_utc"],
    }


def load_complete_trial_history(
    path: Path,
    minimum_variants: int,
    experiment_archive_path: Path,
) -> tuple[list[float], dict[str, Any]]:
    """Load every tested variant and bind the ledger to an immutable experiment archive."""
    payload = load_json(path)
    if payload.get("schema_version") != 1:
        raise ValueError("Trial ledger schema_version must be 1")
    if payload.get("complete") is not True or payload.get("all_variants_included") is not True:
        raise ValueError("Trial ledger must attest complete=true and all_variants_included=true")
    claimed_archive_hash = str(payload.get("experiment_archive_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", claimed_archive_hash):
        raise ValueError("Trial ledger must include a SHA-256 hash for the full experiment archive")
    if not experiment_archive_path.is_file():
        raise ValueError("Full experiment archive is missing")
    observed_archive_hash = sha256_file(experiment_archive_path)
    if observed_archive_hash != claimed_archive_hash:
        raise ValueError("Experiment archive SHA-256 does not match the complete trial ledger")
    experiment_id = str(payload.get("experiment_id") or "").strip()
    variants = payload.get("variants")
    if not experiment_id or not isinstance(variants, list) or len(variants) < minimum_variants:
        raise ValueError(f"Trial ledger must identify an experiment and list at least {minimum_variants} variants")
    identifiers: set[str] = set()
    sharpes: list[float] = []
    for item in variants:
        if not isinstance(item, dict):
            raise ValueError("Every trial entry must be an object")
        variant_id = str(item.get("variant_id") or "").strip()
        if not variant_id or variant_id in identifiers:
            raise ValueError("Trial ledger variant_id values must be present and unique")
        identifiers.add(variant_id)
        raw = item.get("annualized_sharpe")
        if isinstance(raw, bool):
            raise ValueError(f"Invalid annualized_sharpe for variant {variant_id}")
        try:
            sharpe = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid annualized_sharpe for variant {variant_id}") from exc
        if not math.isfinite(sharpe):
            raise ValueError(f"Non-finite annualized_sharpe for variant {variant_id}")
        sharpes.append(sharpe)
    return sharpes, {
        "experiment_id": experiment_id,
        "variant_count": len(sharpes),
        "trial_ledger_sha256": sha256_file(path),
        "experiment_archive_sha256": observed_archive_hash,
        "experiment_archive_file": experiment_archive_path.name,
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    dataset_dir = Path(args.dataset_dir)
    reference_path = Path(args.reference_manifest)
    trial_path = Path(args.trial_ledger)

    integrity = verify_dataset(dataset_dir)
    if integrity.get("status") != "verified":
        raise ValueError("Candidate dataset failed its manifest/hash integrity check")
    provenance = verify_reference_identity(dataset_dir, reference_path, args.expected_rows)
    trial_sharpes, trial_evidence = load_complete_trial_history(
        trial_path, args.folds, Path(args.experiment_archive)
    )

    csv_path = dataset_dir / "oanda_completed_mid_candles.csv"
    frame = pd.read_csv(csv_path)
    required = {"time", "volume", "open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        raise ValueError("Normalized OANDA CSV does not contain required time/OHLCV columns")
    index = pd.to_datetime(frame.pop("time"), utc=True, errors="raise")
    if index.has_duplicates or not index.is_monotonic_increasing:
        raise ValueError("Research input timestamps must be unique and strictly chronological")
    frame.index = pd.DatetimeIndex(index, name="time")
    frame = frame[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="raise")
    if frame.isna().any().any() or not all(math.isfinite(float(v)) for v in frame.to_numpy().ravel()):
        raise ValueError("Research input contains NaN or infinite numeric data")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Research OHLC prices must be positive")
    if len(frame) != args.expected_rows:
        raise ValueError(f"CSV has {len(frame)} rows; expected {args.expected_rows}")

    metrics = ai_walk_forward_backtest(
        frame,
        asset=args.asset,
        folds=args.folds,
        min_train=args.min_train,
        threshold=args.threshold,
        cost_stress_multiplier=args.cost_stress_multiplier,
        research_trial_sharpes=trial_sharpes,
    )
    metrics["status"] = "OK"
    gate = oos_promotion_gate(
        metrics,
        min_dsr=0.95,
        require_cost_stress=True,
        min_folds=args.folds,
        min_total_return=args.min_total_return,
        min_positive_fold_ratio=0.50,
        max_negative_fold_return=-0.20,
        require_last_fold_positive=True,
        min_sharpe=args.min_sharpe,
        max_drawdown=args.max_drawdown,
        min_trades=args.min_trades,
    )
    return {
        "schema_version": 1,
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "result": "PASS" if gate["passed"] else "FAIL",
        "promotion_authorized": bool(gate["passed"]),
        "not_live_trading_evidence": True,
        "commit_sha": __import__("os").environ.get("GITHUB_SHA", ""),
        "dataset_provenance": provenance,
        "trial_history": trial_evidence,
        "evaluation": metrics,
        "promotion_gate": gate,
        "limitations": [
            "This report is valid only for the exact archived dataset and trial-ledger hashes recorded above.",
            "The trial ledger completeness assertion must be backed by the complete experiment archive; a manually incomplete ledger invalidates DSR.",
            "Passing research gates does not authorize live trading or production capital.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", required=True)
    parser.add_argument("--reference-manifest", required=True, help="Separately archived manifest for the known original dataset")
    parser.add_argument("--trial-ledger", required=True, help="Complete immutable JSON ledger of all tested variants and annualized Sharpe values")
    parser.add_argument("--experiment-archive", required=True, help="Full experiment archive whose SHA-256 is recorded in the trial ledger")
    parser.add_argument("--expected-rows", type=int, default=1799)
    parser.add_argument("--asset", choices=("crypto", "forex"), default="forex")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--min-train", type=int, default=800)
    parser.add_argument("--threshold", type=float, default=0.05)
    parser.add_argument("--cost-stress-multiplier", type=float, default=2.0)
    parser.add_argument("--min-total-return", type=float, default=0.0)
    parser.add_argument("--min-sharpe", type=float, default=0.50)
    parser.add_argument("--max-drawdown", type=float, default=-0.25)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--output", default="oanda_oos_evaluation.json")
    args = parser.parse_args()
    if args.expected_rows < 1 or args.folds < 2 or args.min_train < 1 or args.min_trades < 0:
        parser.error("expected rows/min-train/min-trades must be non-negative and folds must be at least 2")
    try:
        report = evaluate(args)
        output = Path(args.output)
        output.write_text(json.dumps(finite_json(report), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({
            "result": report["result"],
            "promotion_authorized": report["promotion_authorized"],
            "dataset_raw_sha256": report["dataset_provenance"]["raw_response_sha256"],
            "dataset_csv_sha256": report["dataset_provenance"]["normalized_csv_sha256"],
            "trial_count": report["trial_history"]["variant_count"],
            "deflated_sharpe": report["evaluation"].get("deflated_sharpe"),
            "deflated_sharpe_status": report["evaluation"].get("deflated_sharpe_status"),
            "cost_stress_ok": report["evaluation"].get("cost_stress_ok"),
            "report": str(output),
        }, indent=2))
        return 0 if report["promotion_authorized"] else 3
    except Exception as exc:
        print(f"ERROR: research evaluation blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
