"""Append-only recording of prospective research trials.

This registry preserves what Atlas evaluates from this commit onward. Its rows
are explicitly UNVERIFIED and cannot replace the signed comparable historical
archive consumed by the promotion gate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from typing import Any, Mapping

from sqlalchemy.exc import IntegrityError

from .db import ResearchTrialArchive, SessionLocal
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
    config = report.get("config")
    config = config if isinstance(config, Mapping) else {}
    config_sha = hashlib.sha256(
        json.dumps(config, default=str, sort_keys=True).encode("utf-8")
    ).hexdigest() if config else ""
    # ATLAS_CODE_SHA256 must be a digest of the source bundle/release artifact, not
    # merely a git commit label. GITHUB_SHA is a commit identifier, not this hash.
    code_sha = str(os.getenv("ATLAS_CODE_SHA256") or report.get("code_sha256") or "").strip().lower()
    dataset_sha = context["dataset_sha256"]
    sample_sha = str(validation.get("oos_sample_sha256") or "").strip().lower()
    protocol_version = str(context.get("protocol_version") or validation.get("protocol_version") or report.get("protocol_version") or "").strip()
    universe = validation.get("crypto_universe_validation")
    universe = universe if isinstance(universe, Mapping) else {}
    cost = validation.get("execution_cost_calibration")
    cost = cost if isinstance(cost, Mapping) else {}
    sharpe = _finite(metrics.get("sharpe", metrics.get("oos_sharpe", metrics.get("annualized_sharpe"))))
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
    """Append or idempotently replay each newly evaluated trial; never certify completeness."""
    if not isinstance(report, Mapping):
        return {"status": "FAILED", "reason": "RESEARCH_REPORT_INVALID", "inserted": 0, "replayed": 0}
    specs = _trial_specs(report)
    if not specs:
        return {"status": "NOT_RECORDED", "reason": "TRIAL_LABELS_MISSING", "inserted": 0, "replayed": 0}
    context = _report_context(report, request)
    inserted = replayed = unverified = 0
    evaluation_id = uuid.uuid4().hex
    try:
        async with SessionLocal() as db:
            for label, metrics, category in specs:
                payload = _row_payload(report, context, label, metrics, category, evaluation_id)
                existing = await db.get(ResearchTrialArchive, payload["trial_key"])
                if existing is not None:
                    if not _same_immutable_row(existing, payload):
                        # The outer transaction has not committed any new trial rows.
                        return {"status": "FAILED", "reason": "TRIAL_IDEMPOTENCY_CONFLICT",
                                "trial_label": label, "inserted": 0, "replayed": 0}
                    replayed += 1
                    unverified += existing.archive_status != "VERIFIED"
                    continue
                try:
                    # Isolate a uniqueness race to this row. Rolling back the whole
                    # session here silently discards earlier trial rows in this batch.
                    async with db.begin_nested():
                        db.add(ResearchTrialArchive(**payload))
                        await db.flush()
                    inserted += 1
                except IntegrityError:
                    existing = await db.get(ResearchTrialArchive, payload["trial_key"])
                    if existing is None or not _same_immutable_row(existing, payload):
                        # The surrounding transaction is abandoned on return, so do not
                        # claim prior flushes were committed.
                        return {"status": "FAILED", "reason": "TRIAL_IDEMPOTENCY_CONFLICT",
                                "trial_label": label, "inserted": 0, "replayed": 0}
                    replayed += 1
                    unverified += existing.archive_status != "VERIFIED"
            await db.commit()
    except Exception as exc:
        return {"status": "FAILED", "reason": type(exc).__name__, "inserted": inserted, "replayed": replayed}
    unverified += inserted
    return {
        "status": "APPENDED" if inserted else "REPLAYED",
        "inserted": inserted, "replayed": replayed,
        "unverified_records": unverified,
        "prospective_archive_only": True,
        "promotion_evidence_complete": False,
    }
