"""Deterministic heuristic cost model for platform-only paper orders.

This model deliberately does not claim to reproduce venue fills. Market impact is
estimated only when the context includes verified base-asset OHLCV volume and a
recent per-bar return volatility estimate. Missing inputs are reported explicitly.
"""
from __future__ import annotations

import hashlib
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
    volume_source = str(inputs.get("source") or "").strip().lower()
    context_asset = str(inputs.get("asset") or "").strip().lower()
    # Independent check at the estimator boundary: caller-supplied volume labels
    # alone are not enough to turn an arbitrary provider's volume field into
    # executable base-asset liquidity.
    impact_available = (
        volume is not None and volatility is not None and volume_unit == "base_asset"
        and volume_source == "ccxt_ohlcv" and context_asset == "crypto"
    )

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
    if not math.isfinite(total) or total >= 10_000.0:
        # A sell-side fill at >=100% adverse slippage is zero/negative. Refuse this
        # before an order row is created rather than leaving a pending paper trade.
        raise ValueError("modeled slippage must be finite and below 10000 bps")
    return {
        "status": status,
        "calibration_status": "UNVALIDATED_HEURISTIC",
        "side": normalized_side,
        "quantity": order_quantity,
        "base_slippage_bps": base,
        "half_spread_bps": half_spread,
        "market_impact_bps": impact,
        "bar_volume_participation": participation,
        "spread_inputs_complete": raw_spread is not None,
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


def simulate_paper_fill_step(
    *,
    remaining_quantity: float,
    context: dict[str, Any] | None,
    order_key: str,
    last_processed_bar_timestamp: str | None,
    bars_waited: int = 0,
    latency_bars: int = 1,
    max_bar_volume_participation: float = 0.10,
    queue_fill_probability: float = 0.80,
    rejection_probability: float = 0.01,
    max_fill_quantity: float | None = None,
) -> dict[str, Any]:
    """Advance one deterministic paper-order event using a *new* completed bar.

    This is deliberately conservative: only CCXT crypto OHLCV volume identified as
    base-asset units is accepted as liquidity. Unverified sources (including FX/OTC
    candle counts) do not fill. A deterministic order/bar hash approximates queue
    uncertainty and venue rejection reproducibly; it is not a calibrated exchange
    matching engine.
    """
    remaining = _finite_number(remaining_quantity, "remaining_quantity")
    if not str(order_key or "").strip():
        raise ValueError("order_key is required for deterministic paper fill simulation")
    if isinstance(bars_waited, bool) or int(bars_waited) < 0:
        raise ValueError("bars_waited must be a non-negative integer")
    if isinstance(latency_bars, bool) or int(latency_bars) < 1:
        raise ValueError("latency_bars must be an integer >= 1")
    participation = _finite_number(
        max_bar_volume_participation, "max_bar_volume_participation", strict_minimum=True
    )
    if participation > 1.0:
        raise ValueError("max_bar_volume_participation must be <= 1")
    queue_probability = _finite_number(queue_fill_probability, "queue_fill_probability")
    reject_probability = _finite_number(rejection_probability, "rejection_probability")
    if queue_probability > 1.0 or reject_probability > 1.0:
        raise ValueError("queue and rejection probabilities must be <= 1")

    inputs = context if isinstance(context, dict) else {}
    current_bar = str(inputs.get("bar_timestamp") or "").strip()
    source = str(inputs.get("source") or "").strip().lower()
    asset = str(inputs.get("asset") or "").strip().lower()
    volume_unit = str(inputs.get("volume_unit") or "").strip().lower()
    volume_value = inputs.get("bar_volume")

    def outcome(event: str, *, fill: float = 0.0, remaining_after: float = remaining,
                waited: int = int(bars_waited), timestamp: str | None = current_bar) -> dict[str, Any]:
        return {
            "event": event,
            "fill_quantity": fill,
            "remaining_quantity": remaining_after,
            "bars_waited": waited,
            "last_processed_bar_timestamp": timestamp,
        }

    if remaining <= 0.0:
        return outcome("ALREADY_FILLED", remaining_after=0.0)
    if not current_bar:
        return outcome("WAITING_FOR_VERIFIED_LIQUIDITY", timestamp=last_processed_bar_timestamp)
    if current_bar == str(last_processed_bar_timestamp or ""):
        return outcome("NO_NEW_BAR", timestamp=last_processed_bar_timestamp)

    verified_crypto_volume = source == "ccxt_ohlcv" and asset == "crypto" and volume_unit == "base_asset"
    if not verified_crypto_volume or volume_value is None:
        return outcome("WAITING_FOR_VERIFIED_LIQUIDITY")

    volume = _finite_number(volume_value, "bar_volume", strict_minimum=True)
    next_waited = int(bars_waited) + 1
    if next_waited < int(latency_bars):
        return outcome("LATENCY_WAIT", waited=next_waited)

    digest = hashlib.sha256(f"{order_key}|{current_bar}".encode("utf-8")).digest()
    rejection_draw = int.from_bytes(digest[0:8], "big") / float(2**64)
    queue_draw = int.from_bytes(digest[8:16], "big") / float(2**64)
    fraction_draw = int.from_bytes(digest[16:24], "big") / float(2**64)
    if rejection_draw < reject_probability:
        return outcome("REJECTED", remaining_after=0.0, waited=next_waited)
    if queue_draw >= queue_probability:
        return outcome("QUEUE_NO_FILL", waited=next_waited)

    capacity = volume * participation
    if max_fill_quantity is not None:
        capacity = min(
            capacity,
            _finite_number(max_fill_quantity, "max_fill_quantity"),
        )
    if capacity <= 0.0:
        return outcome("BAR_LIQUIDITY_EXHAUSTED", waited=next_waited)

    # Queue uncertainty reduces the available slice; each order/bar result stays
    # reproducible for retries and the same bar cannot be consumed twice.
    queue_factor = 0.5 + 0.5 * fraction_draw
    fill_quantity = min(remaining, capacity * queue_factor)
    if not math.isfinite(fill_quantity) or fill_quantity <= 0.0:
        return outcome("BAR_LIQUIDITY_EXHAUSTED", waited=next_waited)
    remaining_after = max(0.0, remaining - fill_quantity)
    event = "FILLED" if remaining_after <= max(1e-12, remaining * 1e-12) else "PARTIAL_FILL"
    if event == "FILLED":
        remaining_after = 0.0
    return outcome(
        event, fill=fill_quantity, remaining_after=remaining_after, waited=next_waited
    )
