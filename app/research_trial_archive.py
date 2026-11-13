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
from typing import Any, Mapping

from sqlalchemy import select
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
                prefix = f"variant:{variant.get('multiplier')}@"
                if label.startswith(prefix):
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
_LEDGER_VERSION = "atlas-research-trial-ledger-v1"


def _ledger_payload(value: Mapping[str, Any] | ResearchTrialArchive) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {field: value.get(field) for field in _LEDGER_PAYLOAD_FIELDS}
    return {field: getattr(value, field) for field in _LEDGER_PAYLOAD_FIELDS}


def compute_trial_ledger_integrity(
    payload: Mapping[str, Any],
    *,
    sequence: int,
    previous_entry_sha256: str,
    signing_key: str | bytes | None = None,
) -> tuple[str, str | None, str]:
    """Return deterministic entry SHA, optional HMAC, and explicit assurance level."""
    if isinstance(sequence, bool) or int(sequence) != sequence or int(sequence) < 1:
        raise ValueError("ledger sequence must be a positive integer")
    previous = str(previous_entry_sha256 or "")
    if previous and not _sha(previous):
        raise ValueError("previous ledger hash must be an empty value or SHA-256")
    envelope = {
        "ledger_version": _LEDGER_VERSION,
        "sequence": int(sequence),
        "previous_entry_sha256": previous,
        "payload": _ledger_payload(payload),
    }
    canonical = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False, default=str,
    ).encode("utf-8")
    entry_sha = hashlib.sha256(canonical).hexdigest()
    key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
    if key and len(key) >= 32:
        signature = hmac.new(key, entry_sha.encode("ascii"), hashlib.sha256).hexdigest()
        return entry_sha, signature, "HMAC_SIGNED"
    return entry_sha, None, "HASH_CHAIN_UNSIGNED"


async def _verify_trial_ledger_in_session(db, signing_key: str | bytes | None = None) -> dict[str, Any]:
    """Verify the complete prospective chain and the database head under one session."""
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
    key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
    previous = ""
    expected_sequence = 1
    signed_count = unsigned_count = 0
    signature_unverifiable = False
    for row in rows:
        if row.ledger_sequence != expected_sequence:
            return {
                "status": "BROKEN", "reason": "LEDGER_SEQUENCE_GAP",
                "expected_sequence": expected_sequence, "actual_sequence": row.ledger_sequence,
                "entries": len(rows),
            }
        if str(row.ledger_previous_sha256 or "") != previous:
            return {"status": "BROKEN", "reason": "LEDGER_PREVIOUS_HASH_MISMATCH",
                    "sequence": expected_sequence, "entries": len(rows)}
        try:
            calculated, signature, assurance = compute_trial_ledger_integrity(
                _ledger_payload(row),
                sequence=expected_sequence,
                previous_entry_sha256=previous,
                signing_key=key if key and len(key) >= 32 else None,
            )
        except (TypeError, ValueError, OverflowError):
            return {"status": "BROKEN", "reason": "LEDGER_PAYLOAD_INVALID",
                    "sequence": expected_sequence, "entries": len(rows)}
        if not hmac.compare_digest(str(row.ledger_entry_sha256 or ""), calculated):
            return {"status": "BROKEN", "reason": "LEDGER_ENTRY_HASH_MISMATCH",
                    "sequence": expected_sequence, "entries": len(rows)}
        if row.ledger_status == "HMAC_SIGNED":
            signed_count += 1
            if not key or len(key) < 32:
                signature_unverifiable = True
            elif not row.ledger_hmac_sha256 or not hmac.compare_digest(
                str(row.ledger_hmac_sha256), hmac.new(
                    key, calculated.encode("ascii"), hashlib.sha256
                ).hexdigest()
            ):
                return {"status": "BROKEN", "reason": "LEDGER_HMAC_MISMATCH",
                        "sequence": expected_sequence, "entries": len(rows)}
        elif row.ledger_status == "HASH_CHAIN_UNSIGNED":
            unsigned_count += 1
            if row.ledger_hmac_sha256:
                return {"status": "BROKEN", "reason": "UNEXPECTED_LEDGER_HMAC",
                        "sequence": expected_sequence, "entries": len(rows)}
        else:
            return {"status": "BROKEN", "reason": "UNKNOWN_LEDGER_ASSURANCE",
                    "sequence": expected_sequence, "entries": len(rows)}
        previous = calculated
        expected_sequence += 1
    last_sequence = expected_sequence - 1
    if int(head.last_sequence or 0) != last_sequence:
        return {"status": "BROKEN", "reason": "LEDGER_HEAD_SEQUENCE_MISMATCH",
                "head_sequence": head.last_sequence, "verified_sequence": last_sequence,
                "entries": len(rows)}
    if str(head.last_entry_sha256 or "") != previous:
        return {"status": "BROKEN", "reason": "LEDGER_HEAD_HASH_MISMATCH", "entries": len(rows)}
    if signature_unverifiable:
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
        "last_sequence": last_sequence,
        "last_entry_sha256": previous or None,
        "hmac_key_configured": bool(key and len(key) >= 32),
        "legacy_unchained_rows_excluded": True,
        "promotion_evidence_complete": False,
    }


async def verify_research_trial_ledger() -> dict[str, Any]:
    """Verify the prospective append-only trial chain without certifying legacy rows."""
    key = os.getenv("ATLAS_RESEARCH_TRIAL_LEDGER_HMAC_KEY") or None
    async with SessionLocal() as db:
        return await _verify_trial_ledger_in_session(db, key)


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
        return {"status": "FAILED", "reason": type(exc).__name__, "inserted": inserted, "replayed": replayed}
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
