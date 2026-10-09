from __future__ import annotations

"""Descriptive and chronological holdout analysis of research-only outcome labels."""

import json
import math
from collections import defaultdict
from datetime import timezone
from statistics import mean, median
from typing import Any

import numpy as np
import pandas as pd


def _finite_return(label: dict, horizon: int) -> float | None:
    outcomes = label.get("outcomes")
    if not isinstance(outcomes, dict):
        return None
    outcome = outcomes.get(str(horizon))
    if not isinstance(outcome, dict) or outcome.get("status") != "VALID":
        return None
    value = outcome.get("candidate_side_net_return_bps")
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _metrics(values: list[float], *, min_group_samples: int) -> dict:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return {
            "n": 0,
            "mean_bps": None,
            "median_bps": None,
            "win_rate": None,
            "loss_rate": None,
            "p10_bps": None,
            "p90_bps": None,
            "sample_sufficient": False,
            "minimum_recommended_n": min_group_samples,
        }
    return {
        "n": len(clean),
        "mean_bps": float(mean(clean)),
        "median_bps": float(median(clean)),
        "win_rate": sum(value > 0 for value in clean) / len(clean),
        "loss_rate": sum(value < 0 for value in clean) / len(clean),
        "p10_bps": float(np.percentile(clean, 10)),
        "p90_bps": float(np.percentile(clean, 90)),
        "sample_sufficient": len(clean) >= min_group_samples,
        "minimum_recommended_n": min_group_samples,
    }


def _text(record: dict, key: str, default: str = "UNKNOWN") -> str:
    value = record.get(key)
    return str(value if value not in (None, "") else default)


def _time(value: Any) -> pd.Timestamp | None:
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return None if pd.isna(timestamp) else timestamp
    except Exception:
        return None


def _max_outcome_close(label: dict, horizon: int) -> pd.Timestamp | None:
    outcomes = label.get("outcomes")
    outcome = outcomes.get(str(horizon)) if isinstance(outcomes, dict) else None
    if not isinstance(outcome, dict):
        return None
    opened = _time(outcome.get("outcome_bar_open"))
    if opened is None:
        return None
    try:
        delta = pd.Timedelta(str(label.get("timeframe") or ""))
    except (TypeError, ValueError):
        return None
    if pd.isna(delta) or delta <= pd.Timedelta(0):
        return None
    return opened + delta


def _normalise_labels(records: list[dict]) -> tuple[list[dict], dict]:
    valid = []
    rejected = 0
    invalid = 0
    missing_side = 0
    for record in records:
        if not isinstance(record, dict) or record.get("record_type") != "adaptive_decision_outcome_label":
            invalid += 1
            continue
        if record.get("status") != "VALID":
            rejected += 1
            continue
        decision = str(record.get("original_decision") or "").upper()
        if decision not in {"TRADE", "NO_TRADE"}:
            rejected += 1
            continue
        if not isinstance(record.get("outcomes"), dict):
            invalid += 1
            continue
        item = dict(record)
        item["original_decision"] = decision
        item["candidate_side"] = str(record.get("candidate_side") or "").lower() or None
        if item["candidate_side"] not in {"buy", "sell"}:
            item["candidate_side"] = None
            missing_side += 1
        valid.append(item)
    return valid, {
        "input_records": len(records),
        "valid_labels": len(valid),
        "ignored_non_valid_or_non_decision_labels": rejected,
        "invalid_records": invalid,
        "valid_labels_without_candidate_side": missing_side,
    }


