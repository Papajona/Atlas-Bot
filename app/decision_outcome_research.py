from __future__ import annotations

"""Descriptive and chronological holdout analysis of research-only outcome labels."""

import json
import math
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from collections import defaultdict
from statistics import mean, median
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


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
    # The loader returns records chronologically. Keep the latest persisted audit
    # row for each decision/version pair so retries cannot overweight one decision.
    latest_by_decision: dict[tuple[str, str], dict] = {}
    rejected = 0
    invalid = 0
    duplicate_labels = 0
    for record in records:
        if not isinstance(record, dict) or record.get("record_type") != "adaptive_decision_outcome_label":
            invalid += 1
            continue
        if record.get("status") != "VALID":
            rejected += 1
            continue
        decision = str(record.get("original_decision") or "").upper()
        decision_id = str(record.get("decision_id") or "").strip()
        label_version = str(record.get("label_version") or "").strip()
        if decision not in {"TRADE", "NO_TRADE"}:
            rejected += 1
            continue
        if not decision_id or not label_version or not isinstance(record.get("outcomes"), dict):
            invalid += 1
            continue
        item = dict(record)
        item["original_decision"] = decision
        item["candidate_side"] = str(record.get("candidate_side") or "").lower() or None
        if item["candidate_side"] not in {"buy", "sell"}:
            item["candidate_side"] = None
        for cost_key in ("cost_bps_per_side", "carry_bps_per_bar"):
            raw_cost = item.get(cost_key)
            if raw_cost in (None, ""):
                item[cost_key] = None
                continue
            try:
                parsed_cost = float(raw_cost)
            except (TypeError, ValueError):
                invalid += 1
                break
            if not math.isfinite(parsed_cost) or parsed_cost < 0:
                invalid += 1
                break
            item[cost_key] = parsed_cost
        else:
            identity = (label_version, decision_id)
            if identity in latest_by_decision:
                duplicate_labels += 1
            latest_by_decision[identity] = item

    valid = list(latest_by_decision.values())
    missing_side = sum(1 for item in valid if item["candidate_side"] is None)
    label_versions: dict[str, int] = defaultdict(int)
    for item in valid:
        label_versions[item["label_version"]] += 1
    return valid, {
        "input_records": len(records),
        "valid_labels": len(valid),
        "ignored_non_valid_or_non_decision_labels": rejected,
        "invalid_records": invalid,
        "valid_labels_without_candidate_side": missing_side,
        "duplicate_decision_labels": duplicate_labels,
        "label_version_counts": dict(sorted(label_versions.items())),
    }

