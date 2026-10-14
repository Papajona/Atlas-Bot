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
from zipfile import BadZipFile, ZipFile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

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

def granularity_bars_per_year(granularity: str) -> int:
    """Use the verified OANDA candle granularity, not an asset-wide hourly default."""
    known = {
        "S5": 4_354_560,
        "S10": 2_177_280,
        "S15": 1_451_520,
        "S30": 725_760,
        "M1": 362_880,
        "M2": 181_440,
        "M4": 90_720,
        "M5": 72_576,
        "M10": 36_288,
        "M15": 24_192,
        "M30": 12_096,
        "H1": 6_048,
        "H2": 3_024,
        "H3": 2_016,
        "H4": 1_512,
        "H6": 1_008,
        "H8": 756,
        "H12": 504,
        "D": 252,
        "W": 52,
        "M": 12,
    }
    value = str(granularity or "").strip().upper()
    if value not in known:
        raise ValueError(f"Unsupported OANDA granularity for annualization: {value!r}")
    return known[value]


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
        "bars_per_year": granularity_bars_per_year(actual["granularity"]),
        "requested_from": request.get("from"),
        "requested_to": request.get("to"),
        "rows": int(actual["completed_row_count"]),
        "first_timestamp_utc": actual["first_completed_timestamp_utc"],
        "last_timestamp_utc": actual["last_completed_timestamp_utc"],
        "request_parameters": request,
    }


def _safe_archive_member_path(value: object, label: str) -> str:
    from pathlib import PurePosixPath

    name = str(value or "")
    member = PurePosixPath(name)
    if (
        not name
        or member.is_absolute()
        or any(part in {"", ".", ".."} for part in member.parts)
        or "\\" in name
        or name != member.as_posix()
        or name == "atlas_trial_manifest.json"
    ):
        raise ValueError(f"Unsafe or invalid archive member path for {label}")
    return name


