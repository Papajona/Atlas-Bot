"""Conservative, deterministic paper fills using completed crypto OHLCV.

This is a synthetic liquidity proxy, not venue-calibrated execution evidence. Customer
paper orders remain blocked until they have a separately isolated simulation ledger.
"""
from __future__ import annotations
import hashlib
import math
from decimal import Decimal, ROUND_DOWN
from typing import Any

import numpy as np
import pandas as pd

SCHEMA = "ATLAS_PAPER_MARKET_CONTEXT_V1"
QUANTUM = Decimal("0.000001")
BAR_SECONDS = {"1m":60,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400}


class PaperExecutionError(ValueError):
    pass


def build_paper_market_context(df: pd.DataFrame, *, symbol: str, timeframe: str, asset: str = "crypto", lookback: int = 20) -> dict[str, Any]:
    """Build market context from complete candles; FX tick counts are not treated as traded units."""
    if asset != "crypto":
        raise PaperExecutionError("Paper participation limits require base-asset volume; FX/commodity tick counts are not comparable")
    if timeframe not in BAR_SECONDS or isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 5:
        raise PaperExecutionError("Unsupported timeframe or invalid lookback")
    required = {"open", "high", "low", "close", "volume"}
    if not isinstance(df, pd.DataFrame) or not required.issubset(df.columns):
        raise PaperExecutionError("Completed OHLCV context is missing required columns")
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.tz is None or not df.index.is_unique or not df.index.is_monotonic_increasing:
        raise PaperExecutionError("OHLCV context requires unique, chronological, timezone-aware candle timestamps")
    if len(df) < lookback + 1:
        raise PaperExecutionError(f"At least {lookback + 1} completed candles are required")
    w = df.iloc[-(lookback + 1):]
    close = pd.to_numeric(w.close, errors="coerce").to_numpy(dtype=float)
    volume = pd.to_numeric(w.volume, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(close).all() or (close <= 0).any() or not np.isfinite(volume).all() or (volume < 0).any():
        raise PaperExecutionError("OHLCV context contains invalid prices or volume")
    usable = volume[-lookback:][volume[-lookback:] > 0]
    if len(usable) < max(3, lookback // 2):
        raise PaperExecutionError("Insufficient positive completed-bar volume observations")
    vol_bps = float(np.std(np.diff(np.log(close))[-lookback:], ddof=1) * 10_000)
    med_volume = float(np.median(usable))
    if not math.isfinite(vol_bps) or vol_bps < 0 or not math.isfinite(med_volume) or med_volume <= 0:
        raise PaperExecutionError("Volatility or median-volume estimate is invalid")
    return {
        "schema": SCHEMA, "source": "ATLAS_CLOSED_OHLCV",
        "fidelity": "SYNTHETIC_OHLCV_PROXY_NOT_VENUE_CALIBRATED",
        "symbol": str(symbol).strip().upper(), "timeframe": timeframe, "asset": asset,
        "as_of": df.index[-1].isoformat(), "last_close": float(close[-1]),
        "median_volume_units": med_volume, "volume_unit": "BASE_ASSET_UNITS",
        "realized_vol_bps_per_bar": vol_bps, "bar_seconds": BAR_SECONDS[timeframe],
        "observations": int(len(usable)), "spread_observed": False,
    }


def _finite(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperExecutionError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise PaperExecutionError(f"{field} must be finite")
    return number


def _unit(order_id: str, salt: str) -> float:
    digest = hashlib.sha256(f"{order_id}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)


def validate_paper_context(context: Any, *, symbol: str, timeframe: str, signal_timestamp: str, price: float, max_reference_deviation_bps: float = 30.0) -> dict[str, Any]:
    if not isinstance(context, dict) or context.get("schema") != SCHEMA or context.get("fidelity") != "SYNTHETIC_OHLCV_PROXY_NOT_VENUE_CALIBRATED":
        raise PaperExecutionError("paper_market_context is missing or unsupported")
    if str(context.get("symbol", "")).upper() != str(symbol).strip().upper() or context.get("timeframe") != timeframe:
        raise PaperExecutionError("paper market context symbol/timeframe mismatch")
    if context.get("asset") != "crypto" or context.get("volume_unit") != "BASE_ASSET_UNITS":
        raise PaperExecutionError("Comparable crypto base-volume units are required")
    try:
        as_of, decision_at = pd.Timestamp(context.get("as_of")), pd.Timestamp(signal_timestamp)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperExecutionError("Context and decision timestamps must be valid ISO timestamps") from exc
    if as_of.tz is None or decision_at.tz is None or as_of > decision_at:
        raise PaperExecutionError("Context timestamp is naive or occurs after the decision")
    last_close = _finite(context.get("last_close"), "last_close")
    med_volume = _finite(context.get("median_volume_units"), "median_volume_units")
    vol_bps = _finite(context.get("realized_vol_bps_per_bar"), "realized_vol_bps_per_bar")
    bar_seconds = _finite(context.get("bar_seconds"), "bar_seconds")
    if last_close <= 0 or med_volume <= 0 or vol_bps < 0 or bar_seconds <= 0:
        raise PaperExecutionError("Invalid price, volume, volatility, or bar cadence")
    if context.get("observations", 0) < 3:
        raise PaperExecutionError("Insufficient volume observations")
    price = _finite(price, "price")
    deviation = abs(price / last_close - 1) * 10_000 if price > 0 else float("inf")
    max_deviation = _finite(max_reference_deviation_bps, "max_reference_deviation_bps")
    if max_deviation < 0 or deviation > max_deviation:
        raise PaperExecutionError("Reference price deviates too far from the latest completed close")
    return {**context, "as_of": as_of.isoformat(), "last_close": last_close, "median_volume_units": med_volume,
            "realized_vol_bps_per_bar": vol_bps, "bar_seconds": int(bar_seconds), "reference_deviation_bps": deviation}


def simulate_paper_order(*, order_id: str, side: str, quantity: float, price: float, base_slippage_bps: float,
                         taker_fee_bps: float, context: dict[str, Any], max_volume_participation: float = 0.01,
                         volatility_slippage_multiplier: float = 0.25, market_impact_bps: float = 10.0,
                         rejection_rate: float = 0.01, latency_min_ms: int = 100, latency_max_ms: int = 500) -> dict[str, Any]:
    """A deterministic synthetic IOC model: volume-capped fills, delayed result and conservative cost."""
    if side not in {"buy", "sell"} or not order_id:
        raise PaperExecutionError("Valid side and order id are required")
    q = Decimal(str(_finite(quantity, "quantity"))).quantize(QUANTUM, rounding=ROUND_DOWN)
    price = _finite(price, "price")
    base = _finite(base_slippage_bps, "base_slippage_bps")
    fee_bps = _finite(taker_fee_bps, "taker_fee_bps")
    max_part = _finite(max_volume_participation, "max_volume_participation")
    vol_mult = _finite(volatility_slippage_multiplier, "volatility_slippage_multiplier")
    impact_coeff = _finite(market_impact_bps, "market_impact_bps")
    reject_rate = _finite(rejection_rate, "rejection_rate")
    if q <= 0 or price <= 0 or min(base, fee_bps, vol_mult, impact_coeff) < 0 or not 0 < max_part <= 1 or not 0 <= reject_rate <= 1:
        raise PaperExecutionError("Invalid quantity/price or paper cost-model settings")
    if isinstance(latency_min_ms, bool) or not isinstance(latency_min_ms, int) or latency_min_ms < 0 or isinstance(latency_max_ms, bool) or not isinstance(latency_max_ms, int) or latency_max_ms < latency_min_ms:
        raise PaperExecutionError("Invalid synthetic latency range")
    ctx = validate_paper_context(context, symbol=context.get("symbol", ""), timeframe=context.get("timeframe", ""),
                                  signal_timestamp=context.get("as_of", ""), price=price, max_reference_deviation_bps=10_000)
    volume = ctx["median_volume_units"]
    vol_bps = ctx["realized_vol_bps_per_bar"]
    latency = latency_min_ms + int(round(_unit(order_id, "latency") * (latency_max_ms - latency_min_ms)))
    volatility_cost = vol_bps * vol_mult
    latency_cost = vol_bps * math.sqrt(min(1.0, latency / max(1.0, ctx["bar_seconds"] * 1000))) * vol_mult
    participation = float(q) / volume
    impact_cost = impact_coeff * math.sqrt(max(0.0, participation))
    slip = base + volatility_cost + latency_cost + impact_cost
    fill_q = min(q, (Decimal(str(volume * max_part))).quantize(QUANTUM, rounding=ROUND_DOWN))
    rejected = _unit(order_id, "reject") < reject_rate
    if rejected:
        fill_q = Decimal("0")
    if fill_q <= 0:
        status = "REJECTED" if rejected else "CANCELED"
        reason = "synthetic_rejection" if rejected else "volume_cap_below_quantity_precision"
        fill_price = 0.0
    else:
        status = "SIMULATED" if fill_q >= q else "CANCELED"
        reason = "full_fill" if status == "SIMULATED" else "ioc_remainder_canceled_by_volume_cap"
        fill_price = price * (1 + (1 if side == "buy" else -1) * slip / 10_000)
        if not math.isfinite(fill_price) or fill_price <= 0:
            raise PaperExecutionError("Computed paper fill price is invalid")
    fee = float(fill_q) * fill_price * fee_bps / 10_000
    return {
        "status": status, "reason": reason, "requested_quantity": float(q), "filled_quantity": float(fill_q),
        "remaining_quantity": float(max(Decimal("0"), q - fill_q)), "average_fill_price": fill_price,
        "notional": float(fill_q) * fill_price, "fee": fee, "base_slippage_bps": base,
        "volatility_slippage_bps": volatility_cost, "latency_slippage_bps": latency_cost,
        "market_impact_slippage_bps": impact_cost, "total_slippage_bps": slip,
        "participation_ratio": participation, "max_volume_participation": max_part,
        "observed_median_volume_units": volume, "latency_ms": latency, "rejection_rate_assumption": reject_rate,
        "spread_observed": False, "execution_model": "DETERMINISTIC_OHLCV_PROXY_IOC",
        "fidelity": "SYNTHETIC_OHLCV_PROXY_NOT_VENUE_CALIBRATED", "promotion_grade_execution_evidence": False,
    }
