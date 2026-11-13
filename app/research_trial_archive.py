"""Append-only recording of prospective research trials.

This registry preserves what Atlas evaluates from this commit onward. Its rows
are explicitly UNVERIFIED and cannot replace the signed comparable historical
archive consumed by the promotion gate.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .db import ResearchTrialArchive, ResearchTrialLedgerHead, SessionLocal
from .research_evidence import canonical_sha256


def _sha(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _int_metric(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _sample_annualized_sharpe(values: Any, annualization_factor: Any) -> float | None:
    """Match the exact capped OOS sample used by the promotion DSR calculation."""
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        return None
    sample = [_finite(value) for value in values]
    annualization = _finite(annualization_factor)
    if (
        any(value is None for value in sample)
        or annualization is None
        or annualization <= 0
    ):
        return None
    parsed = [float(value) for value in sample]
    mean = math.fsum(parsed) / len(parsed)
    variance = math.fsum((value - mean) ** 2 for value in parsed) / (len(parsed) - 1)
    if not math.isfinite(variance) or variance <= 0:
        return None
    return mean / math.sqrt(variance) * math.sqrt(annualization)


def _report_context(report: Mapping[str, Any], request: Any) -> dict[str, str]:
    provenance = report.get("data_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    validation = report.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    return {
        "asset_class": str(report.get("asset_class") or getattr(request, "asset", "") or "").strip().lower(),
        "venue": str(getattr(request, "exchange", "") or provenance.get("exchange") or report.get("exchange") or "").strip().lower(),
        "market_type": str(provenance.get("market_type") or getattr(request, "market_type", "") or report.get("market_type") or "").strip().lower(),
        "symbol": str(getattr(request, "symbol", "") or provenance.get("symbol") or report.get("symbol") or "").strip(),
        "timeframe": str(getattr(request, "timeframe", "") or provenance.get("timeframe") or report.get("timeframe") or "").strip(),
        "dataset_sha256": str(provenance.get("sha256") or "").strip().lower(),
        "protocol_version": str(validation.get("protocol_version") or report.get("protocol_version") or "").strip(),
    }


def _trial_specs(report: Mapping[str, Any]) -> list[tuple[str, dict[str, Any], str]]:
    validation = report.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    labels = validation.get("trial_labels")
    if not isinstance(labels, list):
        return []
    strategies = validation.get("per_strategy_oos")
    strategies = strategies if isinstance(strategies, Mapping) else {}
    output: list[tuple[str, dict[str, Any], str]] = []
    for label_value in labels:
        label = str(label_value or "").strip()
        if not label:
            continue
        if label.startswith("variant:"):
            variant_map = report.get("robustness")
            variant_map = variant_map if isinstance(variant_map, Mapping) else {}
            variants = variant_map.get("parameter_variants")
            variants = variants if isinstance(variants, list) else []
            matched = None
            for variant in variants:
                if not isinstance(variant, Mapping):
                    continue
                try:
                    expected_label = (
                        f"variant:t{float(variant['threshold_multiplier']):.2f}:"
                        f"r{float(variant['risk_multiplier']):.2f}@{variant['config_sha256']}"
                    )
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if label == expected_label:
                    matched = dict(variant)
                    break
            output.append((label, matched or {}, "variant"))
            continue
        if label.startswith("ai@"):
            ai = report.get("ai_walk_forward")
            output.append((label, dict(ai) if isinstance(ai, Mapping) else {}, "ai"))
            continue
        strategy_name = label.split("@", 1)[0]
        metric = strategies.get(strategy_name)
        output.append((label, dict(metric) if isinstance(metric, Mapping) else {}, strategy_name))
    return output


def _row_payload(report: Mapping[str, Any], context: Mapping[str, str],
                  label: str, metrics: Mapping[str, Any], category: str,
                  evaluation_id: str) -> dict[str, Any]:
    validation = report.get("validation")
    validation = validation if isinstance(validation, Mapping) else {}
    is_ai_trial = str(category).strip().lower() == "ai"
    if is_ai_trial:
        config = metrics.get("trial_config")
        config = config if isinstance(config, Mapping) else {}
    else:
        config = report.get("config")
        config = config if isinstance(config, Mapping) else {}
    if str(category).strip().lower() == "variant":
        # Each plateau point has its own fully evaluated config. The base report
        # configuration is not the identity of a threshold/risk-scaled variant.
        variant_sha = str(metrics.get("config_sha256") or "").strip().lower()
        config_sha = variant_sha if _sha(variant_sha) else ""
    else:
        config_sha = hashlib.sha256(
            json.dumps(config, default=str, sort_keys=True).encode("utf-8")
        ).hexdigest() if config else ""
    # ATLAS_CODE_SHA256 must be a digest of the source bundle/release artifact, not
    # merely a git commit label. GITHUB_SHA is a commit identifier, not this hash.
    code_sha = str(os.getenv("ATLAS_CODE_SHA256") or report.get("code_sha256") or "").strip().lower()
    dataset_sha = context["dataset_sha256"]
    if str(category).strip().lower() == "ai":
        sample_sha = str(metrics.get("oos_sample_sha256") or "").strip().lower()
    else:
        sample_sha = str(validation.get("oos_sample_sha256") or "").strip().lower()
    protocol_version = str(
        metrics.get("protocol_version") if str(category).strip().lower() == "ai" else ""
    ).strip() or str(context.get("protocol_version") or validation.get("protocol_version") or report.get("protocol_version") or "").strip()
    universe = validation.get("crypto_universe_validation")
    universe = universe if isinstance(universe, Mapping) else {}
    cost = validation.get("execution_cost_calibration")
    cost = cost if isinstance(cost, Mapping) else {}
    if is_ai_trial:
        # AI's feature-warmup/label-horizon sample is intentionally separate
        # from the fixed-signal same-sample DSR distribution.
        sharpe = _finite(metrics.get("sharpe", metrics.get("oos_sharpe", metrics.get("annualized_sharpe"))))
    else:
        # Promotion DSR uses only the exact serialized (latest <=5,000) OOS
        # return sample. Never archive the full-series Sharpe beside that sample.
        fixed_oos = validation.get("fixed_signal_oos")
        fixed_oos = fixed_oos if isinstance(fixed_oos, Mapping) else {}
        annualization = metrics.get("annualization_factor")
        if annualization is None:
            annualization = fixed_oos.get("annualization_factor")
        sharpe = _sample_annualized_sharpe(metrics.get("oos_return_sample"), annualization)
    total_return = _finite(metrics.get("oos_total_return", metrics.get("total_return")))
    drawdown = _finite(metrics.get("max_drawdown", metrics.get("oos_max_drawdown")))
    trades = _int_metric(metrics.get("trades", metrics.get("oos_trades")))
    normalized_results = {
        "trial_label": label, "category": category, "sharpe": sharpe,
        "oos_total_return": total_return, "max_drawdown": drawdown, "trades": trades,
        "oos_status": metrics.get("status", metrics.get("oos_status")),
    }
    results_sha = canonical_sha256(normalized_results)
    required_context = (
        bool(context["asset_class"]) and bool(context["venue"]) and bool(context["market_type"])
        and bool(context["symbol"]) and bool(context["timeframe"]) and _sha(config_sha)
        and _sha(code_sha) and _sha(dataset_sha) and _sha(sample_sha)
        and bool(protocol_version)
    )
    required_metrics = sharpe is not None and total_return is not None and drawdown is not None and trades is not None
    status = "RECORDED_UNVERIFIED"
    if not required_context:
        status = "UNVERIFIED_MISSING_CONTEXT"
    elif not required_metrics:
        status = "UNVERIFIED_MISSING_METRICS"
    else:
        # The append-only DB registry is a prospective record. Only a signed external
        # archive may satisfy the historical-comparability / selection-bias promotion gate.
        status = "RECORDED_UNVERIFIED"
    trial_identity = {
        "label": label, "config_sha256": config_sha, "code_sha256": code_sha,
        "dataset_sha256": dataset_sha, "oos_sample_sha256": sample_sha,
        "protocol_version": protocol_version,
    }
    # A missing provenance fingerprint cannot identify an immutable experiment.
    # Retain every incomplete attempt under its own evaluation ID rather than
    # accidentally colliding with a later run that reuses its blank fingerprints.
    if not required_context or not required_metrics:
        trial_identity["evaluation_id"] = evaluation_id
    trial_key = canonical_sha256(trial_identity)
    return {
        "trial_key": trial_key, "trial_label": label,
        "asset_class": context["asset_class"], "venue": context["venue"],
        "market_type": context["market_type"], "symbol": context["symbol"],
        "timeframe": context["timeframe"], "config_sha256": config_sha,
        "code_sha256": code_sha, "dataset_sha256": dataset_sha,
        "oos_sample_sha256": sample_sha, "protocol_version": protocol_version,
        "universe_evidence_sha256": str(universe.get("evidence_sha256") or ""),
        "cost_evidence_id": str(cost.get("evidence_id") or ""),
        "annualized_sharpe": sharpe, "oos_total_return": total_return,
        "max_drawdown": drawdown, "trades": trades,
        "results_sha256": results_sha, "archive_status": status,
    }


_LEDGER_PAYLOAD_FIELDS = (
    "trial_key", "trial_label", "asset_class", "venue", "market_type", "symbol",
    "timeframe", "config_sha256", "code_sha256", "dataset_sha256", "oos_sample_sha256",
    "protocol_version", "universe_evidence_sha256", "cost_evidence_id",
    "annualized_sharpe", "oos_total_return", "max_drawdown", "trades", "results_sha256",
    "archive_status",
)
_LEDGER_VERSION = "atlas-research-trial-ledger-v2"


def _ledger_payload(value: Mapping[str, Any] | ResearchTrialArchive) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {field: value.get(field) for field in _LEDGER_PAYLOAD_FIELDS}
    return {field: getattr(value, field) for field in _LEDGER_PAYLOAD_FIELDS}


def _load_trial_ledger_keyring() -> tuple[dict[str, bytes], str | None]:
    """Load all retained HMAC keys plus the explicit active key identifier."""
    raw = os.getenv("ATLAS_RESEARCH_TRIAL_LEDGER_KEYRING_JSON")
    active_id = str(os.getenv("ATLAS_RESEARCH_TRIAL_LEDGER_ACTIVE_KEY_ID") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("TRIAL_LEDGER_KEYRING_INVALID_JSON") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("TRIAL_LEDGER_KEYRING_EMPTY")
        keyring: dict[str, bytes] = {}
        for key_id, secret in parsed.items():
            normalized_id = str(key_id).strip()
            if not normalized_id or len(normalized_id) > 100 or not isinstance(secret, str):
                raise ValueError("TRIAL_LEDGER_KEYRING_ENTRY_INVALID")
            encoded = secret.encode("utf-8")
            if len(encoded) < 32:
                raise ValueError(f"TRIAL_LEDGER_KEY_TOO_SHORT:{normalized_id}")
            keyring[normalized_id] = encoded
        if not active_id or active_id not in keyring:
            raise ValueError("TRIAL_LEDGER_ACTIVE_KEY_ID_MISSING")
        return keyring, active_id

    # Backward-compatible migration path for the original single secret. Its key
    # identifier is explicit and remains verifiable while the operator migrates
    # the secret into the keyring. Do not accept an empty or short key.
    legacy = os.getenv("ATLAS_RESEARCH_TRIAL_LEDGER_HMAC_KEY")
    if legacy:
        encoded = legacy.encode("utf-8")
        if len(encoded) < 32:
            raise ValueError("TRIAL_LEDGER_HMAC_KEY_TOO_SHORT")
        return {"legacy": encoded}, "legacy"
    if active_id:
        raise ValueError("TRIAL_LEDGER_ACTIVE_KEY_ID_WITHOUT_KEYRING")
    return {}, None


def compute_trial_ledger_integrity(
    payload: Mapping[str, Any],
    *,
    sequence: int,
    previous_entry_sha256: str,
    signing_key: str | bytes | None = None,
    signing_key_id: str | None = None,
    assurance_mode: str | None = None,
) -> tuple[str, str | None, str]:
    """Hash payload, sequence, predecessor, and authenticated assurance/key identity."""
    if isinstance(sequence, bool) or int(sequence) != sequence or int(sequence) < 1:
        raise ValueError("ledger sequence must be a positive integer")
    previous = str(previous_entry_sha256 or "")
    if previous and not _sha(previous):
        raise ValueError("previous ledger hash must be an empty value or SHA-256")
    key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
    mode = assurance_mode or (
        "HMAC_SIGNED" if key is not None else "HASH_CHAIN_UNSIGNED"
    )
    key_id = str(signing_key_id or "").strip() if mode == "HMAC_SIGNED" else ""
    if mode not in {"HMAC_SIGNED", "HASH_CHAIN_UNSIGNED"}:
        raise ValueError("unknown trial-ledger assurance mode")
    if mode == "HMAC_SIGNED":
        if not key_id or len(key_id) > 100:
            raise ValueError("HMAC ledger entries require a valid key identifier")
        if key is not None and len(key) < 32:
            raise ValueError("HMAC signing key must contain at least 32 bytes")
    elif key is not None or key_id:
        raise ValueError("unsigned ledger entries cannot carry a signing key or key identifier")
    envelope = {
        "ledger_version": _LEDGER_VERSION,
        "sequence": int(sequence),
        "previous_entry_sha256": previous,
        "assurance_mode": mode,
        "signing_key_id": key_id or None,
        "payload": _ledger_payload(payload),
    }
    canonical = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False, default=str,
    ).encode("utf-8")
    entry_sha = hashlib.sha256(canonical).hexdigest()
    if mode == "HMAC_SIGNED" and key is not None:
        signature = hmac.new(key, entry_sha.encode("ascii"), hashlib.sha256).hexdigest()
        return entry_sha, signature, mode
    return entry_sha, None, mode


def _verify_row_integrity(
    row: ResearchTrialArchive,
    *,
    keyring: Mapping[str, bytes],
) -> tuple[bool, bool, str]:
    """Return (valid, signature_verifiable, reason) for one chain row."""
    mode = str(row.ledger_status or "")
    key_id = str(row.ledger_hmac_key_id or "").strip() or None
    if mode == "HMAC_SIGNED":
        if not key_id:
            return False, False, "SIGNED_ENTRY_KEY_ID_MISSING"
        key = keyring.get(key_id)
        try:
            calculated, _signature, assurance = compute_trial_ledger_integrity(
                _ledger_payload(row),
                sequence=int(row.ledger_sequence),
                previous_entry_sha256=str(row.ledger_previous_sha256 or ""),
                signing_key=key,
                signing_key_id=key_id,
                assurance_mode=mode,
            )
        except (TypeError, ValueError, OverflowError):
            return False, False, "SIGNED_ENTRY_PAYLOAD_INVALID"
        if assurance != mode or not hmac.compare_digest(
            str(row.ledger_entry_sha256 or ""), calculated
        ):
            return False, False, "LEDGER_ENTRY_HASH_MISMATCH"
        if key is None:
            return True, False, "HMAC_KEY_NOT_AVAILABLE"
        if not row.ledger_hmac_sha256 or not hmac.compare_digest(
            str(row.ledger_hmac_sha256),
            hmac.new(key, calculated.encode("ascii"), hashlib.sha256).hexdigest(),
        ):
            return False, True, "LEDGER_HMAC_MISMATCH"
        return True, True, "OK"
    if mode == "HASH_CHAIN_UNSIGNED":
        if key_id or row.ledger_hmac_sha256:
            return False, True, "UNSIGNED_ENTRY_HAS_SIGNING_METADATA"
        try:
            calculated, _signature, assurance = compute_trial_ledger_integrity(
                _ledger_payload(row),
                sequence=int(row.ledger_sequence),
                previous_entry_sha256=str(row.ledger_previous_sha256 or ""),
                assurance_mode=mode,
            )
        except (TypeError, ValueError, OverflowError):
            return False, True, "UNSIGNED_ENTRY_PAYLOAD_INVALID"
        if assurance != mode or not hmac.compare_digest(
            str(row.ledger_entry_sha256 or ""), calculated
        ):
            return False, True, "LEDGER_ENTRY_HASH_MISMATCH"
        return True, True, "OK"
    return False, True, "UNKNOWN_LEDGER_ASSURANCE"


async def _verify_trial_ledger_in_session(
    db, keyring: Mapping[str, bytes] | None = None
) -> dict[str, Any]:
    """Full-history audit intended for explicit admin verification, not every append."""
    keyring = keyring or {}
    rows = (
        await db.execute(
            select(ResearchTrialArchive)
            .where(ResearchTrialArchive.ledger_sequence.is_not(None))
            .order_by(ResearchTrialArchive.ledger_sequence.asc())
        )
    ).scalars().all()
    head = await db.get(ResearchTrialLedgerHead, 1)
    if head is None:
        return {"status": "BROKEN", "reason": "LEDGER_HEAD_MISSING", "entries": len(rows)}
    previous = ""
    expected_sequence = 1
    signed_count = unsigned_count = 0
    unverifiable_key_ids: set[str] = set()
    for row in rows:
        if row.ledger_sequence != expected_sequence:
            return {
                "status": "BROKEN", "reason": "LEDGER_SEQUENCE_GAP",
                "expected_sequence": expected_sequence, "actual_sequence": row.ledger_sequence,
                "entries": len(rows),
            }
        if str(row.ledger_previous_sha256 or "") != previous:
            return {
                "status": "BROKEN", "reason": "LEDGER_PREVIOUS_HASH_MISMATCH",
                "sequence": expected_sequence, "entries": len(rows),
            }
        valid, verifiable, reason = _verify_row_integrity(row, keyring=keyring)
        if not valid:
            return {
                "status": "BROKEN", "reason": reason,
                "sequence": expected_sequence, "entries": len(rows),
            }
        if row.ledger_status == "HMAC_SIGNED":
            signed_count += 1
            if not verifiable:
                unverifiable_key_ids.add(str(row.ledger_hmac_key_id))
        else:
            unsigned_count += 1
        previous = str(row.ledger_entry_sha256 or "")
        expected_sequence += 1
    last_sequence = expected_sequence - 1
    if int(head.last_sequence or 0) != last_sequence:
        return {
            "status": "BROKEN", "reason": "LEDGER_HEAD_SEQUENCE_MISMATCH",
            "head_sequence": head.last_sequence, "verified_sequence": last_sequence,
            "entries": len(rows),
        }
    if str(head.last_entry_sha256 or "") != previous:
        return {"status": "BROKEN", "reason": "LEDGER_HEAD_HASH_MISMATCH", "entries": len(rows)}
    if unverifiable_key_ids:
        status = "HMAC_KEY_MISSING"
    elif signed_count and unsigned_count:
        status = "HASH_CHAIN_VALID_PARTIALLY_SIGNED"
    elif signed_count:
        status = "HMAC_CHAIN_VALID"
    else:
        status = "HASH_CHAIN_VALID_UNSIGNED" if rows else "EMPTY"
    return {
        "status": status,
        "entries": len(rows),
        "signed_entries": signed_count,
        "unsigned_entries": unsigned_count,
        "unverifiable_key_ids": sorted(unverifiable_key_ids),
        "last_sequence": last_sequence,
        "last_entry_sha256": previous or None,
        "hmac_key_ids": sorted({str(row.ledger_hmac_key_id) for row in rows if row.ledger_hmac_key_id}),
        "legacy_unchained_rows_excluded": True,
        "promotion_evidence_complete": False,
    }


async def _verify_trial_ledger_head_in_session(
    db,
    head: ResearchTrialLedgerHead,
    keyring: Mapping[str, bytes],
) -> dict[str, Any]:
    """Constant-size append preflight: verify only the locked head and predecessor."""
    sequence = int(head.last_sequence or 0)
    head_hash = str(head.last_entry_sha256 or "")
    if sequence == 0:
        existing = (
            await db.execute(
                select(ResearchTrialArchive.trial_key)
                .where(ResearchTrialArchive.ledger_sequence.is_not(None))
                .limit(1)
            )
        ).first()
        if head_hash or existing is not None:
            return {"status": "BROKEN", "reason": "EMPTY_LEDGER_HEAD_MISMATCH"}
        return {"status": "EMPTY", "last_sequence": 0}
    if not _sha(head_hash):
        return {"status": "BROKEN", "reason": "LEDGER_HEAD_HASH_INVALID"}
    row = (
        await db.execute(
            select(ResearchTrialArchive)
            .where(ResearchTrialArchive.ledger_sequence == sequence)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(
        str(row.ledger_entry_sha256 or ""), head_hash
    ):
        return {"status": "BROKEN", "reason": "LEDGER_HEAD_ROW_MISSING_OR_MISMATCH"}
    valid, verifiable, reason = _verify_row_integrity(row, keyring=keyring)
    if not valid:
        return {"status": "BROKEN", "reason": reason}
    if sequence > 1:
        predecessor = (
            await db.execute(
                select(ResearchTrialArchive.ledger_entry_sha256)
                .where(ResearchTrialArchive.ledger_sequence == sequence - 1)
                .limit(1)
            )
        ).scalar_one_or_none()
        if not predecessor or not hmac.compare_digest(
            str(row.ledger_previous_sha256 or ""), str(predecessor)
        ):
            return {"status": "BROKEN", "reason": "LEDGER_PREDECESSOR_MISMATCH"}
    if row.ledger_status == "HMAC_SIGNED" and not verifiable:
        return {
            "status": "HMAC_KEY_MISSING",
            "reason": "TRIAL_LEDGER_HISTORICAL_SIGNING_KEY_UNAVAILABLE",
            "key_id": row.ledger_hmac_key_id,
            "last_sequence": sequence,
        }
    return {
        "status": "HEAD_LINK_VALID",
        "last_sequence": sequence,
        "last_entry_sha256": head_hash,
        "last_entry_assurance": row.ledger_status,
        "last_entry_key_id": row.ledger_hmac_key_id,
    }


async def verify_research_trial_ledger() -> dict[str, Any]:
    """Full chain verifier for the admin status endpoint."""
    try:
        keyring, active_key_id = _load_trial_ledger_keyring()
    except ValueError as exc:
        return {"status": "KEYRING_INVALID", "reason": str(exc)}
    async with SessionLocal() as db:
        result = await _verify_trial_ledger_in_session(db, keyring)
    result["active_key_id"] = active_key_id
    result["retained_key_ids"] = sorted(keyring)
    result["key_rotation_supported"] = True
    return result


def _same_immutable_row(existing: ResearchTrialArchive, payload: Mapping[str, Any]) -> bool:
    fields = (
        "trial_key", "trial_label", "asset_class", "venue", "market_type", "symbol",
        "timeframe", "config_sha256", "code_sha256", "dataset_sha256", "oos_sample_sha256",
        "protocol_version", "universe_evidence_sha256", "cost_evidence_id",
        "annualized_sharpe", "oos_total_return", "max_drawdown", "trades", "results_sha256",
        "archive_status",
    )
    return all(getattr(existing, field) == payload[field] for field in fields)


async def archive_research_trials(report: Mapping[str, Any], request: Any) -> dict[str, Any]:
    """Append/replay prospective trials under a serialized, tamper-evident ledger head.

    Legacy rows from before migration 0059 remain LEGACY_UNCHAINED. New records are
    SHA-256 chained; HMAC authenticity is asserted only when an external >=32-byte
    ATLAS_RESEARCH_TRIAL_LEDGER_HMAC_KEY is configured in the backend environment.
    This prospective ledger never replaces the signed comparable archive used for DSR.
    """
    if not isinstance(report, Mapping):
        return {"status": "FAILED", "reason": "RESEARCH_REPORT_INVALID", "inserted": 0, "replayed": 0}
    specs = _trial_specs(report)
    if not specs:
        return {"status": "NOT_RECORDED", "reason": "TRIAL_LABELS_MISSING", "inserted": 0, "replayed": 0}
    context = _report_context(report, request)
    inserted = replayed = unverified = 0
    evaluation_id = uuid.uuid4().hex
    signing_key = os.getenv("ATLAS_RESEARCH_TRIAL_LEDGER_HMAC_KEY") or None
    if signing_key is not None and len(signing_key.encode("utf-8")) < 32:
        return {
            "status": "FAILED", "reason": "TRIAL_LEDGER_HMAC_KEY_TOO_SHORT",
            "inserted": 0, "replayed": 0,
        }
    try:
        async with SessionLocal() as db:
            head = (
                await db.execute(
                    select(ResearchTrialLedgerHead)
                    .where(ResearchTrialLedgerHead.id == 1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if head is None:
                return {
                    "status": "FAILED", "reason": "TRIAL_LEDGER_HEAD_MISSING",
                    "inserted": 0, "replayed": 0,
                }
            integrity_before = await _verify_trial_ledger_in_session(db, signing_key)
            if integrity_before.get("status") == "BROKEN":
                return {
                    "status": "FAILED", "reason": "TRIAL_LEDGER_INTEGRITY_FAILURE",
                    "integrity": integrity_before, "inserted": 0, "replayed": 0,
                }
            if integrity_before.get("status") == "HMAC_KEY_MISSING":
                return {
                    "status": "FAILED", "reason": "TRIAL_LEDGER_HMAC_KEY_REQUIRED_TO_CONTINUE",
                    "integrity": integrity_before, "inserted": 0, "replayed": 0,
                }
            for label, metrics, category in specs:
                payload = _row_payload(report, context, label, metrics, category, evaluation_id)
                existing = await db.get(ResearchTrialArchive, payload["trial_key"])
                if existing is not None:
                    if not _same_immutable_row(existing, payload):
                        return {"status": "FAILED", "reason": "TRIAL_IDEMPOTENCY_CONFLICT",
                                "trial_label": label, "inserted": 0, "replayed": 0}
                    replayed += 1
                    unverified += existing.archive_status != "VERIFIED"
                    continue

                sequence = int(head.last_sequence or 0) + 1
                previous_hash = str(head.last_entry_sha256 or "")
                entry_hash, signature, ledger_status = compute_trial_ledger_integrity(
                    payload, sequence=sequence, previous_entry_sha256=previous_hash,
                    signing_key=signing_key,
                )
                try:
                    # The singleton head row is locked for this transaction. Insert
                    # and head advance share one savepoint to keep sequence/hash atomic.
                    async with db.begin_nested():
                        db.add(ResearchTrialArchive(
                            **payload,
                            ledger_sequence=sequence,
                            ledger_previous_sha256=previous_hash,
                            ledger_entry_sha256=entry_hash,
                            ledger_hmac_sha256=signature,
                            ledger_status=ledger_status,
                        ))
                        head.last_sequence = sequence
                        head.last_entry_sha256 = entry_hash
                        head.updated_at = datetime.now(timezone.utc)
                        await db.flush()
                    inserted += 1
                except IntegrityError:
                    existing = await db.get(ResearchTrialArchive, payload["trial_key"])
                    if existing is None or not _same_immutable_row(existing, payload):
                        return {"status": "FAILED", "reason": "TRIAL_IDEMPOTENCY_CONFLICT",
                                "trial_label": label, "inserted": 0, "replayed": 0}
                    replayed += 1
                    unverified += existing.archive_status != "VERIFIED"
            await db.commit()
    except Exception as exc:
        # The surrounding transaction rolled back; only persisted inserts may be reported.
        return {
            "status": "FAILED",
            "reason": type(exc).__name__,
            "inserted": 0,
            "attempted_inserts": inserted,
            "replayed": 0,
        }
    integrity = await verify_research_trial_ledger()
    unverified += inserted
    return {
        "status": "APPENDED" if inserted else "REPLAYED",
        "inserted": inserted, "replayed": replayed,
        "unverified_records": unverified,
        "prospective_archive_only": True,
        "promotion_evidence_complete": False,
        "ledger_integrity": integrity,
        "ledger_hmac_configured": bool(signing_key),
        "legacy_unchained_rows_excluded": True,
    }
