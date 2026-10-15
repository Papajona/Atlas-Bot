"""Calibrated, liquidity-aware paper fill estimates.

This is a simulation model, not a broker adapter. It can estimate volume-capped
fills and price impact only when callers provide normalized market context and an
operator supplies traceable calibration evidence. Queue position, venue rejections,
and latency remain explicitly unmodelled.
"""
from __future__ import annotations

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


def calibrated_model_available(
    market_context: Any,
    *,
    max_participation_rate: Any,
    impact_coefficient: Any,
    evidence_source: str,
    evidence_id: str,
) -> bool:
    """Return True only when all numeric inputs and traceable calibration identity exist."""
    if not isinstance(market_context, Mapping):
        return False
    if not str(evidence_source or "").strip() or not str(evidence_id or "").strip():
        return False
    try:
        _finite(market_context.get("tradable_volume_units"), "tradable_volume_units", strictly_positive=True)
        _finite(market_context.get("spread_bps"), "spread_bps", minimum=0.0)
        _finite(market_context.get("recent_volatility_bps"), "recent_volatility_bps", minimum=0.0)
        participation = _finite(max_participation_rate, "max_participation_rate", strictly_positive=True)
        if participation > 1:
            return False
        _finite(impact_coefficient, "impact_coefficient", minimum=0.0)
    except PaperExecutionModelError:
        return False
    return True


def estimate_paper_fill(
    *,
    side: str,
    requested_quantity: Any,
    reference_price: Any,
    base_slippage_bps: Any,
    market_context: Mapping[str, Any],
    max_participation_rate: Any,
    impact_coefficient: Any,
    evidence_source: str,
    evidence_id: str,
) -> dict[str, Any]:
    """Estimate a bar-volume-capped fill and size/volatility-dependent price impact.

    Volume must already be normalized to the instrument's trade-quantity units.
    The impact coefficient and participation ceiling are assumptions and must be
    supplied with the evidence identifier that supports their calibration.
    """
    if side not in {"buy", "sell"}:
        raise PaperExecutionModelError("side must be 'buy' or 'sell'")
    if not isinstance(market_context, Mapping):
        raise PaperExecutionModelError("paper_market_context must be an object")
    source = str(evidence_source or "").strip()
    evidence = str(evidence_id or "").strip()
    if not source or not evidence:
        raise PaperExecutionModelError("traceable slippage calibration evidence is required")

    quantity = _finite(requested_quantity, "requested_quantity", strictly_positive=True)
    price = _finite(reference_price, "reference_price", strictly_positive=True)
    base_bps = _finite(base_slippage_bps, "base_slippage_bps", minimum=0.0)
    volume = _finite(market_context.get("tradable_volume_units"), "tradable_volume_units", strictly_positive=True)
    spread_bps = _finite(market_context.get("spread_bps"), "spread_bps", minimum=0.0)
    volatility_bps = _finite(market_context.get("recent_volatility_bps"), "recent_volatility_bps", minimum=0.0)
    max_participation = _finite(max_participation_rate, "max_participation_rate", strictly_positive=True)
    coefficient = _finite(impact_coefficient, "impact_coefficient", minimum=0.0)
    if max_participation > 1:
        raise PaperExecutionModelError("max_participation_rate cannot exceed 1")

    # Do not model an instant fill larger than the configured fraction of observed
    # tradable bar volume. The unfilled remainder is cancelled, not silently filled.
    capacity = volume * max_participation
    filled = min(quantity, capacity)
    if not math.isfinite(filled) or filled <= 0:
        raise PaperExecutionModelError("market context provides no positive fill capacity")
    participation = filled / volume
    impact_bps = coefficient * volatility_bps * math.sqrt(participation)
    total_slippage_bps = base_bps + spread_bps / 2.0 + impact_bps
    if not all(math.isfinite(x) and x >= 0 for x in (impact_bps, total_slippage_bps)):
        raise PaperExecutionModelError("calculated slippage is invalid")

    fill_price = price * (1.0 + total_slippage_bps / 10_000.0) if side == "buy" else price * (1.0 - total_slippage_bps / 10_000.0)
    if not math.isfinite(fill_price) or fill_price <= 0:
        raise PaperExecutionModelError("calculated fill price is invalid")
    unfilled_cancelled = max(0.0, quantity - filled)
    return {
        "model": "VOLUME_PARTICIPATION_VOLATILITY_V1",
        "model_status": "CALIBRATED_ASSUMPTIONS",
        "evidence_source": source,
        "evidence_id": evidence,
        "requested_quantity": quantity,
        "filled_quantity": filled,
        "cancelled_quantity": unfilled_cancelled,
        "full_fill": unfilled_cancelled == 0.0,
        "reference_price": price,
        "fill_price": fill_price,
        "base_slippage_bps": base_bps,
        "spread_bps": spread_bps,
        "half_spread_bps": spread_bps / 2.0,
        "recent_volatility_bps": volatility_bps,
        "tradable_volume_units": volume,
        "max_participation_rate": max_participation,
        "realized_participation_rate": participation,
        "impact_coefficient": coefficient,
        "market_impact_bps": impact_bps,
        "total_slippage_bps": total_slippage_bps,
        "model_limitations": [
            "latency_and_queue_position_not_modelled",
            "venue_rejection_probability_not_modelled",
            "cancelled_remainder_is_not_retried",
            "bar_volume_is_a_proxy_not_order_book_depth",
        ],
        "validation_grade": "INCOMPLETE_EXECUTION_MODEL",
    }
