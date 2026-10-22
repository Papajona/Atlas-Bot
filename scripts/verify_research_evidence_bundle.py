#!/usr/bin/env python3
"""Verify a local bundle of signed research evidence and its raw source bytes.

This tool never downloads URLs, signs evidence, or authorizes model promotion. The
operator independently retrieves the referenced sources into a local directory;
this script verifies that each source's bytes match the SHA-256 in its signed
manifest and then invokes Atlas's fail-closed evidence validators.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

# Allow direct execution as `python scripts/verify_research_evidence_bundle.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research_evidence import (
    parse_public_keys,
    validate_comparable_trial_archive,
    validate_crypto_universe_manifest,
    validate_execution_cost_calibration,
    _source_uri_is_retrievable,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SOURCE_TYPES = {
    "universe": {"listing_history", "delisting_history", "failed_assets"},
    "calibration": {"observed_fills", "order_book", "fee_schedule"},
    # The application validator validates trial rows and comparability. The release
    # bundle also requires the artifacts an independent reviewer needs to reconcile
    # the completeness claim, configs, code, dataset, and held-out results.
    "trial_archive": {
        "experiment_log", "config_snapshot", "code_snapshot", "dataset", "oos_results"
    },
}


def _read_json_object(path: str | Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: cannot read JSON ({type(exc).__name__})") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected a JSON object")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_source_artifacts(
    documents: Mapping[str, Mapping[str, Any]],
    source_root: str | Path,
    source_map: Mapping[str, str],
) -> dict[str, Any]:
    """Verify every signed source reference against a local raw artifact.

    source_map maps exact source URIs to relative paths beneath source_root. Paths
    may not be absolute, traverse upward, or resolve outside the root (including
    through symlinks). No network request is performed by this function.
    """
    issues: list[dict[str, str]] = []
    checks: list[dict[str, str]] = []
    used_uris: set[str] = set()
    uri_digests: dict[str, str] = {}

    try:
        root = Path(source_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return {
            "verified": False,
            "checked_count": 0,
            "checks": [],
            "issues": [{"code": "SOURCE_ROOT_UNAVAILABLE", "detail": type(exc).__name__}],
        }
    if not root.is_dir():
        return {
            "verified": False,
            "checked_count": 0,
            "checks": [],
            "issues": [{"code": "SOURCE_ROOT_NOT_DIRECTORY", "detail": str(root)}],
        }

    if not isinstance(source_map, Mapping) or not source_map:
        source_map = {}
        issues.append({"code": "SOURCE_MAP_MISSING", "detail": "source map must be a non-empty URI-to-relative-path object"})
    else:
        for uri, relative in source_map.items():
            if not isinstance(uri, str) or not uri.strip() or not isinstance(relative, str) or not relative.strip():
                issues.append({"code": "SOURCE_MAP_ENTRY_INVALID", "detail": "URI and relative path must be non-empty strings"})
                continue
            candidate = Path(relative)
            if candidate.is_absolute() or ".." in candidate.parts:
                issues.append({"code": "SOURCE_MAP_PATH_UNSAFE", "detail": uri})
            if not _source_uri_is_retrievable(uri):
                issues.append({"code": "SOURCE_MAP_URI_NOT_RETRIEVABLE", "detail": uri})

    for doc_name, required_types in _REQUIRED_SOURCE_TYPES.items():
        document = documents.get(doc_name)
        if not isinstance(document, Mapping):
            issues.append({"code": "EVIDENCE_DOCUMENT_MISSING", "detail": doc_name})
            continue
        sources = document.get("sources")
        if not isinstance(sources, list):
            issues.append({"code": "SOURCE_LIST_MISSING", "detail": doc_name})
            continue

        found_types: set[str] = set()
        for index, source in enumerate(sources):
            if not isinstance(source, Mapping):
                issues.append({"code": "SOURCE_RECORD_INVALID", "detail": f"{doc_name}[{index}]"})
                continue
            source_type = source.get("source_type")
            uri = source.get("uri")
            expected_digest = source.get("sha256")
            if not isinstance(source_type, str) or not source_type.strip():
                issues.append({"code": "SOURCE_TYPE_MISSING", "detail": f"{doc_name}[{index}]"})
                continue
            source_type = source_type.strip().lower()
            found_types.add(source_type)
            if not isinstance(uri, str) or not _source_uri_is_retrievable(uri):
                issues.append({"code": "SOURCE_URI_INVALID", "detail": f"{doc_name}:{source_type}"})
                continue
            if not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest):
                issues.append({"code": "SOURCE_SHA256_INVALID", "detail": f"{doc_name}:{source_type}"})
                continue
            previous = uri_digests.get(uri)
            if previous is not None and previous != expected_digest:
                issues.append({"code": "SOURCE_URI_DIGEST_CONFLICT", "detail": uri})
                continue
            uri_digests[uri] = expected_digest
            relative = source_map.get(uri)
            if not isinstance(relative, str) or not relative.strip():
                issues.append({"code": "SOURCE_ARTIFACT_NOT_MAPPED", "detail": uri})
                continue
            rel_path = Path(relative)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                issues.append({"code": "SOURCE_ARTIFACT_PATH_UNSAFE", "detail": uri})
                continue
            try:
                source_path = (root / rel_path).resolve(strict=True)
                source_path.relative_to(root)
            except (OSError, RuntimeError, ValueError):
                issues.append({"code": "SOURCE_ARTIFACT_PATH_ESCAPE_OR_MISSING", "detail": uri})
                continue
            if not source_path.is_file():
                issues.append({"code": "SOURCE_ARTIFACT_NOT_A_FILE", "detail": uri})
                continue
            try:
                actual_digest = _file_sha256(source_path)
            except OSError as exc:
                issues.append({"code": "SOURCE_ARTIFACT_READ_FAILED", "detail": f"{uri}:{type(exc).__name__}"})
                continue
            used_uris.add(uri)
            if actual_digest != expected_digest:
                issues.append({"code": "SOURCE_ARTIFACT_HASH_MISMATCH", "detail": uri})
                continue
            checks.append({
                "document": doc_name,
                "source_type": source_type,
                "uri": uri,
                "sha256": actual_digest,
                "status": "PASS",
            })

        missing_types = sorted(required_types - found_types)
        if missing_types:
            issues.append({
                "code": "SOURCE_TYPES_MISSING",
                "detail": f"{doc_name}:{','.join(missing_types)}",
            })

    unused = sorted(set(source_map) - used_uris)
    if unused:
        issues.append({
            "code": "SOURCE_MAP_ENTRIES_UNUSED",
            "detail": ",".join(unused),
        })

    return {
        "verified": not issues and len(checks) > 0,
        "checked_count": len(checks),
        "checks": checks,
        "issues": issues,
    }


def _configured_limit(name: str, errors: list[dict[str, str]]) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        errors.append({"code": "COST_POLICY_THRESHOLD_INVALID", "detail": name})
        return None
    if not math.isfinite(value) or value < 0:
        errors.append({"code": "COST_POLICY_THRESHOLD_INVALID", "detail": name})
        return None
    return value


def verify_bundle(
    context: Mapping[str, Any],
    documents: Mapping[str, Mapping[str, Any]],
    source_root: str | Path,
    source_map: Mapping[str, str],
    trusted_keys_json: str | None,
    max_spread_error_bps: float | None,
    max_slippage_error_bps: float | None,
) -> dict[str, Any]:
    """Return a redacted verification report for a complete evidence bundle."""
    input_issues: list[dict[str, str]] = []
    required_text = (
        "venue", "market_type", "asset_class", "symbol", "timeframe",
        "coverage_start", "coverage_end", "dataset_sha256", "oos_sample_sha256",
        "protocol_version", "execution_cost_model_fingerprint",
    )
    for field in required_text:
        if not isinstance(context.get(field), str) or not context[field].strip():
            input_issues.append({"code": "CONTEXT_FIELD_MISSING", "detail": field})
    for field in ("dataset_sha256", "oos_sample_sha256", "execution_cost_model_fingerprint"):
        value = context.get(field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            input_issues.append({"code": "CONTEXT_SHA256_INVALID", "detail": field})
    labels = context.get("candidate_labels")
    if (
        not isinstance(labels, list)
        or not labels
        or any(not isinstance(label, str) or not label.strip() for label in labels)
        or len(set(labels)) != len(labels)
    ):
        input_issues.append({"code": "CANDIDATE_LABELS_INVALID", "detail": "candidate_labels must be unique non-empty strings"})
        labels = []

    keys_raw = trusted_keys_json or ""
    key_count = len(parse_public_keys(keys_raw))
    if key_count == 0:
        input_issues.append({
            "code": "TRUSTED_PUBLIC_KEYS_MISSING",
            "detail": "RESEARCH_EVIDENCE_PUBLIC_KEYS_JSON is empty or contains no valid 32-byte Ed25519 keys",
        })

    common = {
        "venue": str(context.get("venue") or ""),
        "market_type": str(context.get("market_type") or ""),
        "symbol": str(context.get("symbol") or ""),
    }
    try:
        universe = validate_crypto_universe_manifest(
            documents.get("universe"),
            keys_raw,
            **common,
            coverage_start=str(context.get("coverage_start") or ""),
            coverage_end=str(context.get("coverage_end") or ""),
        )
    except Exception as exc:
        universe = {"verified": False, "universe_verified": False, "reason": f"VALIDATOR_ERROR_{type(exc).__name__}"}

    try:
        calibration = validate_execution_cost_calibration(
            documents.get("calibration"),
            keys_raw,
            **common,
            model_fingerprint=str(context.get("execution_cost_model_fingerprint") or ""),
            max_spread_error_bps=max_spread_error_bps,
            max_slippage_error_bps=max_slippage_error_bps,
        )
    except Exception as exc:
        calibration = {"verified": False, "calibration_verified": False, "reason": f"VALIDATOR_ERROR_{type(exc).__name__}"}

    expected_archive_context = {
        "asset_class": str(context.get("asset_class") or ""),
        "venue": str(context.get("venue") or ""),
        "market_type": str(context.get("market_type") or ""),
        "symbol": str(context.get("symbol") or ""),
        "timeframe": str(context.get("timeframe") or ""),
        "dataset_sha256": str(context.get("dataset_sha256") or ""),
        "oos_sample_sha256": str(context.get("oos_sample_sha256") or ""),
        "protocol_version": str(context.get("protocol_version") or ""),
    }
    try:
        archive = validate_comparable_trial_archive(
            documents.get("trial_archive"),
            keys_raw,
            candidate_labels=set(labels),
            expected_context=expected_archive_context,
        )
    except Exception as exc:
        archive = {"verified": False, "trial_archive_verified": False, "reason": f"VALIDATOR_ERROR_{type(exc).__name__}"}

    sources = verify_source_artifacts(documents, source_root, source_map)
    validations = {
        "crypto_universe": universe,
        "execution_cost_calibration": calibration,
        "comparable_trial_archive": archive,
    }
    validators_pass = (
        universe.get("universe_verified") is True
        and calibration.get("calibration_verified") is True
        and archive.get("trial_archive_verified") is True
    )
    technical_pass = validators_pass and sources.get("verified") is True and not input_issues
    return {
        "schema_version": 1,
        "status": "TECHNICAL_VALIDATION_PASS_PENDING_EXTERNAL_REVIEW" if technical_pass else "BLOCKED",
        "technical_validation_passed": technical_pass,
        "promotion_authorized": False,
        "trusted_key_count": key_count,
        "validators": validations,
        "raw_source_artifacts": sources,
        "input_issues": input_issues,
        "external_review_required": True,
        "release_note": (
            "This report checks signatures, context, policy thresholds, and local raw-source byte hashes. "
            "It does not certify that upstream records are complete or truthful and never authorizes promotion."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="Exact dataset/model context and current candidate labels JSON")
    parser.add_argument("--universe", required=True, help="Signed point-in-time universe manifest JSON")
    parser.add_argument("--calibration", required=True, help="Signed venue execution-cost calibration JSON")
    parser.add_argument("--trial-archive", required=True, help="Signed comparable trial archive JSON")
    parser.add_argument("--source-root", required=True, help="Directory containing independently retrieved raw source files")
    parser.add_argument("--source-map", required=True, help="JSON object mapping each exact source URI to a relative raw-file path")
    parser.add_argument("--output", default="research-evidence-verification.json")
    parser.add_argument("--trusted-keys-env", default="RESEARCH_EVIDENCE_PUBLIC_KEYS_JSON")
    args = parser.parse_args()

    errors: list[dict[str, str]] = []
    try:
        context = _read_json_object(args.context, "context")
        documents = {
            "universe": _read_json_object(args.universe, "universe"),
            "calibration": _read_json_object(args.calibration, "calibration"),
            "trial_archive": _read_json_object(args.trial_archive, "trial archive"),
        }
        source_map_raw = _read_json_object(args.source_map, "source map")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in source_map_raw.items()):
            raise ValueError("source map must map source URI strings to relative file-path strings")
        spread = _configured_limit("RESEARCH_COST_MAX_SPREAD_MAE_BPS", errors)
        slippage = _configured_limit("RESEARCH_COST_MAX_SLIPPAGE_MAE_BPS", errors)
        report = verify_bundle(
            context,
            documents,
            args.source_root,
            source_map_raw,
            os.getenv(args.trusted_keys_env),
            spread,
            slippage,
        )
        report["input_issues"].extend(errors)
        if errors:
            report["technical_validation_passed"] = False
            report["promotion_authorized"] = False
            report["status"] = "BLOCKED"
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({
            "status": report["status"],
            "technical_validation_passed": report["technical_validation_passed"],
            "promotion_authorized": False,
            "report_path": str(output),
        }, sort_keys=True))
        return 0 if report["technical_validation_passed"] else 1
    except (OSError, ValueError) as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "technical_validation_passed": False,
            "promotion_authorized": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