def _zip_member_sha256(archive: ZipFile, path: str, label: str) -> str:
    matches = [item for item in archive.infolist() if item.filename == path]
    if len(matches) != 1:
        raise ValueError(f"Archive must contain exactly one evidence file for {label}: {path}")
    info = matches[0]
    if info.is_dir() or info.file_size < 1 or info.file_size > 100_000_000:
        raise ValueError(f"Archive evidence file has invalid size/type for {label}: {path}")
    digest = hashlib.sha256()
    with archive.open(info, "r") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_experiment_archive_manifest(
    archive_path: Path,
    experiment_id: str,
    ledger_variants: dict[str, dict[str, Any]],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Bind every tested variant to checksummed result evidence inside the experiment ZIP."""
    try:
        with ZipFile(archive_path, "r") as archive:
            matches = [info for info in archive.infolist() if info.filename == "atlas_trial_manifest.json"]
            if len(matches) != 1:
                raise ValueError("Experiment ZIP must contain exactly one atlas_trial_manifest.json")
            info = matches[0]
            if info.file_size < 1 or info.file_size > 5_000_000:
                raise ValueError("Experiment trial manifest exceeds the 5 MB safety limit")
            raw_manifest = archive.read(info)
            try:
                manifest = json.loads(raw_manifest.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Archived trial manifest is not valid UTF-8 JSON") from exc
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise ValueError("Archived trial manifest schema_version must be 1")
            if manifest.get("complete") is not True or manifest.get("all_variants_included") is not True:
                raise ValueError("Archived trial manifest must attest complete=true and all_variants_included=true")
            if str(manifest.get("experiment_id") or "").strip() != experiment_id:
                raise ValueError("Ledger experiment_id does not match the experiment ZIP manifest")
            expected_data = {
                "dataset_raw_response_sha256": provenance["raw_response_sha256"],
                "dataset_csv_sha256": provenance["normalized_csv_sha256"],
                "instrument": provenance["instrument"],
                "granularity": provenance["granularity"],
                "request_parameters": provenance["request_parameters"],
                "evaluation_assumptions": provenance["evaluation_assumptions"],
            }
            for key, expected in expected_data.items():
                if manifest.get(key) != expected:
                    raise ValueError(f"Archived trial manifest data identity mismatch: {key}")

            log_path = _safe_archive_member_path(manifest.get("experiment_log_path"), "experiment log")
            log_sha = str(manifest.get("experiment_log_sha256") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", log_sha):
                raise ValueError("Archived trial manifest must include the SHA-256 of the full experiment log")
            observed_log_sha = _zip_member_sha256(archive, log_path, "experiment log")
            if observed_log_sha != log_sha:
                raise ValueError("Experiment log SHA-256 differs from its archived trial manifest")

            archived_variants = manifest.get("variants")
            if not isinstance(archived_variants, list) or not archived_variants:
                raise ValueError("Archived trial manifest must list every evaluated variant")
            archive_variants: dict[str, dict[str, Any]] = {}
            for item in archived_variants:
                if not isinstance(item, dict):
                    raise ValueError("Archived trial manifest contains a malformed variant entry")
                variant_id = str(item.get("variant_id") or "").strip()
                if not variant_id or variant_id in archive_variants:
                    raise ValueError("Archived trial manifest variant_id values must be present and unique")
                raw_sharpe = item.get("annualized_sharpe")
                if isinstance(raw_sharpe, bool):
                    raise ValueError(f"Archived annualized Sharpe invalid for variant {variant_id}")
                try:
                    sharpe = float(raw_sharpe)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"Archived annualized Sharpe invalid for variant {variant_id}") from exc
                if not math.isfinite(sharpe):
                    raise ValueError(f"Archived annualized Sharpe non-finite for variant {variant_id}")
                evidence_path = _safe_archive_member_path(item.get("evidence_path"), f"variant {variant_id}")
                evidence_sha = str(item.get("evidence_sha256") or "").lower()
                if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha):
                    raise ValueError(f"Variant {variant_id} is missing its evidence SHA-256")
                if evidence_path == log_path:
                    raise ValueError(f"Variant {variant_id} result evidence must be separate from the experiment log")
                observed_evidence_sha = _zip_member_sha256(archive, evidence_path, f"variant {variant_id}")
                if observed_evidence_sha != evidence_sha:
                    raise ValueError(f"Variant {variant_id} evidence SHA-256 mismatch")
                archive_variants[variant_id] = {
                    "annualized_sharpe": sharpe,
                    "evidence_path": evidence_path,
                    "evidence_sha256": evidence_sha,
                }

            if archive_variants.keys() != ledger_variants.keys():
                raise ValueError("Trial ledger variants do not exactly match archived experiment trial manifest")
            for variant_id, archive_item in archive_variants.items():
                ledger_item = ledger_variants[variant_id]
                if not math.isclose(
                    archive_item["annualized_sharpe"],
                    ledger_item["annualized_sharpe"],
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    raise ValueError(f"Trial ledger Sharpe differs from archived experiment manifest for {variant_id}")
                if (
                    archive_item["evidence_path"] != ledger_item["evidence_path"]
                    or archive_item["evidence_sha256"] != ledger_item["evidence_sha256"]
                ):
                    raise ValueError(f"Trial ledger evidence reference differs from archive manifest for {variant_id}")
    except (BadZipFile, OSError) as exc:
        raise ValueError("Full experiment archive is not a valid readable ZIP") from exc

    return {
        "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "experiment_log_sha256": log_sha,
        "experiment_log_verified": True,
        "manifest_variant_count": len(archive_variants),
        "variant_evidence_verified": len(archive_variants),
        "data_identity_matches": True,
        "trial_list_matches_archive": True,
    }


def load_complete_trial_history(
    path: Path,
    minimum_variants: int,
    experiment_archive_path: Path,
    provenance: dict[str, Any],
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
    ledger_variants: dict[str, dict[str, Any]] = {}
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
        evidence_path = _safe_archive_member_path(item.get("evidence_path"), f"ledger variant {variant_id}")
        evidence_sha = str(item.get("evidence_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha):
            raise ValueError(f"Trial ledger variant {variant_id} is missing an evidence SHA-256")
        sharpes.append(sharpe)
        ledger_variants[variant_id] = {
            "annualized_sharpe": sharpe,
            "evidence_path": evidence_path,
            "evidence_sha256": evidence_sha,
        }
    archive_manifest = verify_experiment_archive_manifest(
        experiment_archive_path, experiment_id, ledger_variants, provenance
    )
    return sharpes, {
        "experiment_id": experiment_id,
        "variant_count": len(sharpes),
        "trial_ledger_sha256": sha256_file(path),
        "experiment_archive_sha256": observed_archive_hash,
        "experiment_archive_file": experiment_archive_path.name,
        "archive_manifest": archive_manifest,
    }


def promotion_gate_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """Map walk-forward output fields to the explicit OOS promotion gate contract."""
    normalized = dict(metrics)
    # ai_walk_forward_backtest currently emits "folds"; accept "fold_metrics"
    # as a compatibility alias but never erase valid fold results from the actual output.
    raw_folds = metrics.get("fold_metrics")
    if raw_folds is None:
        raw_folds = metrics.get("folds")
    normalized["folds"] = list(raw_folds) if isinstance(raw_folds, (list, tuple)) else []
    cost = metrics.get("cost_analysis")
    normalized["cost_stress_ok"] = bool(isinstance(cost, dict) and cost.get("stress_ok") is True)
    return normalized


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    dataset_dir = Path(args.dataset_dir)
    reference_path = Path(args.reference_manifest)
    trial_path = Path(args.trial_ledger)

    integrity = verify_dataset(dataset_dir)
    if integrity.get("status") != "verified":
        raise ValueError("Candidate dataset failed its manifest/hash integrity check")
    provenance = verify_reference_identity(dataset_dir, reference_path, args.expected_rows)
    cost_values = {
        "taker_bps": args.taker_bps,
        "slippage_bps": args.slippage_bps,
        "carry_bps_per_bar": args.carry_bps_per_bar,
    }
    if any(not math.isfinite(float(value)) or float(value) < 0 for value in cost_values.values()):
        raise ValueError("Approved taker/slippage/carry assumptions must be finite and non-negative")
    if args.max_hold_bars < 1:
        raise ValueError("max_hold_bars must be a positive integer")
    evaluation_assumptions = {
        "bars_per_year": int(provenance["bars_per_year"]),
        **{key: float(value) for key, value in cost_values.items()},
        "max_hold_bars": int(args.max_hold_bars),
    }
    provenance["evaluation_assumptions"] = evaluation_assumptions
    trial_sharpes, trial_evidence = load_complete_trial_history(
        trial_path, args.folds, Path(args.experiment_archive), provenance
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
        bars_per_year=evaluation_assumptions["bars_per_year"],
        taker_bps=evaluation_assumptions["taker_bps"],
        slippage_bps=evaluation_assumptions["slippage_bps"],
        carry_bps_per_bar=evaluation_assumptions["carry_bps_per_bar"],
        max_hold_bars=evaluation_assumptions["max_hold_bars"],
    )
    # Preserve the backtest's status; never turn an insufficient/failed run into an OK.
    gate = oos_promotion_gate(
        promotion_gate_metrics(metrics),
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
    parser.add_argument("--taker-bps", type=float, required=True, help="Approved target-venue taker/commission cost in bps per traded notional")
    parser.add_argument("--slippage-bps", type=float, required=True, help="Evidence-backed target-venue slippage assumption in bps")
    parser.add_argument("--carry-bps-per-bar", type=float, required=True, help="Evidence-backed per-bar financing/swap/carry drag in bps; do not assume zero without evidence")
    parser.add_argument("--max-hold-bars", type=int, required=True, help="Approved triple-barrier holding horizon expressed in this dataset's verified candle bars")
    parser.add_argument("--min-total-return", type=float, default=0.0)
    parser.add_argument("--min-sharpe", type=float, default=0.50)
    parser.add_argument("--max-drawdown", type=float, default=-0.25)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--output", default="oanda_oos_evaluation.json")
    args = parser.parse_args()
    if args.expected_rows != 1799:
        parser.error("The audited original OANDA research dataset must contain exactly 1799 completed candles")
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
