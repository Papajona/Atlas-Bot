"""Deterministic heuristic cost model for platform-only paper orders.

This model deliberately does not claim to reproduce venue fills. Market impact is
estimated only when the context includes verified base-asset OHLCV volume and a
recent per-bar return volatility estimate. Missing inputs are reported explicitly.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _finite_number(value: Any, name: str, *, minimum: float = 0.0, strict_minimum: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be a finite number")
    if parsed < minimum or (strict_minimum and parsed == minimum):
        comparator = "greater than" if strict_minimum else "at least"
        raise ValueError(f"{name} must be {comparator} {minimum}")
    return parsed


def paper_execution_context_from_ohlcv(
    frame: pd.DataFrame,
    *,
    asset: str,
    spread_bps: float | None = None,
) -> dict[str, Any]:
    """Build context without mistaking tick counts for traded volume.

    CCXT OHLCV volume is accepted as base-asset volume only when Atlas's attached
    provenance explicitly identifies the source as ccxt_ohlcv. OANDA candle counts
    and Yahoo Finance volume fields are not treated as traded base-asset quantity.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("paper execution context requires a non-empty OHLCV frame")
    if "close" not in frame.columns or "volume" not in frame.columns:
        raise ValueError("paper execution context requires close and volume columns")
    if not frame.index.is_monotonic_increasing or not frame.index.is_unique:
        raise ValueError("paper execution context index must be unique and chronological")

    normalized_asset = str(asset or "").strip().lower()
    provenance = frame.attrs.get("data_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    source = str(provenance.get("source") or "UNKNOWN")
    context: dict[str, Any] = {
        "asset": normalized_asset,
        "source": source,
        "bar_timestamp": frame.index[-1].isoformat() if hasattr(frame.index[-1], "isoformat") else str(frame.index[-1]),
        "bar_volume": None,
        "volume_unit": None,
        "realized_volatility_per_bar": None,
        "spread_bps": None,
    }

    if spread_bps is not None:
        context["spread_bps"] = _finite_number(spread_bps, "spread_bps")

    close = pd.to_numeric(frame["close"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    returns = np.log(close).diff().replace([np.inf, -np.inf], np.nan).dropna().tail(20)
    if len(returns) >= 2:
        volatility = float(returns.std(ddof=1))
        if math.isfinite(volatility) and volatility >= 0:
            context["realized_volatility_per_bar"] = volatility

    # Never infer that a provider's volume field is measured in base-asset units.
    if normalized_asset == "crypto" and source == "ccxt_ohlcv":
        volume = _finite_number(frame["volume"].iloc[-1], "latest_base_asset_volume")
        if volume > 0:
            context["bar_volume"] = volume
            context["volume_unit"] = "base_asset"

    return context


def estimate_paper_slippage(
    *,
    side: str,
    quantity: float,
    base_slippage_bps: float,
    context: dict[str, Any] | None,
    impact_coefficient: float = 0.5,
    max_market_impact_bps: float = 250.0,
) -> dict[str, Any]:
    """Estimate costs with a square-root participation impact term.

    Impact = coefficient * per-bar volatility * sqrt(order quantity / observed
    base-asset bar volume) * 10,000. This is an uncalibrated heuristic, not a
    venue execution model. Output distinguishes dynamic estimates from fallback.
    """
    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    order_quantity = _finite_number(quantity, "quantity", strict_minimum=True)
    base = _finite_number(base_slippage_bps, "base_slippage_bps")
    coefficient = _finite_number(impact_coefficient, "impact_coefficient")
    impact_cap = _finite_number(max_market_impact_bps, "max_market_impact_bps", strict_minimum=True)
    inputs = context if isinstance(context, dict) else {}

    raw_spread = inputs.get("spread_bps")
    half_spread = 0.0 if raw_spread is None else _finite_number(raw_spread, "spread_bps") / 2.0

    volume = inputs.get("bar_volume")
    volatility = inputs.get("realized_volatility_per_bar")
    volume_unit = str(inputs.get("volume_unit") or "").strip().lower()
    impact_available = volume is not None and volatility is not None and volume_unit == "base_asset"

    if impact_available:
        base_volume = _finite_number(volume, "bar_volume", strict_minimum=True)
        per_bar_volatility = _finite_number(volatility, "realized_volatility_per_bar")
        participation = order_quantity / base_volume
        raw_impact = coefficient * per_bar_volatility * math.sqrt(participation) * 10_000.0
        if not math.isfinite(raw_impact):
            raise ValueError("estimated market impact is non-finite")
        impact = min(raw_impact, impact_cap)
        status = "DYNAMIC_IMPACT_ESTIMATE"
    else:
        participation = None
        impact = None
        status = "BASELINE_ONLY_MISSING_VERIFIED_IMPACT_INPUTS"

    total = base + half_spread + (impact if impact is not None else 0.0)
    return {
        "status": status,
        "calibration_status": "UNVALIDATED_HEURISTIC",
        "side": normalized_side,
        "quantity": order_quantity,
        "base_slippage_bps": base,
        "half_spread_bps": half_spread,
        "market_impact_bps": impact,
        "bar_volume_participation": participation,
        "modeled_slippage_bps": total,
        "impact_inputs_complete": bool(impact_available),
        "volume_unit": volume_unit or None,
        "volume_source": inputs.get("source"),
        "volatility_measure": "realized_log_return_std_per_bar" if impact_available else None,
    }


def adverse_fill_price(reference_price: float, side: str, slippage_bps: float) -> float:
    """Return an adverse side-dependent paper fill, rejecting invalid input."""
    price = _finite_number(reference_price, "reference_price", strict_minimum=True)
    slippage = _finite_number(slippage_bps, "slippage_bps")
    normalized_side = str(side or "").strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    result = price * (1.0 + slippage / 10_000.0 if normalized_side == "buy" else 1.0 - slippage / 10_000.0)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("modeled paper fill price is non-positive or non-finite")
    return result
