"""Fail-closed validation for signed external research evidence."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UNIVERSE_SOURCES = {"listing_history", "delisting_history", "failed_assets"}
_COST_SOURCES = {"observed_fills", "order_book", "fee_schedule"}
_TRIAL_FIELDS = {"trial_label", "config_sha256", "code_sha256", "dataset_sha256",
                 "oos_sample_sha256", "protocol_version", "annualized_sharpe", "results_sha256"}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def parse_public_keys(keys: str | Mapping[str, str] | None) -> dict[str, bytes]:
    if not keys:
        return {}
    if isinstance(keys, str):
        try:
            keys = json.loads(keys)
        except (TypeError, ValueError):
            return {}
    if not isinstance(keys, Mapping):
        return {}
    output = {}
    for key_id, encoded in keys.items():
        if not isinstance(key_id, str) or not key_id.strip() or not isinstance(encoded, str):
            continue
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        if len(key) == 32:
            output[key_id.strip()] = key
    return output


def verify_signed_document(document: Mapping[str, Any] | None,
                           trusted_public_keys: str | Mapping[str, str] | None,
                           *, expected_type: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        return {"verified": False, "reason": "EVIDENCE_DOCUMENT_MISSING"}
    if document.get("evidence_type") != expected_type:
        return {"verified": False, "reason": "EVIDENCE_TYPE_MISMATCH"}
    attestation = document.get("attestation")
    if not isinstance(attestation, Mapping) or attestation.get("algorithm") != "Ed25519":
        return {"verified": False, "reason": "EVIDENCE_ATTESTATION_MISSING_OR_UNSUPPORTED"}
    key_id = str(attestation.get("key_id") or "").strip()
    raw_key = parse_public_keys(trusted_public_keys).get(key_id)
    if raw_key is None:
        return {"verified": False, "reason": "EVIDENCE_SIGNING_KEY_UNTRUSTED"}
    signature_text = attestation.get("signature_base64")
    try:
        if not isinstance(signature_text, str):
            raise ValueError("signature missing")
        signature = base64.b64decode(signature_text, validate=True)
        signed = {str(k): v for k, v in document.items() if k != "attestation"}
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, canonical_json_bytes(signed))
        digest = canonical_sha256(signed)
    except (ValueError, TypeError, InvalidSignature):
        return {"verified": False, "reason": "EVIDENCE_SIGNATURE_INVALID"}
    evidence_id = str(document.get("evidence_id") or "").strip()
    if not evidence_id:
        return {"verified": False, "reason": "EVIDENCE_ID_MISSING"}
    return {"verified": True, "reason": "SIGNED_EVIDENCE_VERIFIED",
            "evidence_id": evidence_id, "evidence_sha256": digest, "key_id": key_id}


def _sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _sources_ok(sources: Any, required: set[str]) -> bool:
    if not isinstance(sources, list):
        return False
    found = set()
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        kind, uri, digest = source.get("source_type"), source.get("uri"), source.get("sha256")
        if isinstance(kind, str) and isinstance(uri, str) and uri.strip() and _sha(digest):
            found.add(kind.strip().lower())
    return required.issubset(found)


def validate_crypto_universe_manifest(document, trusted_public_keys, *, venue: str,
                                      market_type: str, coverage_start: str,
                                      coverage_end: str, symbol: str | None = None) -> dict[str, Any]:
    signed = verify_signed_document(document, trusted_public_keys,
                                    expected_type="crypto_point_in_time_universe")
    if not signed.get("verified"):
        return {**signed, "universe_verified": False}
    doc = document
    if str(doc.get("venue") or "").casefold() != str(venue or "").casefold():
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_VENUE_MISMATCH"}
    if str(doc.get("market_type") or "").casefold() != str(market_type or "").casefold():
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_MARKET_TYPE_MISMATCH"}
    start, end, want_start, want_end = map(_time, (
        doc.get("coverage_start"), doc.get("coverage_end"), coverage_start, coverage_end))
    if not all((start, end, want_start, want_end)) or start > want_start or end < want_end or start >= end:
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_COVERAGE_INCOMPLETE"}
    if (doc.get("membership_policy") != "POINT_IN_TIME" or doc.get("coverage_complete") is not True
            or doc.get("includes_delisted") is not True or doc.get("includes_failed_assets") is not True):
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_SURVIVORSHIP_EVIDENCE_INCOMPLETE"}
    if not _sources_ok(doc.get("sources"), _UNIVERSE_SOURCES):
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_RAW_SOURCE_MANIFEST_INCOMPLETE"}
    rows = doc.get("instruments")
    if not isinstance(rows, list) or not rows:
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_INSTRUMENTS_MISSING"}
    symbols = set()
    try:
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError
            name = str(row.get("symbol") or "").strip()
            listed = _time(row.get("listed_at"))
            delisted = _time(row.get("delisted_at")) if row.get("delisted_at") else None
            if not name or name in symbols or listed is None or (delisted and delisted < listed):
                raise ValueError
            if listed > end or (delisted and delisted < start):
                raise ValueError
            symbols.add(name)
    except (ValueError, TypeError):
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_INSTRUMENT_TIMELINE_INVALID"}
    digest = canonical_sha256(sorted(rows, key=lambda row: str(row["symbol"]).casefold()))
    if doc.get("instruments_sha256") != digest:
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_INSTRUMENT_HASH_MISMATCH"}
    if symbol and symbol not in symbols:
        return {"verified": False, "universe_verified": False, "reason": "UNIVERSE_SYMBOL_MISSING"}
    return {**signed, "universe_verified": True, "reason": "POINT_IN_TIME_UNIVERSE_VERIFIED",
            "instruments_sha256": digest, "instrument_count": len(symbols),
            "venue": venue, "market_type": market_type}


def validate_execution_cost_calibration(document, trusted_public_keys, *, venue: str,
                                         market_type: str, symbol: str,
                                         model_fingerprint: str) -> dict[str, Any]:
    signed = verify_signed_document(document, trusted_public_keys,
                                    expected_type="venue_execution_cost_calibration")
    if not signed.get("verified"):
        return {**signed, "calibration_verified": False}
    doc = document
    for field, expected in (("venue", venue), ("market_type", market_type),
                            ("symbol", symbol), ("model_fingerprint", model_fingerprint)):
        if not str(doc.get(field) or "").strip() or str(doc.get(field)).casefold() != str(expected or "").casefold():
            return {"verified": False, "calibration_verified": False,
                    "reason": "COST_CALIBRATION_CONTEXT_MISMATCH", "mismatched_field": field}
    if doc.get("synthetic_only") is not False or doc.get("testnet_only") is not False:
        return {"verified": False, "calibration_verified": False,
                "reason": "COST_CALIBRATION_NOT_REAL_VENUE_EVIDENCE"}
    if not _sources_ok(doc.get("sources"), _COST_SOURCES):
        return {"verified": False, "calibration_verified": False,
                "reason": "COST_CALIBRATION_SOURCE_MANIFEST_INCOMPLETE"}
    try:
        orders, fills = int(doc.get("observed_order_count")), int(doc.get("observed_fill_count"))
        heldout = doc.get("heldout_validation")
        count = int(heldout.get("sample_count"))
        spread_error = float(heldout.get("mean_absolute_spread_error_bps"))
        slippage_error = float(heldout.get("mean_absolute_slippage_error_bps"))
        start, end = _time(doc.get("validation_start")), _time(doc.get("validation_end"))
    except (ValueError, TypeError, OverflowError, AttributeError):
        return {"verified": False, "calibration_verified": False, "reason": "COST_CALIBRATION_METRICS_INVALID"}
    if (orders < 30 or fills < 30 or count < 30 or heldout.get("status") != "PASS"
            or not math.isfinite(spread_error) or not math.isfinite(slippage_error)
            or spread_error < 0 or slippage_error < 0 or not start or not end or start >= end):
        return {"verified": False, "calibration_verified": False,
                "reason": "COST_CALIBRATION_HELDOUT_VALIDATION_INCOMPLETE"}
    return {**signed, "calibration_verified": True, "reason": "VENUE_CALIBRATION_VERIFIED",
            "observed_order_count": orders, "observed_fill_count": fills,
            "heldout_sample_count": count, "model_fingerprint": model_fingerprint}


def validate_comparable_trial_archive(document, trusted_public_keys, *, candidate_labels: set[str],
                                      expected_context: Mapping[str, str]) -> dict[str, Any]:
    signed = verify_signed_document(document, trusted_public_keys,
                                    expected_type="comparable_trial_performance_archive")
    if not signed.get("verified"):
        return {**signed, "trial_archive_verified": False}
    doc = document
    if doc.get("archive_complete") is not True or doc.get("includes_all_trials") is not True:
        return {"verified": False, "trial_archive_verified": False, "reason": "TRIAL_ARCHIVE_NOT_COMPLETE"}
    rows = doc.get("trials")
    try:
        count = int(doc.get("trial_count"))
    except (TypeError, ValueError, OverflowError):
        count = -1
    if not isinstance(rows, list) or len(rows) < 2 or count != len(rows):
        return {"verified": False, "trial_archive_verified": False, "reason": "TRIAL_ARCHIVE_COUNT_MISMATCH"}
    labels, samples, protocols, sharpes = set(), set(), set(), []
    for row in rows:
        if not isinstance(row, Mapping) or not _TRIAL_FIELDS.issubset(row.keys()):
            return {"verified": False, "trial_archive_verified": False, "reason": "TRIAL_ARCHIVE_ROW_INCOMPLETE"}
        label = str(row.get("trial_label") or "").strip()
        if not label or label in labels:
            return {"verified": False, "trial_archive_verified": False, "reason": "TRIAL_ARCHIVE_DUPLICATE_LABEL"}
        labels.add(label)
        if not all(_sha(row.get(k)) for k in ("config_sha256", "code_sha256", "dataset_sha256",
                                               "oos_sample_sha256", "results_sha256")):
            return {"verified": False, "trial_archive_verified": False, "reason": "TRIAL_ARCHIVE_HASH_INVALID"}
        try:
            sharpe = float(row.get("annualized_sharpe"))
        except (TypeError, ValueError, OverflowError):
            return {"verified": False, "trial_archive_verified": False, "reason": "TRIAL_ARCHIVE_SHARPE_INVALID"}
        if not math.isfinite(sharpe):
            return {"verified": False, "trial_archive_verified": False, "reason": "TRIAL_ARCHIVE_SHARPE_INVALID"}
        samples.add(str(row["oos_sample_sha256"]))
        protocols.add(str(row["protocol_version"] or "").strip())
        for field, expected in expected_context.items():
            if str(row.get(field) or "").casefold() != str(expected or "").casefold():
                return {"verified": False, "trial_archive_verified": False,
                        "reason": "TRIAL_ARCHIVE_CONTEXT_MISMATCH", "mismatched_field": field}
        sharpes.append(sharpe)
    if len(samples) != 1 or "" in protocols or len(protocols) != 1:
        return {"verified": False, "trial_archive_verified": False, "reason": "TRIAL_ARCHIVE_NOT_COMPARABLE"}
    if not candidate_labels.issubset(labels):
        return {"verified": False, "trial_archive_verified": False,
                "reason": "TRIAL_ARCHIVE_MISSING_CURRENT_TRIALS",
                "missing_trial_labels": sorted(candidate_labels - labels)}
    return {**signed, "trial_archive_verified": True, "reason": "COMPARABLE_TRIAL_ARCHIVE_VERIFIED",
            "trial_count": len(rows), "trial_sharpes_annual": sharpes,
            "oos_sample_sha256": next(iter(samples)), "protocol_version": next(iter(protocols))}


def oos_sample_fingerprint(dataset_sha256: str, oos_windows: list[dict[str, Any]],
                           protocol_version: str = "atlas-purged-walk-forward-v1") -> str:
    if not _sha(dataset_sha256) or not oos_windows:
        return ""
    return canonical_sha256({"dataset_sha256": dataset_sha256,
                             "protocol_version": protocol_version, "oos_windows": oos_windows})
