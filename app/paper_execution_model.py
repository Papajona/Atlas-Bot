"""Calibrated, liquidity-aware paper fill estimates.

This is a simulation model, not a broker adapter. It estimates volume-capped fills,
spread/volatility/participation impact, latency-aware adverse cost, and reproducible
venue rejection outcomes only when traceable calibration inputs are supplied.

Queue position and order-book depth remain unmodelled; volume is a bar-level proxy.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from typing import Any


class PaperExecutionModelError(ValueError):
    """Paper execution context is incomplete or invalid."""


def _finite(value: Any, name: str, *, minimum: float | None = None,
            strictly_positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PaperExecutionModelError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise PaperExecutionModelError(f"{name} must be a finite number")
    if strictly_positive and number <= 0:
        raise PaperExecutionModelError(f"{name} must be greater than zero")
    if minimum is not None and number < minimum:
        raise PaperExecutionModelError(f"{name} must be at least {minimum}")
    return number


def _valid_evidence_id(value: Any) -> bool:
    """Require a SHA-256 digest that can pin the exact calibration artifact."""
    raw = str(value or "").strip()
    digest = raw.removeprefix("sha256:")
    if len(digest) != 64:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in digest)


def _latency_from_quantiles(
    uniform_draw: float,
    *,
    minimum_ms: float,
    median_ms: float,
    p95_ms: float,
    maximum_ms: float,
) -> float:
    """Deterministically interpolate declared empirical latency quantiles."""
    anchors = ((0.0, minimum_ms), (0.50, median_ms), (0.95, p95_ms), (1.0, maximum_ms))
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if uniform_draw <= x1:
            if x1 == x0:
                return float(y1)
            weight = (uniform_draw - x0) / (x1 - x0)
            return float(y0 + weight * (y1 - y0))
    return float(maximum_ms)


def calibrated_model_available(
    market_context: Any,
    *,
    max_participation_rate: Any,
    impact_coefficient: Any,
    rejection_probability: Any,
    latency_min_ms: Any,
    latency_median_ms: Any,
    latency_p95_ms: Any,
    latency_max_ms: Any,
    latency_impact_coefficient: Any,
    evidence_source: str,
    evidence_id: str,
) -> bool:
    """Require every fill, rejection, and latency assumption plus its evidence identity."""
    if not isinstance(market_context, Mapping):
        return False
    if not str(evidence_source or "").strip() or not _valid_evidence_id(evidence_id):
        return False
    try:
        _finite(market_context.get("tradable_volume_units"), "tradable_volume_units", strictly_positive=True)
        _finite(market_context.get("spread_bps"), "spread_bps", minimum=0.0)
        _finite(market_context.get("recent_volatility_bps"), "recent_volatility_bps", minimum=0.0)
        _finite(market_context.get("bar_duration_ms"), "bar_duration_ms", strictly_positive=True)
        participation = _finite(max_participation_rate, "max_participation_rate", strictly_positive=True)
        if participation > 1:
            return False
        _finite(impact_coefficient, "impact_coefficient", minimum=0.0)
        rejection = _finite(rejection_probability, "rejection_probability", minimum=0.0)
        if rejection > 1:
            return False
        minimum_ms = _finite(latency_min_ms, "latency_min_ms", minimum=0.0)
        median_ms = _finite(latency_median_ms, "latency_median_ms", strictly_positive=True)
        p95_ms = _finite(latency_p95_ms, "latency_p95_ms", strictly_positive=True)
        maximum_ms = _finite(latency_max_ms, "latency_max_ms", strictly_positive=True)
        _finite(latency_impact_coefficient, "latency_impact_coefficient", minimum=0.0)
        if not minimum_ms <= median_ms <= p95_ms <= maximum_ms:
            return False
    except PaperExecutionModelError:
        return False
    return True


def estimate_paper_fill(
    *,
    order_key: str,
    side: str,
    requested_quantity: Any,
    reference_price: Any,
    base_slippage_bps: Any,
    market_context: Mapping[str, Any],
    max_participation_rate: Any,
    impact_coefficient: Any,
    rejection_probability: Any,
    latency_min_ms: Any,
    latency_median_ms: Any,
    latency_p95_ms: Any,
    latency_max_ms: Any,
    latency_impact_coefficient: Any,
    evidence_source: str,
    evidence_id: str,
) -> dict[str, Any]:
    """Estimate a bar-volume-capped fill with calibrated latency/rejection assumptions.

    Rejection and latency draws are deterministic by order_key so the same paper
    order is reproducible across retries. Quantile interpolation is an explicit model
    assumption. The latency impact coefficient must be fitted against traceable execution
    observations; square-root time scaling is an approximation, not a venue guarantee.
    """
    if side not in {"buy", "sell"}:
        raise PaperExecutionModelError("side must be 'buy' or 'sell'")
    if not isinstance(market_context, Mapping):
        raise PaperExecutionModelError("paper_market_context must be an object")
    key = str(order_key or "").strip()
    source = str(evidence_source or "").strip()
    evidence = str(evidence_id or "").strip()
    if not key:
        raise PaperExecutionModelError("a stable order_key is required for reproducible simulation")
    if not source or not _valid_evidence_id(evidence):
        raise PaperExecutionModelError("calibration source and SHA-256 evidence ID are required")

    quantity = _finite(requested_quantity, "requested_quantity", strictly_positive=True)
    price = _finite(reference_price, "reference_price", strictly_positive=True)
    base_bps = _finite(base_slippage_bps, "base_slippage_bps", minimum=0.0)
    volume = _finite(market_context.get("tradable_volume_units"), "tradable_volume_units", strictly_positive=True)
    spread_bps = _finite(market_context.get("spread_bps"), "spread_bps", minimum=0.0)
    volatility_bps = _finite(market_context.get("recent_volatility_bps"), "recent_volatility_bps", minimum=0.0)
    bar_duration_ms = _finite(market_context.get("bar_duration_ms"), "bar_duration_ms", strictly_positive=True)
    max_participation = _finite(max_participation_rate, "max_participation_rate", strictly_positive=True)
    coefficient = _finite(impact_coefficient, "impact_coefficient", minimum=0.0)
    rejection_rate = _finite(rejection_probability, "rejection_probability", minimum=0.0)
    minimum_ms = _finite(latency_min_ms, "latency_min_ms", minimum=0.0)
    median_ms = _finite(latency_median_ms, "latency_median_ms", strictly_positive=True)
    p95_ms = _finite(latency_p95_ms, "latency_p95_ms", strictly_positive=True)
    maximum_ms = _finite(latency_max_ms, "latency_max_ms", strictly_positive=True)
    latency_coefficient = _finite(latency_impact_coefficient, "latency_impact_coefficient", minimum=0.0)

    if max_participation > 1:
        raise PaperExecutionModelError("max_participation_rate cannot exceed 1")
    if rejection_rate > 1:
        raise PaperExecutionModelError("rejection_probability cannot exceed 1")
    if not minimum_ms <= median_ms <= p95_ms <= maximum_ms:
        raise PaperExecutionModelError("latency quantiles must satisfy min <= median <= p95 <= max")

    digest = hashlib.sha256(key.encode("utf-8")).digest()
    rejection_draw = int.from_bytes(digest[:8], "big") / float(2**64)
    latency_draw = int.from_bytes(digest[8:16], "big") / float(2**64)
    simulated_latency_ms = _latency_from_quantiles(
        latency_draw,
        minimum_ms=minimum_ms,
        median_ms=median_ms,
        p95_ms=p95_ms,
        maximum_ms=maximum_ms,
    )
    simulated_rejection = rejection_draw < rejection_rate

    # Never model an instant fill larger than the configured fraction of observed
    # tradable bar volume. Any residual quantity is explicitly cancelled.
    capacity = volume * max_participation
    planned_fill = min(quantity, capacity)
    if not math.isfinite(planned_fill) or planned_fill <= 0:
        raise PaperExecutionModelError("market context provides no positive fill capacity")

    common = {
        "model": "VOLUME_PARTICIPATION_VOLATILITY_LATENCY_REJECTION_V2",
        "evidence_source": source,
        "evidence_id": evidence,
        "order_key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
        "requested_quantity": quantity,
        "reference_price": price,
        "base_slippage_bps": base_bps,
        "spread_bps": spread_bps,
        "half_spread_bps": spread_bps / 2.0,
        "recent_volatility_bps": volatility_bps,
        "tradable_volume_units": volume,
        "max_participation_rate": max_participation,
        "bar_duration_ms": bar_duration_ms,
        "impact_coefficient": coefficient,
        "rejection_probability": rejection_rate,
        "rejection_draw": rejection_draw,
        "latency_min_ms": minimum_ms,
        "latency_median_ms": median_ms,
        "latency_p95_ms": p95_ms,
        "latency_max_ms": maximum_ms,
        "latency_quantile_draw": latency_draw,
        "simulated_latency_ms": simulated_latency_ms,
        "latency_quantile_method": "PIECEWISE_LINEAR_FROM_CALIBRATED_QUANTILES",
        "latency_impact_coefficient": latency_coefficient,
        "model_limitations": [
            "queue_position_not_modelled",
            "bar_volume_is_a_proxy_not_order_book_depth",
            "cancelled_remainder_is_not_retried",
            "latency_price_adjustment_uses_calibrated_coefficient_and_sqrt_time_approximation",
        ],
        "validation_grade": "INCOMPLETE_EXECUTION_MODEL",
    }
    if simulated_rejection:
        return {
            **common,
            "model_status": "SIMULATED_VENUE_REJECTION",
            "simulated_rejection": True,
            "filled_quantity": 0.0,
            "cancelled_quantity": quantity,
            "full_fill": False,
            "market_impact_bps": 0.0,
            "latency_impact_bps": 0.0,
            "total_slippage_bps": None,
            "fill_price": None,
        }

    filled = planned_fill
    participation = filled / volume
    impact_bps = coefficient * volatility_bps * math.sqrt(participation)
    latency_impact_bps = latency_coefficient * volatility_bps * math.sqrt(simulated_latency_ms / bar_duration_ms)
    total_slippage_bps = base_bps + spread_bps / 2.0 + impact_bps + latency_impact_bps
    if not all(
        math.isfinite(value) and value >= 0
        for value in (impact_bps, latency_impact_bps, total_slippage_bps)
    ):
        raise PaperExecutionModelError("calculated slippage is invalid")

    fill_price = price * (1.0 + total_slippage_bps / 10_000.0) if side == "buy" else price * (1.0 - total_slippage_bps / 10_000.0)
    if not math.isfinite(fill_price) or fill_price <= 0:
        raise PaperExecutionModelError("calculated fill price is invalid")
    unfilled_cancelled = max(0.0, quantity - filled)
    return {
        **common,
        "model_status": "CALIBRATED_ASSUMPTIONS",
        "simulated_rejection": False,
        "filled_quantity": filled,
        "cancelled_quantity": unfilled_cancelled,
        "full_fill": unfilled_cancelled == 0.0,
        "fill_price": fill_price,
        "realized_participation_rate": participation,
        "market_impact_bps": impact_bps,
        "latency_impact_bps": latency_impact_bps,
        "total_slippage_bps": total_slippage_bps,
    }
