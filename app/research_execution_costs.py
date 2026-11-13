"""Evidence-bound per-bar execution costs for quantitative research."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from .execution_costs import (
    EXECUTION_COST_MODEL_VERSION,
    deterministic_execution_draws,
    execution_cost_components,
    latency_from_quantiles,
)
from .paper_execution_model import calibrated_model_available
from .research_evidence import canonical_sha256, validate_execution_cost_calibration

PER_BAR_CONTEXT_SCHEMA = "atlas-per-bar-execution-context-v1"
_VOLUME_WINDOW_BARS = 20
_ALLOWED_USD_QUOTE_ASSETS = {"USD", "USDT", "USDC", "BUSD", "FDUSD", "DAI"}
_PARAMETER_NAMES = (
    "base_slippage_bps", "max_participation_rate", "impact_coefficient",
    "rejection_probability", "latency_min_ms", "latency_median_ms",
    "latency_p95_ms", "latency_max_ms", "latency_impact_coefficient",
)


class ResearchExecutionCostError(ValueError):
    """A candidate return stream cannot be priced from verified execution context."""


def _setting(settings: Any, name: str, default: Any = None) -> Any:
    return getattr(settings, name, default)


def _number(value: Any, *, minimum: float = 0.0, positive: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or parsed < minimum or (positive and parsed <= 0.0):
        return None
    return parsed


def _execution_parameters(settings: Any, *, taker_bps: float, base_slippage_bps: float) -> dict[str, Any]:
    return {
        "base_slippage_bps": base_slippage_bps,
        "max_participation_rate": _setting(settings, "paper_impact_max_participation_rate"),
        "impact_coefficient": _setting(settings, "paper_impact_coefficient"),
        "rejection_probability": _setting(settings, "paper_execution_rejection_probability"),
        "latency_min_ms": _setting(settings, "paper_execution_latency_min_ms"),
        "latency_median_ms": _setting(settings, "paper_execution_latency_median_ms"),
        "latency_p95_ms": _setting(settings, "paper_execution_latency_p95_ms"),
        "latency_max_ms": _setting(settings, "paper_execution_latency_max_ms"),
        "latency_impact_coefficient": _setting(settings, "paper_latency_impact_coefficient"),
        "taker_bps": taker_bps,
        "research_order_notional_usd": _setting(settings, "research_order_notional_usd"),
        "research_fee_source": str(_setting(settings, "research_fee_source", "") or "").strip(),
        "research_fee_evidence_id": str(_setting(settings, "research_fee_evidence_id", "") or "").strip(),
        "paper_execution_evidence_source": str(_setting(settings, "paper_execution_evidence_source", "") or "").strip(),
        "paper_execution_evidence_id": str(_setting(settings, "paper_execution_evidence_id", "") or "").strip(),
    }


def research_execution_model_fingerprint(
    settings: Any, *, taker_bps: float, base_slippage_bps: float,
    cost_stress_multiplier: float,
) -> str:
    """Fingerprint every parameter and input algorithm affecting research execution."""
    return canonical_sha256({
        "model": "atlas-research-per-bar-execution-v1",
        "shared_execution_cost_model_version": EXECUTION_COST_MODEL_VERSION,
        "spread_source": "SIGNED_PER_BAR_ORDER_BOOK_OBSERVATIONS",
        "volume_source": "PRIOR_20_COMPLETED_CCXT_SPOT_BASE_VOLUME_MEDIAN",
        "volatility_source": "PRIOR_20_COMPLETED_CLOSE_RETURN_STD_DDOF1_BPS",
        "bar_duration_source": "EXACT_UTC_INDEX_CADENCE_MILLISECONDS",
        "latency_draw_method": "SHARED_SHA256_ORDER_KEY_QUANTILE_INTERPOLATION_V1",
        "fill_policy": "FAIL_CLOSED_ABOVE_CONFIGURED_MAX_PARTICIPATION",
        "rejection_policy": "ZERO_REJECTION_REQUIRED_UNTIL_RESEARCH_STATE_MODELLED",
        "cost_stress_multiplier": float(cost_stress_multiplier),
        "parameters": _execution_parameters(
            settings, taker_bps=taker_bps, base_slippage_bps=base_slippage_bps
        ),
        "context_schema": PER_BAR_CONTEXT_SCHEMA,
    })


def _valid_model_parameters(parameters: Mapping[str, Any]) -> tuple[dict[str, float] | None, str | None]:
    parsed: dict[str, float] = {}
    for name in _PARAMETER_NAMES:
        value = _number(parameters.get(name))
        if value is None:
            return None, f"EXECUTION_PARAMETER_MISSING_OR_INVALID:{name}"
        parsed[name] = value
    if not 0.0 < parsed["max_participation_rate"] <= 1.0:
        return None, "EXECUTION_MAX_PARTICIPATION_RATE_INVALID"
    if parsed["rejection_probability"] != 0.0:
        return None, "REJECTION_MODEL_NOT_INTEGRATED_IN_RESEARCH_POSITION_STATE"
    if not (parsed["latency_min_ms"] <= parsed["latency_median_ms"] <= parsed["latency_p95_ms"] <= parsed["latency_max_ms"]):
        return None, "EXECUTION_LATENCY_QUANTILES_INVALID"
    return parsed, None


def build_research_execution_cost_context(
    df: pd.DataFrame, *, settings: Any, taker_bps: float,
    base_slippage_bps: float, cost_stress_multiplier: float, asset: str = "crypto",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Verify signed per-bar spreads and build causal volume/volatility inputs."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None, {"status": "INCOMPLETE", "reason": "RESEARCH_DATAFRAME_MISSING",
                      "execution_cost_model_complete": False}
    provenance = df.attrs.get("data_provenance", {})
    provenance = provenance if isinstance(provenance, Mapping) else {}
    venue = str(provenance.get("exchange") or "").strip().lower()
    market_type = str(provenance.get("market_type") or "").strip().lower()
    symbol = str(provenance.get("symbol") or "").strip()
    timeframe = str(provenance.get("timeframe") or "").strip()
    dataset_sha256 = str(provenance.get("sha256") or "").strip().lower()
    document = df.attrs.get("execution_cost_calibration_evidence")
    evidence_id = str(document.get("evidence_id") or "").strip() if isinstance(document, Mapping) else ""
    try:
        fingerprint = research_execution_model_fingerprint(
            settings, taker_bps=taker_bps, base_slippage_bps=base_slippage_bps,
            cost_stress_multiplier=cost_stress_multiplier,
        )
    except (TypeError, ValueError, OverflowError):
        fingerprint = None

    def blocked(reason: str, calibration: Mapping[str, Any] | None = None):
        return None, {
            "status": "INCOMPLETE", "reason": reason, "execution_cost_model_complete": False,
            "execution_cost_calibration_status": (
                "VERIFIED" if calibration and calibration.get("calibration_verified") is True
                else str((calibration or {}).get("reason") or "MISSING_VENUE_CALIBRATION_EVIDENCE")
            ),
            "execution_cost_calibration_evidence_id": evidence_id or None,
            "execution_cost_model_fingerprint": fingerprint,
            "venue": venue or None, "market_type": market_type or None,
            "symbol": symbol or None, "timeframe": timeframe or None,
            "dataset_sha256": dataset_sha256 or None,
        }

    if (
        str(asset or "").strip().lower() != "crypto"
        or str(provenance.get("source") or "") != "ccxt_ohlcv"
        or market_type != "spot"
        or str(provenance.get("asset_class") or "").lower() != "crypto"
        or str(provenance.get("volume_unit") or "").lower() != "base_asset"
        or provenance.get("volume_unit_verified") is not True
    ):
        return blocked("VERIFIED_CRYPTO_SPOT_BASE_VOLUME_REQUIRED")
    if not venue or not symbol or not timeframe or len(dataset_sha256) != 64:
        return blocked("MARKET_DATA_PROVENANCE_INCOMPLETE")
    if any(char not in "0123456789abcdef" for char in dataset_sha256):
        return blocked("MARKET_DATA_DATASET_HASH_INVALID")
    symbol_parts = symbol.split("/")
    quote_asset = symbol_parts[1].split(":")[0].upper() if len(symbol_parts) == 2 else ""
    if quote_asset not in _ALLOWED_USD_QUOTE_ASSETS:
        return blocked("USD_OR_STABLE_QUOTE_MARKET_REQUIRED")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None:
        return blocked("TIMEZONE_AWARE_BAR_INDEX_REQUIRED")
    if not df.index.is_unique or not df.index.is_monotonic_increasing or len(df) < _VOLUME_WINDOW_BARS + 2:
        return blocked("UNIQUE_MONOTONIC_BAR_HISTORY_REQUIRED")
    if not {"open", "close", "volume"}.issubset(set(df.columns)):
        return blocked("OHLCV_COLUMNS_REQUIRED")

    index = pd.DatetimeIndex(df.index)
    utc_index = [pd.Timestamp(value).tz_convert("UTC").isoformat() for value in index]
    deltas_ms = index.to_series().diff().dropna().dt.total_seconds().to_numpy(dtype=float) * 1000.0
    if not len(deltas_ms) or not np.isfinite(deltas_ms).all() or (deltas_ms <= 0.0).any():
        return blocked("BAR_CADENCE_INVALID")
    bar_duration_ms = float(np.median(deltas_ms))
    if not np.allclose(deltas_ms, bar_duration_ms, rtol=0.0, atol=1.0):
        return blocked("IRREGULAR_BAR_CADENCE_NOT_CALIBRATABLE")

    calibration = validate_execution_cost_calibration(
        document, _setting(settings, "research_evidence_public_keys_json", ""),
        venue=venue, market_type=market_type, symbol=symbol,
        model_fingerprint=str(fingerprint or ""),
        max_spread_error_bps=_setting(settings, "research_cost_max_spread_mae_bps"),
        max_slippage_error_bps=_setting(settings, "research_cost_max_slippage_mae_bps"),
    )
    if calibration.get("calibration_verified") is not True:
        return blocked(str(calibration.get("reason") or "VENUE_COST_CALIBRATION_UNVERIFIED"), calibration)

    per_bar = document.get("per_bar_execution_context") if isinstance(document, Mapping) else None
    if not isinstance(per_bar, Mapping) or per_bar.get("schema") != PER_BAR_CONTEXT_SCHEMA:
        return blocked("SIGNED_PER_BAR_EXECUTION_CONTEXT_MISSING", calibration)
    if str(per_bar.get("dataset_sha256") or "").lower() != dataset_sha256:
        return blocked("PER_BAR_DATASET_HASH_MISMATCH", calibration)
    if per_bar.get("bar_index_sha256") != canonical_sha256(utc_index):
        return blocked("PER_BAR_INDEX_FINGERPRINT_MISMATCH", calibration)
    fee_source = str(_setting(settings, "research_fee_source", "") or "").strip()
    fee_evidence_id = str(_setting(settings, "research_fee_evidence_id", "") or "").strip()
    if not fee_source or not fee_evidence_id:
        return blocked("RESEARCH_TAKER_FEE_EVIDENCE_NOT_CONFIGURED", calibration)
    reported_fee = _number(document.get("taker_fee_bps"))
    if (
        reported_fee is None
        or not math.isclose(reported_fee, taker_bps, rel_tol=0.0, abs_tol=1e-9)
        or str(document.get("fee_source") or "").strip() != fee_source
        or str(document.get("fee_evidence_id") or "").strip() != fee_evidence_id
    ):
        return blocked("SIGNED_TAKER_FEE_DOES_NOT_MATCH_RESEARCH_FEE_POLICY", calibration)

    sources = document.get("sources")
    fee_schedule_sources = [
        row for row in sources or []
        if isinstance(row, Mapping) and row.get("source_type") == "fee_schedule"
    ]
    if len(fee_schedule_sources) != 1:
        return blocked("EXACTLY_ONE_FEE_SCHEDULE_SOURCE_REQUIRED", calibration)
    fee_schedule_sha256 = str(fee_schedule_sources[0].get("sha256") or "").lower()
    if str(document.get("fee_schedule_source_sha256") or "").lower() != fee_schedule_sha256:
        return blocked("TAKER_FEE_SCHEDULE_SOURCE_HASH_MISMATCH", calibration)

    order_book_sources = [
        row for row in sources or []
        if isinstance(row, Mapping) and row.get("source_type") == "order_book"
    ]
    if len(order_book_sources) != 1:
        return blocked("EXACTLY_ONE_ORDER_BOOK_SOURCE_REQUIRED", calibration)
    order_book_sha256 = str(order_book_sources[0].get("sha256") or "").lower()
    if str(per_bar.get("spread_source_sha256") or "").lower() != order_book_sha256:
        return blocked("PER_BAR_SPREAD_SOURCE_HASH_MISMATCH", calibration)
    raw_spreads = per_bar.get("spread_bps")
    if not isinstance(raw_spreads, list) or len(raw_spreads) != len(df):
        return blocked("PER_BAR_SPREAD_SERIES_LENGTH_MISMATCH", calibration)
    if per_bar.get("spread_observation_count") != len(raw_spreads):
        return blocked("PER_BAR_SPREAD_OBSERVATION_COUNT_MISMATCH", calibration)
    try:
        spreads = np.asarray(raw_spreads, dtype=float)
    except (TypeError, ValueError, OverflowError):
        return blocked("PER_BAR_SPREAD_SERIES_INVALID", calibration)
    if not np.isfinite(spreads).all() or (spreads < 0.0).any():
        return blocked("PER_BAR_SPREAD_SERIES_INVALID", calibration)
    if per_bar.get("spread_values_sha256") != canonical_sha256(raw_spreads):
        return blocked("PER_BAR_SPREAD_SERIES_FINGERPRINT_MISMATCH", calibration)

    volume = pd.to_numeric(df["volume"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    open_price = pd.to_numeric(df["open"], errors="coerce")
    for series in (volume, close, open_price):
        if not np.isfinite(series.to_numpy(dtype=float)).all():
            return blocked("OHLCV_CONTAINS_NONFINITE_VALUES", calibration)
    if (volume < 0.0).any() or (close <= 0.0).any() or (open_price <= 0.0).any():
        return blocked("OHLCV_PRICE_OR_VOLUME_RANGE_INVALID", calibration)

    prior_volume = volume.shift(1).rolling(_VOLUME_WINDOW_BARS, min_periods=_VOLUME_WINDOW_BARS).median().to_numpy(dtype=float)
    prior_returns = close.pct_change().shift(1)
    prior_volatility = (
        prior_returns.rolling(_VOLUME_WINDOW_BARS, min_periods=_VOLUME_WINDOW_BARS)
        .std(ddof=1).mul(10_000.0).to_numpy(dtype=float)
    )
    parameters = _execution_parameters(
        settings, taker_bps=taker_bps, base_slippage_bps=base_slippage_bps
    )
    parsed_parameters, parameter_error = _valid_model_parameters(parameters)
    if parameter_error:
        return blocked(parameter_error, calibration)
    order_notional = _number(parameters.get("research_order_notional_usd"), positive=True)
    if order_notional is None:
        return blocked("RESEARCH_ORDER_NOTIONAL_USD_NOT_CONFIGURED", calibration)
    taker = _number(taker_bps)
    if taker is None:
        return blocked("RESEARCH_TAKER_FEE_INVALID", calibration)
    candidates = np.flatnonzero(
        np.isfinite(prior_volume) & (prior_volume > 0.0)
        & np.isfinite(prior_volatility) & (prior_volatility >= 0.0)
    )
    if not len(candidates):
        return blocked("PREVIOUS_COMPLETED_VOLUME_OR_VOLATILITY_UNAVAILABLE", calibration)
    sample_i = int(candidates[0])
    sample_context = {
        "tradable_volume_units": float(prior_volume[sample_i]),
        "spread_bps": float(spreads[sample_i]),
        "recent_volatility_bps": float(prior_volatility[sample_i]),
        "bar_duration_ms": bar_duration_ms, "exchange": venue,
        "symbol": symbol, "market_type": market_type,
    }
    paper_artifact_ok = calibrated_model_available(
        sample_context, **parsed_parameters,
        evidence_source=str(_setting(settings, "paper_execution_evidence_source", "") or ""),
        evidence_path=str(_setting(settings, "paper_execution_evidence_path", "") or ""),
        evidence_id=str(_setting(settings, "paper_execution_evidence_id", "") or ""),
        allow_test_fixture=False,
    )
    if not paper_artifact_ok:
        return blocked("PAPER_EXECUTION_CALIBRATION_ARTIFACT_UNVERIFIED", calibration)

    context_sha256 = canonical_sha256(per_bar)
    context = {
        "index": index, "spread_bps": spreads, "volume_units": prior_volume,
        "volatility_bps": prior_volatility, "bar_duration_ms": bar_duration_ms,
        "taker_bps": taker, **parsed_parameters,
        "order_notional_usd": order_notional, "dataset_sha256": dataset_sha256,
        "context_sha256": context_sha256, "model_fingerprint": fingerprint,
        "evidence_id": evidence_id, "venue": venue, "market_type": market_type,
        "symbol": symbol, "timeframe": timeframe,
    }
    return context, {
        "status": "READY", "reason": "SIGNED_PER_BAR_CONTEXT_AND_PAPER_MODEL_VERIFIED",
        "execution_cost_model_complete": True, "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_evidence_id": evidence_id or None,
        "execution_cost_model_fingerprint": fingerprint,
        "per_bar_execution_context_sha256": context_sha256, "dataset_sha256": dataset_sha256,
        "venue": venue, "market_type": market_type, "symbol": symbol, "timeframe": timeframe,
    }


def slice_research_execution_context(context: Mapping[str, Any] | None, index: pd.Index) -> dict[str, Any] | None:
    """Return an exact index-aligned view of a previously verified full context."""
    if not isinstance(context, Mapping):
        return None
    source_index = context.get("index")
    if not isinstance(source_index, pd.Index):
        return None
    positions = source_index.get_indexer(index)
    if len(positions) != len(index) or (positions < 0).any():
        return None
    sliced = dict(context)
    sliced["index"] = pd.Index(index)
    for name in ("spread_bps", "volume_units", "volatility_bps"):
        values = np.asarray(context.get(name), dtype=float)
        if len(values) != len(source_index):
            return None
        sliced[name] = values[positions]
    return sliced


def execution_cost_for_event(
    context: Mapping[str, Any] | None, *, bar_position: int, turnover: float,
    reference_price: float, side: str, namespace: str,
) -> tuple[float | None, str | None]:
    """Calculate one-side taker fee plus calibrated spread, impact and latency."""
    if not isinstance(context, Mapping):
        return None, "EXECUTION_CONTEXT_UNAVAILABLE"
    try:
        position = int(bar_position)
        amount = float(turnover)
        price = float(reference_price)
    except (TypeError, ValueError, OverflowError):
        return None, "EXECUTION_EVENT_NUMERIC_INPUT_INVALID"
    if position < 0 or position >= len(context.get("index", [])):
        return None, "EXECUTION_EVENT_BAR_OUT_OF_RANGE"
    if not math.isfinite(amount) or amount < 0.0 or not math.isfinite(price) or price <= 0.0:
        return None, "EXECUTION_EVENT_NUMERIC_INPUT_INVALID"
    if amount == 0.0:
        return 0.0, None
    try:
        volume = float(context["volume_units"][position])
        spread = float(context["spread_bps"][position])
        volatility = float(context["volatility_bps"][position])
        duration = float(context["bar_duration_ms"])
        notional = float(context["order_notional_usd"])
        quantity = amount * notional / price
        participation = quantity / volume
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError, OverflowError):
        return None, "EXECUTION_EVENT_MARKET_CONTEXT_INVALID"
    if (
        not all(math.isfinite(value) for value in (volume, spread, volatility, duration, notional, quantity, participation))
        or volume <= 0.0 or spread < 0.0 or volatility < 0.0
        or duration <= 0.0 or notional <= 0.0 or quantity <= 0.0
    ):
        return None, "EXECUTION_EVENT_MARKET_CONTEXT_INVALID"
    if participation > float(context["max_participation_rate"]):
        return None, "ORDER_PARTICIPATION_LIMIT_EXCEEDED"
    try:
        timestamp = pd.Timestamp(context["index"][position]).tz_convert("UTC").isoformat()
        normalized_side = str(side).strip().lower()
        if normalized_side not in {"buy", "sell"}:
            return None, "EXECUTION_EVENT_SIDE_INVALID"
        order_key = canonical_sha256({
            "dataset_sha256": context["dataset_sha256"],
            "per_bar_context_sha256": context["context_sha256"],
            "namespace": str(namespace), "bar_timestamp_utc": timestamp,
            "side": normalized_side, "turnover": amount,
            "quantity_units": quantity, "reference_price": price,
            "execution_cost_model_fingerprint": context["model_fingerprint"],
        })
        _, latency_draw = deterministic_execution_draws(order_key)
        latency_ms = latency_from_quantiles(
            latency_draw, minimum_ms=float(context["latency_min_ms"]),
            median_ms=float(context["latency_median_ms"]),
            p95_ms=float(context["latency_p95_ms"]),
            maximum_ms=float(context["latency_max_ms"]),
        )
        components = execution_cost_components(
            base_slippage_bps=float(context["base_slippage_bps"]),
            spread_bps=spread, volatility_bps=volatility,
            participation_rate=participation,
            impact_coefficient=float(context["impact_coefficient"]),
            latency_ms=latency_ms, bar_duration_ms=duration,
            latency_impact_coefficient=float(context["latency_impact_coefficient"]),
        )
        total = float(context["taker_bps"]) + float(components["total_slippage_bps"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None, "EXECUTION_EVENT_COST_CALCULATION_INVALID"
    if not math.isfinite(total) or total < 0.0:
        return None, "EXECUTION_EVENT_COST_CALCULATION_INVALID"
    return total, None


def make_execution_cost_provider(
    context: Mapping[str, Any] | None, *, namespace: str,
) -> Callable[[int, float, float, str, str], float] | None:
    """Build the event callback consumed by the OHLC research simulator."""
    if not isinstance(context, Mapping):
        return None

    def provide(bar_position: int, turnover: float, reference_price: float, side: str, event_type: str) -> float:
        cost, reason = execution_cost_for_event(
            context, bar_position=bar_position, turnover=turnover,
            reference_price=reference_price, side=side,
            namespace=f"{namespace}:{event_type}",
        )
        if cost is None:
            raise ResearchExecutionCostError(reason or "EXECUTION_EVENT_UNAVAILABLE")
        return cost

    return provide


def execution_cost_series_for_positions(
    context: Mapping[str, Any] | None, position: pd.Series, reference_prices: pd.Series,
    *, namespace: str, fallback_cost_bps: float,
) -> tuple[pd.Series, dict[str, Any]]:
    """Build router costs; fallback rows are explicitly marked incomplete."""
    fallback = _number(fallback_cost_bps)
    if fallback is None:
        raise ValueError("fallback_cost_bps must be finite and non-negative")
    positions = pd.Series(position, dtype=float)
    prices = pd.Series(reference_prices, index=positions.index, dtype=float)
    if not prices.index.equals(positions.index):
        raise ValueError("execution prices must align exactly with position index")
    values = np.full(len(positions), fallback, dtype=float)
    reasons: dict[str, int] = {}
    complete = isinstance(context, Mapping)
    previous = 0.0
    for i, current in enumerate(positions.to_numpy(dtype=float)):
        if not math.isfinite(float(current)):
            complete = False
            reasons["NONFINITE_POSITION"] = reasons.get("NONFINITE_POSITION", 0) + 1
            previous = 0.0
            continue
        turnover = abs(float(current) - previous)
        if turnover > 0.0:
            side = "buy" if current - previous > 0.0 else "sell"
            cost, reason = execution_cost_for_event(
                context, bar_position=i, turnover=turnover,
                reference_price=float(prices.iloc[i]), side=side, namespace=namespace,
            )
            if cost is None:
                complete = False
                key = reason or "EXECUTION_EVENT_UNAVAILABLE"
                reasons[key] = reasons.get(key, 0) + 1
            else:
                values[i] = cost
        previous = float(current)
    return pd.Series(values, index=positions.index, dtype=float, name="execution_cost_bps"), {
        "execution_cost_model_complete": bool(complete),
        "execution_cost_model_status": (
            "VERIFIED_PER_BAR_EXECUTION_COSTS" if complete
            else "STATIC_FALLBACK_OR_INCOMPLETE_PER_BAR_CONTEXT"
        ),
        "execution_cost_model_fingerprint": (
            str(context.get("model_fingerprint") or "") if isinstance(context, Mapping) else None
        ),
        "per_bar_execution_context_sha256": (
            str(context.get("context_sha256") or "") if isinstance(context, Mapping) else None
        ),
        "execution_cost_failure_reasons": reasons,
    }
