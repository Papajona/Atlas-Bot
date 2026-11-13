"""Shared deterministic execution-cost arithmetic for research and paper paths.

The functions here are independent of venue adapters. Callers must supply evidence-backed
spread, volatility, participation, fee and financing inputs. A common formula does not
make missing or assumed inputs venue-calibrated.
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

import numpy as np

EXECUTION_COST_MODEL_VERSION = "atlas-execution-cost-v1"


def _finite_nonnegative(value: Any, name: str, *, strictly_positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite and non-negative") from exc
    if not math.isfinite(parsed) or parsed < 0 or (strictly_positive and parsed <= 0):
        qualifier = "positive" if strictly_positive else "non-negative"
        raise ValueError(f"{name} must be finite and {qualifier}")
    return parsed


def market_impact_bps(
    *,
    volatility_bps: Any,
    participation_rate: Any,
    impact_coefficient: Any,
    volatility_horizon_fraction: Any = 1.0,
    max_market_impact_bps: Any | None = None,
) -> float:
    """Shared square-root impact model used by every paper/research execution path."""
    volatility = _finite_nonnegative(volatility_bps, "volatility_bps")
    participation = _finite_nonnegative(participation_rate, "participation_rate")
    coefficient = _finite_nonnegative(impact_coefficient, "impact_coefficient")
    horizon = _finite_nonnegative(
        volatility_horizon_fraction, "volatility_horizon_fraction"
    )
    if participation > 1.0:
        raise ValueError("participation_rate cannot exceed 1")
    impact = coefficient * volatility * math.sqrt(participation * horizon)
    if max_market_impact_bps is not None:
        impact = min(
            impact,
            _finite_nonnegative(max_market_impact_bps, "max_market_impact_bps"),
        )
    if not math.isfinite(impact) or impact < 0:
        raise ValueError("market impact calculation produced an invalid value")
    return impact


def execution_cost_components(
    *,
    base_slippage_bps: Any,
    spread_bps: Any,
    volatility_bps: Any,
    participation_rate: Any,
    impact_coefficient: Any,
    latency_ms: Any = 0.0,
    bar_duration_ms: Any = 1.0,
    latency_impact_coefficient: Any = 0.0,
    max_market_impact_bps: Any | None = None,
) -> dict[str, float | str]:
    """Return one-side spread, square-root impact, latency impact and slippage.

    spread_bps is the full quoted spread; a single execution pays half-spread.
    Market impact is coefficient times volatility_bps times square-root participation.
    Latency impact uses the same square-root time approximation in every caller.
    Fees and financing are separate because their event timing differs from slippage.
    """
    base = _finite_nonnegative(base_slippage_bps, "base_slippage_bps")
    spread = _finite_nonnegative(spread_bps, "spread_bps")
    volatility = _finite_nonnegative(volatility_bps, "volatility_bps")
    participation = _finite_nonnegative(participation_rate, "participation_rate")
    coefficient = _finite_nonnegative(impact_coefficient, "impact_coefficient")
    latency = _finite_nonnegative(latency_ms, "latency_ms")
    duration = _finite_nonnegative(bar_duration_ms, "bar_duration_ms", strictly_positive=True)
    latency_coefficient = _finite_nonnegative(
        latency_impact_coefficient, "latency_impact_coefficient"
    )
    if participation > 1.0:
        raise ValueError("participation_rate cannot exceed 1")
    market_impact = market_impact_bps(
        volatility_bps=volatility,
        participation_rate=participation,
        impact_coefficient=coefficient,
        max_market_impact_bps=max_market_impact_bps,
    )
    latency_impact = latency_coefficient * volatility * math.sqrt(latency / duration)
    half_spread = spread / 2.0
    total_slippage = base + half_spread + market_impact + latency_impact
    values = (market_impact, latency_impact, half_spread, total_slippage)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("execution-cost calculation produced an invalid value")
    return {
        "model_version": EXECUTION_COST_MODEL_VERSION,
        "half_spread_bps": half_spread,
        "market_impact_bps": market_impact,
        "latency_impact_bps": latency_impact,
        "total_slippage_bps": total_slippage,
    }


def adverse_fill_price(reference_price: Any, side: str, slippage_bps: Any) -> float:
    """Apply one-side slippage adversely to a buy or sell reference price."""
    price = _finite_nonnegative(reference_price, "reference_price", strictly_positive=True)
    slippage = _finite_nonnegative(slippage_bps, "slippage_bps")
    normalized_side = str(side).strip().lower()
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    fraction = slippage / 10_000.0
    result = price * (1.0 + fraction if normalized_side == "buy" else 1.0 - fraction)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("adverse fill price must remain finite and positive")
    return result



def deterministic_execution_draws(order_key: str) -> tuple[float, float]:
    """Return reproducible rejection and latency uniforms for the paper execution model."""
    key = str(order_key or "").strip()
    if not key:
        raise ValueError("order_key is required for deterministic execution draws")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    scale = float(2**64)
    return (
        int.from_bytes(digest[:8], "big") / scale,
        int.from_bytes(digest[8:16], "big") / scale,
    )


def latency_from_quantiles(
    uniform_draw: Any,
    *,
    minimum_ms: Any,
    median_ms: Any,
    p95_ms: Any,
    maximum_ms: Any,
) -> float:
    """Interpolate calibrated latency quantiles using the shared paper-model method."""
    try:
        draw = float(uniform_draw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("latency draw must be a finite number in [0, 1)") from exc
    if not math.isfinite(draw) or not 0.0 <= draw < 1.0:
        raise ValueError("latency draw must be a finite number in [0, 1)")
    minimum = _finite_nonnegative(minimum_ms, "minimum_ms")
    median = _finite_nonnegative(median_ms, "median_ms", strictly_positive=True)
    p95 = _finite_nonnegative(p95_ms, "p95_ms", strictly_positive=True)
    maximum = _finite_nonnegative(maximum_ms, "maximum_ms", strictly_positive=True)
    if not minimum <= median <= p95 <= maximum:
        raise ValueError("latency quantiles must satisfy min <= median <= p95 <= max")
    anchors = ((0.0, minimum), (0.50, median), (0.95, p95), (1.0, maximum))
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if draw <= x1:
            if x1 == x0:
                return float(y1)
            weight = (draw - x0) / (x1 - x0)
            return float(y0 + weight * (y1 - y0))
    return float(maximum)

def net_returns_after_costs(
    gross_returns: Any,
    *,
    turnover: Any,
    execution_cost_bps: Any,
    exposure: Any = 0.0,
    financing_cost_bps_per_bar: Any = 0.0,
    cost_multiplier: Any = 1.0,
) -> np.ndarray:
    """Vectorized shared net-return calculation for aligned strategy return streams."""
    try:
        gross, traded, costs, size, financing, multiplier = np.broadcast_arrays(
            np.asarray(gross_returns, dtype=float),
            np.asarray(turnover, dtype=float),
            np.asarray(execution_cost_bps, dtype=float),
            np.asarray(exposure, dtype=float),
            np.asarray(financing_cost_bps_per_bar, dtype=float),
            np.asarray(cost_multiplier, dtype=float),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("net-return inputs must be numeric and broadcast-compatible") from exc
    if not np.isfinite(gross).all() or not np.isfinite(size).all():
        raise ValueError("gross returns and exposures must be finite")
    for name, values in (
        ("turnover", traded),
        ("execution_cost_bps", costs),
        ("financing_cost_bps_per_bar", financing),
        ("cost_multiplier", multiplier),
    ):
        if not np.isfinite(values).all() or np.any(values < 0):
            raise ValueError(f"{name} must be finite and non-negative")
    if np.any(multiplier <= 0):
        raise ValueError("cost_multiplier must be greater than zero")
    net = gross - traded * costs * multiplier / 10_000.0
    net -= np.abs(size) * financing * multiplier / 10_000.0
    if not np.isfinite(net).all():
        raise ValueError("net return calculation produced a non-finite value")
    return np.asarray(net, dtype=float)


def net_return_after_costs(
    gross_return: Any,
    *,
    turnover: Any,
    execution_cost_bps: Any,
    exposure: Any = 0.0,
    financing_cost_bps_per_bar: Any = 0.0,
    cost_multiplier: Any = 1.0,
) -> float:
    """Scalar cost calculation without allocating vectorized NumPy buffers."""
    try:
        gross = float(gross_return)
        raw_exposure = float(exposure)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("gross return and exposure must be finite numbers") from exc
    if not math.isfinite(gross) or not math.isfinite(raw_exposure):
        raise ValueError("gross return and exposure must be finite numbers")
    traded = _finite_nonnegative(turnover, "turnover")
    cost = _finite_nonnegative(execution_cost_bps, "execution_cost_bps")
    financing = _finite_nonnegative(
        financing_cost_bps_per_bar, "financing_cost_bps_per_bar"
    )
    multiplier = _finite_nonnegative(
        cost_multiplier, "cost_multiplier", strictly_positive=True
    )
    result = gross - traded * cost * multiplier / 10_000.0
    result -= abs(raw_exposure) * financing * multiplier / 10_000.0
    if not math.isfinite(result):
        raise ValueError("net return calculation produced a non-finite value")
    return result