def evaluate_decision_outcome_labels(
    records: list[dict],
    *,
    holdout_fraction: float = 0.30,
    min_group_samples: int = 30,
    min_unique_timestamps: int = 10,
) -> dict:
    """Summarize rejected-signal outcomes and create a purged chronological holdout.

    This is descriptive research, not a hypothesis test or strategy promotion gate.
    Training-side labels whose outcome close reaches the holdout start are purged.
    """
    if not 0.1 <= float(holdout_fraction) <= 0.5:
        raise ValueError("holdout_fraction must be between 0.1 and 0.5")
    min_group_samples = max(1, int(min_group_samples))
    min_unique_timestamps = max(3, int(min_unique_timestamps))
    labels, coverage = _normalise_labels(records)

    decision_counts: dict[str, int] = defaultdict(int)
    mode_counts: dict[str, int] = defaultdict(int)
    candidate_side_counts: dict[str, int] = defaultdict(int)
    for label in labels:
        decision_counts[label["original_decision"]] += 1
        mode_counts[_text(label, "mode")] += 1
        candidate_side_counts[label["candidate_side"] or "UNAVAILABLE"] += 1

    grouped: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    rejected_grouped: dict[tuple, list[float]] = defaultdict(list)
    for label in labels:
        if label["candidate_side"] is None:
            continue
        strategy = _text(label, "strategy")
        regime = _text(label, "regime")
        for horizon_key, outcome in label["outcomes"].items():
            try:
                horizon = int(horizon_key)
            except (TypeError, ValueError):
                continue
            value = _finite_return(label, horizon)
            if value is None:
                continue
            key = (
                _text(label, "asset"), _text(label, "timeframe"), _text(label, "mode"),
                horizon, label["candidate_side"], strategy, regime,
            )
            grouped[key][label["original_decision"]].append(value)
            if label["original_decision"] == "NO_TRADE":
                rejection_key = key + (
                    _text(label, "decision_stage"),
                    _text(label, "decision_reason_code", "UNRECORDED"),
                )
                rejected_grouped[rejection_key].append(value)

    by_market = []
    for key, decision_metrics in sorted(grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        asset, timeframe, mode, horizon, side, strategy, regime = key
        by_market.append({
            "asset": asset,
            "timeframe": timeframe,
            "mode": mode,
            "horizon_bars": horizon,
            "candidate_side": side,
            "strategy": strategy,
            "regime": regime,
            "by_decision": {
                decision: _metrics(values, min_group_samples=min_group_samples)
                for decision, values in sorted(decision_metrics.items())
            },
        })

    rejected_by_reason = []
    for key, values in sorted(rejected_grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        asset, timeframe, mode, horizon, side, strategy, regime, stage, reason = key
        rejected_by_reason.append({
            "asset": asset,
            "timeframe": timeframe,
            "mode": mode,
            "horizon_bars": horizon,
            "candidate_side": side,
            "strategy": strategy,
            "regime": regime,
            "decision_stage": stage,
            "decision_reason_code": reason,
            "metrics": _metrics(values, min_group_samples=min_group_samples),
        })

    oos_groups: dict[tuple, list[tuple[pd.Timestamp, dict, float]]] = defaultdict(list)
    invalid_oos_timestamps = 0
    for label in labels:
        if label["candidate_side"] is None:
            continue
        timestamp = _time(label.get("market_timestamp"))
        if timestamp is None:
            invalid_oos_timestamps += 1
            continue
        for horizon_key in (label.get("horizons_bars") or []):
            try:
                horizon = int(horizon_key)
            except (TypeError, ValueError):
                continue
            value = _finite_return(label, horizon)
            if value is None:
                continue
            key = (
                _text(label, "asset"), _text(label, "timeframe"), _text(label, "mode"),
                horizon, label["candidate_side"], _text(label, "strategy"), _text(label, "regime"),
            )
            oos_groups[key].append((timestamp, label, value))

    out_of_sample = []
    for key, entries in sorted(oos_groups.items(), key=lambda item: tuple(str(x) for x in item[0])):
        asset, timeframe, mode, horizon, side, strategy, regime = key
        entries.sort(key=lambda item: item[0])
        unique_times = sorted({item[0] for item in entries})
        base = {
            "asset": asset,
            "timeframe": timeframe,
            "mode": mode,
            "horizon_bars": horizon,
            "candidate_side": side,
            "strategy": strategy,
            "regime": regime,
            "n_labels": len(entries),
            "n_unique_decision_timestamps": len(unique_times),
        }
        if len(unique_times) < min_unique_timestamps:
            out_of_sample.append({
                **base,
                "status": "INSUFFICIENT_UNIQUE_TIMESTAMPS",
                "minimum_unique_timestamps": min_unique_timestamps,
                "holdout_start": None,
                "n_train_after_purge": 0,
                "n_purged_train_labels": 0,
                "n_holdout": 0,
                "train_by_decision": {},
                "holdout_by_decision": {},
            })
            continue

        split_position = int((1.0 - float(holdout_fraction)) * len(unique_times))
        split_position = max(1, min(len(unique_times) - 1, split_position))
        holdout_start = unique_times[split_position]
        train_candidates = [item for item in entries if item[0] < holdout_start]
        holdout_entries = [item for item in entries if item[0] >= holdout_start]
        train_kept = []
        purged = 0
        for item in train_candidates:
            end_close = _max_outcome_close(item[1], horizon)
            if end_close is None or end_close >= holdout_start:
                purged += 1
            else:
                train_kept.append(item)

        def by_decision(items: list[tuple[pd.Timestamp, dict, float]]) -> dict:
            result = {}
            for decision in ("TRADE", "NO_TRADE"):
                values = [value for _, label, value in items if label["original_decision"] == decision]
                result[decision] = _metrics(values, min_group_samples=min_group_samples)
            return result

        out_of_sample.append({
            **base,
            "status": "CHRONOLOGICAL_HOLDOUT",
            "holdout_fraction": float(holdout_fraction),
            "holdout_start": holdout_start.isoformat(),
            "n_train_before_purge": len(train_candidates),
            "n_train_after_purge": len(train_kept),
            "n_purged_train_labels": purged,
            "n_holdout": len(holdout_entries),
            "train_by_decision": by_decision(train_kept),
            "holdout_by_decision": by_decision(holdout_entries),
            "interpretation": "descriptive_only; not a significance test or profitability claim",
        })

    return {
        "report_version": "rejected-signal-evaluation-v1",
        "research_only": True,
        "automatic_promotion": False,
        "methodology": {
            "outcome_metric": "candidate-side hypothetical net return in bps",
            "costs": "label-time configured taker/slippage plus per-bar carry assumptions",
            "holdout": "chronological; training labels whose outcome close reaches holdout start are purged",
            "minimum_group_samples": min_group_samples,
            "minimum_unique_timestamps_for_holdout": min_unique_timestamps,
            "not_a_profitability_or_significance_claim": True,
        },
        "coverage": {
            **coverage,
            "decision_counts": dict(sorted(decision_counts.items())),
            "mode_counts": dict(sorted(mode_counts.items())),
            "candidate_side_counts": dict(sorted(candidate_side_counts.items())),
            "invalid_holdout_timestamps": invalid_oos_timestamps,
        },
        "by_market_and_decision": by_market,
        "rejected_by_reason": rejected_by_reason,
        "out_of_sample": out_of_sample,
    }


async def load_decision_outcome_labels(*, limit: int = 10_000) -> list[dict]:
    """Read encrypted outcome-label audit records without writing to the database."""
    from sqlalchemy import select
    from .db import AuditLog, SessionLocal

    limit = max(1, min(100_000, int(limit)))
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.event == "ADAPTIVE_DECISION_OUTCOME_LABEL")
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(limit)
            )
        ).scalars().all()
    result = []
    for row in reversed(rows):
        detail = row.detail
        if isinstance(detail, dict):
            record = detail
        elif isinstance(detail, str):
            try:
                record = json.loads(detail)
            except (TypeError, ValueError):
                continue
        else:
            continue
        if isinstance(record, dict):
            result.append(record)
    return result
