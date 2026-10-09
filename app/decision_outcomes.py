from __future__ import annotations

"""Research-only forward outcome labels for recorded Atlas adaptive decisions.

Labels are appended as separate encrypted audit events. They are not model features,
do not mutate the original decision record, and do not trigger training or promotion.
"""

import asyncio
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select


LABEL_VERSION = "decision-outcome-v1"
DEFAULT_HORIZONS_BARS = (1, 3, 6)
_SUPPORTED_YFINANCE_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "1d"}
_SUPPORTED_OANDA_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}


class OutcomeLabelError(ValueError):
    """A deterministic reason a decision cannot be labelled from verified data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utc_timestamp(value: Any) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        if pd.isna(timestamp):
            raise ValueError("NaT")
        return timestamp
    except Exception as exc:
        raise OutcomeLabelError("INVALID_TIMESTAMP", "Timestamp is missing or invalid") from exc


def _timeframe_delta(timeframe: str) -> pd.Timedelta:
    try:
        delta = pd.Timedelta(str(timeframe))
    except (TypeError, ValueError) as exc:
        raise OutcomeLabelError("UNSUPPORTED_TIMEFRAME", f"Unsupported timeframe: {timeframe!r}") from exc
    if pd.isna(delta) or delta <= pd.Timedelta(0):
        raise OutcomeLabelError("UNSUPPORTED_TIMEFRAME", f"Unsupported timeframe: {timeframe!r}")
    return delta


def _validate_horizons(horizons: tuple[int, ...] | list[int]) -> tuple[int, ...]:
    try:
        values = tuple(sorted({int(value) for value in horizons}))
    except (TypeError, ValueError) as exc:
        raise OutcomeLabelError("INVALID_HORIZON", "Horizons must be positive integer bar counts") from exc
    if not values or any(value <= 0 or value > 1000 for value in values):
        raise OutcomeLabelError("INVALID_HORIZON", "Horizons must be between 1 and 1000 bars")
    return values


def _candidate_side(record: dict) -> str | None:
    context = record.get("decision_context")
    context = context if isinstance(context, dict) else {}
    analysis = context.get("analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    router = context.get("strategy_router")
    router = router if isinstance(router, dict) else {}
    plan = context.get("trade_plan")
    plan = plan if isinstance(plan, dict) else {}
    analysis_plan = analysis.get("trade_plan")
    analysis_plan = analysis_plan if isinstance(analysis_plan, dict) else {}

    for value in (
        plan.get("side"),
        analysis_plan.get("side"),
        analysis.get("side"),
        router.get("signal"),
    ):
        side = str(value or "").strip().lower()
        if side in {"buy", "sell"}:
            return side
    return None


def _configured_costs_bps(asset: str) -> float:
    from .trading_core import PROFILES

    profile = PROFILES.get(asset)
    if profile is None:
        raise OutcomeLabelError(
            "UNSUPPORTED_COST_PROFILE",
            f"No configured transaction-cost profile for asset {asset!r}",
        )
    costs = float(profile.taker_bps) + float(profile.slippage_bps)
    if not math.isfinite(costs) or costs < 0:
        raise OutcomeLabelError("INVALID_COST_PROFILE", f"Invalid configured costs for asset {asset!r}")
    return costs


def _outcome_window_sha256(
    df: pd.DataFrame, index: pd.DatetimeIndex, start_index: int, end_index: int
) -> str:
    columns = [column for column in ("open", "high", "low", "close", "volume") if column in df.columns]
    rows = []
    for position in range(start_index, end_index + 1):
        row = {"timestamp": index[position].isoformat()}
        for column in columns:
            value = float(df.iloc[position][column])
            if not math.isfinite(value):
                raise OutcomeLabelError("INVALID_MARKET_DATA", f"Non-finite {column} in outcome window")
            row[column] = format(value, ".17g")
        rows.append(row)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_decision_outcome_label(
    decision_record: dict,
    df: pd.DataFrame,
    *,
    horizons_bars: tuple[int, ...] | list[int] = DEFAULT_HORIZONS_BARS,
) -> dict:
    """Label forward close-to-close outcomes using only fully closed observed candles.

    The original decision timestamp is a candle-open timestamp. The baseline is that
    exact candle's close, available one timeframe later. Horizons count subsequent
    observed completed candles, so gaps are measured and recorded rather than silently
    converted into assumed elapsed-time bars.
    """
    if not isinstance(decision_record, dict):
        raise OutcomeLabelError("INVALID_DECISION_RECORD", "Decision record must be an object")

    decision_id = str(decision_record.get("decision_id") or "").strip()
    if not decision_id:
        raise OutcomeLabelError("MISSING_DECISION_ID", "Decision record has no decision_id")
    if decision_record.get("market_timestamp_source") != "analysis_timestamp":
        raise OutcomeLabelError("MISSING_MARKET_TIMESTAMP", "Decision has no verified market timestamp")
    market_timestamp_value = decision_record.get("market_timestamp")
    if not market_timestamp_value:
        raise OutcomeLabelError("MISSING_MARKET_TIMESTAMP", "Decision has no market timestamp")
    snapshot_sha = str(decision_record.get("snapshot_sha256") or "")
    if len(snapshot_sha) != 64 or any(ch not in "0123456789abcdef" for ch in snapshot_sha.lower()):
        raise OutcomeLabelError("MISSING_SNAPSHOT_HASH", "Decision snapshot SHA-256 is missing or invalid")

    asset = str(decision_record.get("asset") or "").strip().lower()
    symbol = str(decision_record.get("symbol") or "").strip()
    exchange = str(decision_record.get("exchange") or "").strip().lower()
    timeframe = str(decision_record.get("timeframe") or "").strip()
    if not asset or not symbol or not timeframe:
        raise OutcomeLabelError("MISSING_MARKET_METADATA", "Recorded asset, symbol and timeframe are required")
    if asset not in {"crypto", "forex"}:
        raise OutcomeLabelError("UNSUPPORTED_ASSET", f"No verified label data adapter for asset {asset!r}")
    if asset == "crypto" and not exchange:
        raise OutcomeLabelError("MISSING_EXCHANGE", "Crypto labels require the recorded exchange")
    if asset == "forex" and exchange not in {"oanda", "yfinance"}:
        raise OutcomeLabelError(
            "UNSUPPORTED_EXCHANGE",
            "Forex labels require an explicit oanda or yfinance source; no source is inferred",
        )
    if asset == "forex":
        if exchange == "yfinance" and not re.fullmatch(r"[A-Z]{6}(?:=X)?", symbol.upper()):
            raise OutcomeLabelError(
                "UNSUPPORTED_SYMBOL",
                "Yfinance FX labels require an explicit six-letter currency pair, optionally suffixed by =X",
            )
        supported = _SUPPORTED_OANDA_TIMEFRAMES if exchange == "oanda" else _SUPPORTED_YFINANCE_TIMEFRAMES
        if timeframe not in supported:
            raise OutcomeLabelError(
                "UNSUPPORTED_TIMEFRAME",
                f"Timeframe {timeframe!r} is not supported by the recorded forex source {exchange!r}",
            )

    provenance = df.attrs.get("data_provenance")
    if not isinstance(provenance, dict):
        raise OutcomeLabelError("MISSING_DATA_PROVENANCE", "Market data has no verified provenance metadata")
    required_provenance = ("source", "symbol", "timeframe", "fetched_at_utc", "sha256")
    if any(not provenance.get(key) for key in required_provenance):
        raise OutcomeLabelError("MISSING_DATA_PROVENANCE", "Market data provenance is incomplete")
    if str(provenance.get("symbol")) != symbol or str(provenance.get("timeframe")) != timeframe:
        raise OutcomeLabelError("DATA_PROVENANCE_MISMATCH", "Fetched data symbol/timeframe does not match the decision")
    expected_source = (
        "ccxt_ohlcv" if asset == "crypto"
        else ("oanda_v20_mid" if exchange == "oanda" else "yfinance")
    )
    if str(provenance.get("source")) != expected_source:
        raise OutcomeLabelError("DATA_PROVENANCE_MISMATCH", "Fetched data source does not match the explicit market source")
    canonical_source = df.reset_index().to_csv(
        index=False, float_format="%.17g", lineterminator="\n"
    )
    actual_source_sha = hashlib.sha256(canonical_source.encode("utf-8")).hexdigest()
    if actual_source_sha.lower() != str(provenance.get("sha256") or "").lower():
        raise OutcomeLabelError(
            "DATA_PROVENANCE_MISMATCH",
            "Fetched candle bytes do not match the recorded source dataset SHA-256",
        )
    if asset == "crypto" and str(provenance.get("exchange") or "").lower() != exchange:
        raise OutcomeLabelError("DATA_PROVENANCE_MISMATCH", "Fetched crypto exchange does not match the decision")
    if asset == "forex" and exchange == "oanda" and str(provenance.get("exchange") or "").lower() != "oanda":
        raise OutcomeLabelError("DATA_PROVENANCE_MISMATCH", "Fetched OANDA data is not identified as OANDA")
    if asset == "forex" and exchange == "yfinance" and str(provenance.get("exchange") or "").lower() != "yfinance":
        raise OutcomeLabelError("DATA_PROVENANCE_MISMATCH", "Fetched FX data is not identified as yfinance")

    if df.empty or "close" not in df.columns:
        raise OutcomeLabelError("EMPTY_MARKET_DATA", "Market data is empty or lacks close prices")
    index = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True))
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise OutcomeLabelError("INVALID_MARKET_INDEX", "Market candle index must be strictly increasing and unique")

    delta = _timeframe_delta(timeframe)
    fetched_at = _utc_timestamp(provenance.get("fetched_at_utc"))
    closed_mask = (index + delta) <= fetched_at
    decision_timestamp = _utc_timestamp(market_timestamp_value)
    baseline_positions = [i for i, timestamp in enumerate(index) if timestamp == decision_timestamp]
    if len(baseline_positions) != 1:
        raise OutcomeLabelError(
            "DECISION_CANDLE_NOT_FOUND",
            "The exact decision candle is absent from the fetched dataset; nearest-candle substitution is prohibited",
        )
    baseline_index = baseline_positions[0]
    if not bool(closed_mask[baseline_index]):
        raise OutcomeLabelError("DECISION_CANDLE_NOT_CLOSED", "The decision candle was not complete in the fetched dataset")

    horizons = _validate_horizons(horizons_bars)
    last_index = baseline_index + max(horizons)
    if last_index >= len(df):
        raise OutcomeLabelError("INSUFFICIENT_FUTURE_DATA", "Not enough subsequent candles for all requested horizons")
    if not bool(closed_mask[last_index]):
        raise OutcomeLabelError("INSUFFICIENT_FUTURE_DATA", "The longest outcome horizon is not fully closed yet")
    # The index is monotonic and all horizons are <= max(horizons); checking the final
    # candle also ensures each requested horizon endpoint is closed.
    baseline_close = float(df.iloc[baseline_index]["close"])
    if not math.isfinite(baseline_close) or baseline_close <= 0:
        raise OutcomeLabelError("INVALID_MARKET_DATA", "Decision-candle close must be positive and finite")
    costs_bps = _configured_costs_bps(asset)
    from .trading_core import PROFILES
    carry_bps_per_bar = float(PROFILES[asset].carry_bps_per_bar)
    if not math.isfinite(carry_bps_per_bar) or carry_bps_per_bar < 0:
        raise OutcomeLabelError("INVALID_COST_PROFILE", f"Invalid configured carry for asset {asset!r}")
    side = _candidate_side(decision_record)

    horizon_results = {}
    for horizon in horizons:
        outcome_index = baseline_index + horizon
        outcome_close = float(df.iloc[outcome_index]["close"])
        if not math.isfinite(outcome_close) or outcome_close <= 0:
            raise OutcomeLabelError("INVALID_MARKET_DATA", f"Invalid close at the {horizon}-bar horizon")
        forward_bps = (outcome_close / baseline_close - 1.0) * 10_000.0
        short_bps = (baseline_close / outcome_close - 1.0) * 10_000.0
        elapsed_seconds = float((index[outcome_index] - index[baseline_index]).total_seconds())
        gaps = [
            float((index[position] - index[position - 1]).total_seconds())
            for position in range(baseline_index + 1, outcome_index + 1)
        ]
        candidate_net = None
        carry_cost_bps = horizon * carry_bps_per_bar
        if side == "buy":
            candidate_net = forward_bps - 2.0 * costs_bps - carry_cost_bps
        elif side == "sell":
            candidate_net = short_bps - 2.0 * costs_bps - carry_cost_bps
        horizon_results[str(horizon)] = {
            "status": "VALID",
            "horizon_bars": horizon,
            "horizon_unit": "subsequent_observed_completed_candles",
            "baseline_bar_open": index[baseline_index].isoformat(),
            "outcome_bar_open": index[outcome_index].isoformat(),
            "baseline_close": baseline_close,
            "outcome_close": outcome_close,
            "forward_return_bps": float(forward_bps),
            "long_gross_return_bps": float(forward_bps),
            "short_gross_return_bps": float(short_bps),
            "long_net_return_bps": float(forward_bps - 2.0 * costs_bps - carry_cost_bps),
            "short_net_return_bps": float(short_bps - 2.0 * costs_bps - carry_cost_bps),
            "carry_bps_per_bar": float(carry_bps_per_bar),
            "carry_cost_bps": float(carry_cost_bps),
            "candidate_side": side,
            "candidate_side_net_return_bps": float(candidate_net) if candidate_net is not None else None,
            "cost_bps_per_side": float(costs_bps),
            "elapsed_seconds": elapsed_seconds,
            "max_interbar_gap_seconds": max(gaps, default=0.0),
        }

    outcome_window_sha = _outcome_window_sha256(df, index, baseline_index, last_index)
    label_identity = {
        "decision_id": decision_id,
        "label_version": LABEL_VERSION,
        "snapshot_sha256": snapshot_sha,
        "outcome_window_sha256": outcome_window_sha,
        "horizons_bars": list(horizons),
    }
    label_id = hashlib.sha256(
        json.dumps(label_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "record_type": "adaptive_decision_outcome_label",
        "label_version": LABEL_VERSION,
        "label_id": label_id,
        "status": "VALID",
        "research_only": True,
        "not_for_training": True,
        "automatic_promotion": False,
        "decision_id": decision_id,
        "source_decision_sha256": snapshot_sha,
        "original_decision": str(decision_record.get("decision") or "UNKNOWN").upper(),
        "decision_stage": str(decision_record.get("stage") or "unknown")[:100],
        "asset": asset,
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "market_timestamp": decision_timestamp.isoformat(),
        "decision_bar_close_at": (index[baseline_index] + delta).isoformat(),
        "candidate_side": side,
        "horizons_bars": list(horizons),
        "horizon_unit": "subsequent_observed_completed_candles",
        "cost_source": "configured_asset_profile",
        "cost_bps_per_side": float(costs_bps),
        "carry_bps_per_bar": float(carry_bps_per_bar),
        "outcomes": horizon_results,
        "outcome_window_sha256": outcome_window_sha,
        "data_provenance": {
            "source": str(provenance["source"]),
            "symbol": str(provenance["symbol"]),
            "exchange": str(provenance.get("exchange") or ""),
            "timeframe": str(provenance["timeframe"]),
            "fetched_at_utc": str(provenance["fetched_at_utc"]),
            "row_count": int(provenance.get("row_count") or len(df)),
            "source_dataset_sha256": str(provenance["sha256"]),
        },
        "labelled_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_decision_data(record: dict, *, days: int = 365) -> pd.DataFrame:
    from .data import fetch_crypto, fetch_forex, fetch_forex_oanda

    asset = str(record.get("asset") or "").strip().lower()
    exchange = str(record.get("exchange") or "").strip().lower()
    symbol = str(record.get("symbol") or "").strip()
    timeframe = str(record.get("timeframe") or "").strip()
    if asset == "crypto":
        if not exchange or not symbol or not timeframe:
            raise OutcomeLabelError("MISSING_MARKET_METADATA", "Crypto source requires recorded exchange, symbol and timeframe")
        return fetch_crypto(symbol=symbol, exchange=exchange, timeframe=timeframe, days=days, closed_only=True)
    if asset == "forex":
        if not symbol or not timeframe:
            raise OutcomeLabelError("MISSING_MARKET_METADATA", "Forex source requires recorded symbol and timeframe")
        if exchange == "oanda":
            if timeframe not in _SUPPORTED_OANDA_TIMEFRAMES:
                raise OutcomeLabelError("UNSUPPORTED_TIMEFRAME", f"Unsupported OANDA timeframe {timeframe!r}")
            return fetch_forex_oanda(symbol=symbol, timeframe=timeframe, days=days)
        if exchange == "yfinance":
            if not re.fullmatch(r"[A-Z]{6}(?:=X)?", symbol.upper()):
                raise OutcomeLabelError(
                    "UNSUPPORTED_SYMBOL",
                    "Yfinance FX labels require an explicit six-letter currency pair, optionally suffixed by =X",
                )
            if timeframe not in _SUPPORTED_YFINANCE_TIMEFRAMES:
                raise OutcomeLabelError("UNSUPPORTED_TIMEFRAME", f"Unsupported yfinance timeframe {timeframe!r}")
            return fetch_forex(symbol=symbol, timeframe=timeframe, days=days)
        raise OutcomeLabelError("UNSUPPORTED_EXCHANGE", "Forex source must explicitly be oanda or yfinance")
    raise OutcomeLabelError("UNSUPPORTED_ASSET", f"No verified outcome data adapter for asset {asset!r}")


def _decode_audit_detail(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def process_decision_outcome_labels(
    *,
    limit: int = 100,
    horizons_bars: tuple[int, ...] | list[int] = DEFAULT_HORIZONS_BARS,
    days: int = 365,
    persist: bool = False,
) -> dict:
    """Evaluate recorded decisions; persist labels only when persist=True.

    Existing valid labels at this version are skipped. Insufficient future data is
    retried on a later run. The default is dry-run and cannot write audit records.
    """
    from .audit_chain import append_audit
    from .db import AuditLog, SessionLocal

    limit = max(1, min(10_000, int(limit)))
    horizons = _validate_horizons(horizons_bars)
    async with SessionLocal() as db:
        decisions = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.event == "ADAPTIVE_DECISION_MEMORY")
                .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            )
        ).scalars().all()
        existing_rows = (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.event == "ADAPTIVE_DECISION_OUTCOME_LABEL")
                .order_by(AuditLog.id.asc())
            )
        ).scalars().all()
        decision_records = [_decode_audit_detail(row.detail) for row in decisions]
        existing_records = [_decode_audit_detail(row.detail) for row in existing_rows]

    already_labelled = {
        (str(record.get("decision_id")), str(record.get("label_version")))
        for record in existing_records
        if record and record.get("status") == "VALID"
    }
    data_cache: dict[tuple[str, str, str, str], Any] = {}
    created = skipped = failed = selected = 0
    reasons: dict[str, int] = {}
    preview: list[dict] = []

    for record in decision_records:
        if not record:
            skipped += 1
            reasons["invalid_audit_detail"] = reasons.get("invalid_audit_detail", 0) + 1
            continue
        decision_id = str(record.get("decision_id") or "")
        if not decision_id:
            skipped += 1
            reasons["missing_decision_id"] = reasons.get("missing_decision_id", 0) + 1
            continue
        if (decision_id, LABEL_VERSION) in already_labelled:
            continue
        if created >= limit:
            break
        selected += 1
        key = (
            str(record.get("asset") or "").strip().lower(),
            str(record.get("exchange") or "").strip().lower(),
            str(record.get("symbol") or "").strip(),
            str(record.get("timeframe") or "").strip(),
        )
        if key not in data_cache:
            try:
                data_cache[key] = await asyncio.to_thread(_fetch_decision_data, record, days=days)
            except OutcomeLabelError as exc:
                data_cache[key] = exc
            except Exception as exc:
                data_cache[key] = exc
        fetched = data_cache[key]
        if isinstance(fetched, Exception):
            skipped += 1
            code = fetched.code if isinstance(fetched, OutcomeLabelError) else "market_data_fetch_error"
            reasons[code] = reasons.get(code, 0) + 1
            if not isinstance(fetched, OutcomeLabelError):
                failed += 1
            continue
        try:
            label = build_decision_outcome_label(record, fetched, horizons_bars=horizons)
        except OutcomeLabelError as exc:
            skipped += 1
            reasons[exc.code] = reasons.get(exc.code, 0) + 1
            continue
        if persist:
            async with SessionLocal() as db:
                await append_audit(
                    db,
                    event="ADAPTIVE_DECISION_OUTCOME_LABEL",
                    detail=label,
                    actor_id="research-outcome-labeler",
                )
        elif len(preview) < 10:
            preview.append(label)
        created += 1
        already_labelled.add((decision_id, LABEL_VERSION))

    return {
        "label_version": LABEL_VERSION,
        "research_only": True,
        "persisted": bool(persist),
        "selected": selected,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "skip_reasons": reasons,
        "preview": preview,
    }