def evaluate_decision_outcome_labels(
    records: list[dict],
    *,
    holdout_fraction: float = 0.30,
    min_group_samples: int = 30,
    min_unique_timestamps: int = 10,
    redact_holdout: bool = True,
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
                _text(label, "asset"),
                _text(label, "symbol"),
                _text(label, "exchange"),
                _text(label, "timeframe"),
                _text(label, "mode"),
                _text(label, "label_version"),
                _text(label, "cost_source", "UNKNOWN"),
                label.get("cost_bps_per_side"),
                label.get("carry_bps_per_bar"),
                horizon,
                label["candidate_side"],
                strategy,
                regime,
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
        (
            asset, symbol, exchange, timeframe, mode, label_version, cost_source,
            cost_bps_per_side, carry_bps_per_bar, horizon, side, strategy, regime,
        ) = key
        by_market.append({
            "asset": asset,
            "symbol": symbol,
            "exchange": exchange,
            "timeframe": timeframe,
            "mode": mode,
            "label_version": label_version,
            "cost_source": cost_source,
            "cost_bps_per_side": cost_bps_per_side,
            "carry_bps_per_bar": carry_bps_per_bar,
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
        (
            asset, symbol, exchange, timeframe, mode, label_version, cost_source,
            cost_bps_per_side, carry_bps_per_bar, horizon, side, strategy, regime,
            stage, reason,
        ) = key
        rejected_by_reason.append({
            "asset": asset,
            "symbol": symbol,
            "exchange": exchange,
            "timeframe": timeframe,
            "mode": mode,
            "label_version": label_version,
            "cost_source": cost_source,
            "cost_bps_per_side": cost_bps_per_side,
            "carry_bps_per_bar": carry_bps_per_bar,
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
                _text(label, "asset"),
                _text(label, "symbol"),
                _text(label, "exchange"),
                _text(label, "timeframe"),
                _text(label, "mode"),
                _text(label, "label_version"),
                _text(label, "cost_source", "UNKNOWN"),
                label.get("cost_bps_per_side"),
                label.get("carry_bps_per_bar"),
                horizon,
                label["candidate_side"],
                _text(label, "strategy"),
                _text(label, "regime"),
            )
            oos_groups[key].append((timestamp, label, value))

    out_of_sample = []
    for key, entries in sorted(oos_groups.items(), key=lambda item: tuple(str(x) for x in item[0])):
        (
            asset, symbol, exchange, timeframe, mode, label_version, cost_source,
            cost_bps_per_side, carry_bps_per_bar, horizon, side, strategy, regime,
        ) = key
        entries.sort(key=lambda item: item[0])
        unique_times = sorted({item[0] for item in entries})
        base = {
            "asset": asset,
            "symbol": symbol,
            "exchange": exchange,
            "timeframe": timeframe,
            "mode": mode,
            "label_version": label_version,
            "cost_source": cost_source,
            "cost_bps_per_side": cost_bps_per_side,
            "carry_bps_per_bar": carry_bps_per_bar,
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
        "report_version": "rejected-signal-evaluation-v2",
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
        # The holdout is withheld by default. Only the atomic one-use evaluator
        # opts in to computing and releasing it after registry consumption.
        "out_of_sample": [] if redact_holdout else out_of_sample,
        "holdout_metrics_withheld": bool(redact_holdout),
    }


async def load_decision_outcome_labels(*, limit: int = 10_000) -> list[dict]:
    """Read encrypted outcome-label audit records without writing to the database."""
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



HOLDOUT_REGISTRY_VERSION = "decision-outcome-one-time-holdout-v1"
_HOLDOUT_EVALUATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="atlas-decision-holdout"
)
_HOLDOUT_EVALUATION_GATE = threading.BoundedSemaphore(1)


async def evaluate_decision_outcome_labels_once(
    records: list[dict],
    *,
    holdout_fraction: float = 0.30,
    min_group_samples: int = 30,
    min_unique_timestamps: int = 10,
) -> dict:
    """Consume and release the global chronological holdout once, across all workers.

    The fixed registry scope is independent of the current input corpus and requested
    fraction. Once a usable holdout is consumed, later calls return no held-out metrics,
    even when new labels, parameters, or data are added.
    """
    from .db import ResearchHoldoutRegistry, SessionLocal
    from .research_evidence import canonical_sha256

    scope_sha = canonical_sha256({
        "registry": "atlas-global-decision-outcome-holdout",
        "version": HOLDOUT_REGISTRY_VERSION,
    })
    async with SessionLocal() as db:
        existing = await db.get(ResearchHoldoutRegistry, scope_sha)
        if existing is not None:
            return {
                "status": "HOLDOUT_ALREADY_CONSUMED",
                "holdout_version": existing.holdout_version,
                "consumed_at": existing.consumed_at.isoformat() if existing.consumed_at else None,
                "metrics_released": False,
                "report": None,
                "reason": "one_time_holdout_is_global_and_cannot_be_reopened",
            }

    if not _HOLDOUT_EVALUATION_GATE.acquire(blocking=False):
        return {
            "status": "HOLDOUT_EVALUATION_BUSY",
            "holdout_version": HOLDOUT_REGISTRY_VERSION,
            "metrics_released": False,
            "consumed": False,
            "report": None,
            "reason": "bounded_holdout_worker_is_already_processing_a_request",
        }
    try:
        from functools import partial
        loop = asyncio.get_running_loop()
        report = await loop.run_in_executor(
            _HOLDOUT_EVALUATION_EXECUTOR,
            partial(
                evaluate_decision_outcome_labels,
                records,
                holdout_fraction=holdout_fraction,
                min_group_samples=min_group_samples,
                min_unique_timestamps=min_unique_timestamps,
                redact_holdout=False,
            ),
        )
    finally:
        _HOLDOUT_EVALUATION_GATE.release()
    usable = [
        row for row in report.get("out_of_sample", [])
        if isinstance(row, dict)
        and row.get("status") == "CHRONOLOGICAL_HOLDOUT"
        and int(row.get("n_holdout", 0) or 0) > 0
    ]
    adequately_sampled = []
    for row in usable:
        by_decision = row.get("holdout_by_decision")
        if not isinstance(by_decision, dict):
            continue
        trade = by_decision.get("TRADE")
        no_trade = by_decision.get("NO_TRADE")
        # Both sides of the decision must meet the sample floor independently.
        # A large majority class may not consume the one-time holdout for a thin arm.
        if not isinstance(trade, dict) or not isinstance(no_trade, dict):
            continue
        minimum = max(1, int(min_group_samples))
        if (
            int(trade.get("n", 0) or 0) >= minimum
            and int(no_trade.get("n", 0) or 0) >= minimum
            and trade.get("sample_sufficient") is True
            and no_trade.get("sample_sufficient") is True
        ):
            adequately_sampled.append(row)
    if not adequately_sampled:
        return {
            "status": "INSUFFICIENT_HOLDOUT",
            "holdout_version": HOLDOUT_REGISTRY_VERSION,
            "metrics_released": False,
            "consumed": False,
            "usable_holdout_groups": len(usable),
            "adequately_sampled_holdout_groups": len(adequately_sampled),
            "report": None,
            "reason": "no_chronological_holdout_group_meets_the_minimum_sample_floor",
        }

    # Release only independently sufficient groups, never the thin-arm groups.
    report["out_of_sample"] = adequately_sampled
    normalized, _coverage = _normalise_labels(records)
    # Bind all normalized metadata and every horizon outcome, not merely label IDs.
    # This makes a changed return, execution cost, timestamp, or decision field a new
    # sample fingerprint rather than silently replaying the prior holdout.
    sample_identity = sorted(canonical_sha256(row) for row in normalized)
    source_sample_sha = canonical_sha256(sample_identity)
    report_sha = canonical_sha256(report)
    plan = [
        {
            "asset": row.get("asset"),
            "symbol": row.get("symbol"),
            "exchange": row.get("exchange"),
            "timeframe": row.get("timeframe"),
            "label_version": row.get("label_version"),
            "horizon_bars": row.get("horizon_bars"),
            "holdout_start": row.get("holdout_start"),
            "n_train_before_purge": row.get("n_train_before_purge"),
            "n_train_after_purge": row.get("n_train_after_purge"),
            "n_purged_train_labels": row.get("n_purged_train_labels"),
            "n_holdout": row.get("n_holdout"),
        }
        for row in usable
    ]
    plan_json = json.dumps(
        {"version": HOLDOUT_REGISTRY_VERSION, "groups": plan},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    try:
        async with SessionLocal() as db:
            # A primary-key insert is the final race arbiter. The immutable database
            # trigger prevents any worker from changing or resetting this consumption.
            existing = await db.get(ResearchHoldoutRegistry, scope_sha)
            if existing is not None:
                return {
                    "status": "HOLDOUT_ALREADY_CONSUMED",
                    "holdout_version": existing.holdout_version,
                    "consumed_at": existing.consumed_at.isoformat() if existing.consumed_at else None,
                    "metrics_released": False,
                    "report": None,
                    "reason": "one_time_holdout_is_global_and_cannot_be_reopened",
                }
            async with db.begin_nested():
                db.add(ResearchHoldoutRegistry(
                    scope_sha256=scope_sha,
                    holdout_version=HOLDOUT_REGISTRY_VERSION,
                    source_sample_sha256=source_sample_sha,
                    report_sha256=report_sha,
                    holdout_plan_json=plan_json,
                    consumed_at=datetime.now(timezone.utc),
                ))
                await db.flush()
            await db.commit()
    except IntegrityError:
        # Another request consumed the global scope concurrently. Never return this
        # request's report after losing the insert race.
        return {
            "status": "HOLDOUT_ALREADY_CONSUMED",
            "holdout_version": HOLDOUT_REGISTRY_VERSION,
            "metrics_released": False,
            "report": None,
            "reason": "concurrent_request_consumed_the_one_time_holdout",
        }
    except Exception as exc:
        return {
            "status": "HOLDOUT_CONSUMPTION_FAILED",
            "holdout_version": HOLDOUT_REGISTRY_VERSION,
            "metrics_released": False,
            "report": None,
            "reason": type(exc).__name__,
        }

    return {
        "status": "HOLDOUT_CONSUMED_AND_RELEASED",
        "holdout_version": HOLDOUT_REGISTRY_VERSION,
        "consumed": True,
        "consumed_at": datetime.now(timezone.utc).isoformat(),
        "metrics_released": True,
        "source_sample_sha256": source_sample_sha,
        "report_sha256": report_sha,
        "report": report,
        "warning": "This global holdout is permanently consumed. Subsequent calls return no held-out metrics.",
    }



async def decision_outcome_holdout_status() -> dict[str, Any]:
    """Constant-size consumed-state check so repeat requests do not reload label history."""
    from .db import ResearchHoldoutRegistry, SessionLocal
    from .research_evidence import canonical_sha256

    scope_sha = canonical_sha256({
        "registry": "atlas-global-decision-outcome-holdout",
        "version": HOLDOUT_REGISTRY_VERSION,
    })
    async with SessionLocal() as db:
        existing = await db.get(ResearchHoldoutRegistry, scope_sha)
    if existing is None:
        return {"status": "AVAILABLE", "consumed": False}
    return {
        "status": "HOLDOUT_ALREADY_CONSUMED",
        "consumed": True,
        "holdout_version": existing.holdout_version,
        "consumed_at": existing.consumed_at.isoformat() if existing.consumed_at else None,
        "metrics_released": False,
    }


class DecisionOutcomeHistoryResourceLimit(RuntimeError):
    """The complete audit history exceeds the configured in-memory research budget."""


async def load_all_decision_outcome_labels(
    *,
    batch_size: int = 2000,
    max_records: int = 100_000,
    max_decoded_bytes: int = 128 * 1024 * 1024,
) -> list[dict]:
    """Read all labels in bounded scalar batches; fail closed instead of truncating.

    The query never retains ORM rows or a whole-table result. Decoded JSON remains
    in memory for the current evaluator, so a hard record ceiling prevents an
    unexpectedly large history from exhausting the API process. The caller must
    leave the holdout unconsumed when this limit is reached.
    """
    from .db import AuditLog, SessionLocal

    batch_size = max(100, min(5000, int(batch_size)))
    max_records = max(1, min(250_000, int(max_records)))
    max_decoded_bytes = max(1_048_576, min(256 * 1024 * 1024, int(max_decoded_bytes)))
    result: list[dict] = []
    decoded_bytes = 0
    before_id = None
    async with SessionLocal() as db:
        while True:
            query = (
                select(AuditLog.id, AuditLog.detail)
                .where(AuditLog.event == "ADAPTIVE_DECISION_OUTCOME_LABEL")
                .order_by(AuditLog.id.asc())
                .limit(batch_size)
            )
            if before_id is not None:
                query = query.where(AuditLog.id > before_id)
            batch = (await db.execute(query)).all()
            if not batch:
                break
            before_id = int(batch[-1][0])
            for _row_id, detail in batch:
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
                    try:
                        record_bytes = len(json.dumps(
                            record, sort_keys=True, separators=(",", ":"), allow_nan=False,
                        ).encode("utf-8"))
                    except (TypeError, ValueError, OverflowError):
                        raise DecisionOutcomeHistoryResourceLimit(
                            "decision label contains non-serializable or non-finite data; "
                            "holdout was not consumed"
                        )
                    decoded_bytes += record_bytes
                    if len(result) + 1 > max_records or decoded_bytes > max_decoded_bytes:
                        raise DecisionOutcomeHistoryResourceLimit(
                            "complete decision label history exceeds the bounded "
                            f"resource budget ({max_records} records / {max_decoded_bytes} bytes); "
                            "holdout was not consumed"
                        )
                    result.append(record)
            if len(batch) < batch_size:
                break
    return result
