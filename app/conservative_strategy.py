"""Deterministic, fail-closed conservative strategy policy.

This module is intentionally independent of AI/provider code. The profile is frozen
and is enforced at the central execution boundary for strategy='atlas_conservative_v1'.
All instrument-specific money conversion must be supplied by trusted broker metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import math
from typing import Any, Mapping


class ConservativePolicyError(ValueError):
    """The proposed trade cannot be safely evaluated under the conservative policy."""


@dataclass(frozen=True, slots=True)
class ConservativeRiskProfile:
    name: str = "atlas_conservative_v1"
    risk_fraction: float = 0.0025
    daily_loss_fraction: float = 0.01
    max_drawdown_fraction: float = 0.05
    max_open_positions: int = 2
    max_leverage: float = 1.0
    min_reward_risk: float = 2.0
    max_spread_bps: float = 30.0


CONSERVATIVE_PROFILE = ConservativeRiskProfile()


def _positive_number(metadata: Mapping[str, Any], key: str) -> float:
    value = metadata.get(key)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConservativePolicyError(f"missing_or_invalid_instrument_metadata:{key}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ConservativePolicyError(f"missing_or_invalid_instrument_metadata:{key}")
    return number


def conservative_quantity(
    *,
    equity: float,
    entry_price: float,
    stop_loss_price: float,
    take_profit_price: float,
    side: str,
    metadata: Mapping[str, Any] | None,
    profile: ConservativeRiskProfile = CONSERVATIVE_PROFILE,
) -> float:
    """Size from stop risk using provider-confirmed contract and account-currency data.

    Required metadata:
      contract_multiplier, quote_to_account_rate, amount_step, min_amount,
      max_amount, price_tick.
    """
    if not isinstance(metadata, Mapping):
        raise ConservativePolicyError("instrument_metadata_required")
    equity = float(equity)
    entry = float(entry_price)
    stop = float(stop_loss_price)
    target = float(take_profit_price)
    if not all(math.isfinite(v) and v > 0 for v in (equity, entry, stop, target)):
        raise ConservativePolicyError("invalid_equity_or_trade_prices")
    if side not in {"buy", "sell"}:
        raise ConservativePolicyError("invalid_side")
    if side == "buy" and not (stop < entry < target):
        raise ConservativePolicyError("invalid_long_stop_or_target")
    if side == "sell" and not (target < entry < stop):
        raise ConservativePolicyError("invalid_short_stop_or_target")

    contract = _positive_number(metadata, "contract_multiplier")
    conversion = _positive_number(metadata, "quote_to_account_rate")
    step = _positive_number(metadata, "amount_step")
    min_amount = _positive_number(metadata, "min_amount")
    max_amount = _positive_number(metadata, "max_amount")
    _positive_number(metadata, "price_tick")

    risk_distance = abs(entry - stop)
    reward_distance = abs(target - entry)
    if reward_distance + 1e-12 < profile.min_reward_risk * risk_distance:
        raise ConservativePolicyError("minimum_reward_risk_not_met")

    risk_per_unit = risk_distance * contract * conversion
    if not math.isfinite(risk_per_unit) or risk_per_unit <= 0:
        raise ConservativePolicyError("invalid_instrument_risk")
    raw_quantity = (equity * profile.risk_fraction) / risk_per_unit
    step_d = Decimal(str(step))
    raw_d = Decimal(str(raw_quantity))
    quantity = float((raw_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d)
    if not math.isfinite(quantity) or quantity < min_amount:
        raise ConservativePolicyError("quantity_below_instrument_minimum")
    if quantity > max_amount:
        raise ConservativePolicyError("quantity_exceeds_instrument_maximum")

    notional = quantity * entry * contract * conversion
    if not math.isfinite(notional) or notional > equity * profile.max_leverage + 1e-8:
        raise ConservativePolicyError("maximum_leverage_exceeded")
    realized_risk = risk_per_unit * quantity
    if realized_risk > equity * profile.risk_fraction + 1e-8:
        raise ConservativePolicyError("risk_per_trade_limit")
    return quantity


def validate_conservative_order(
    *,
    equity: float,
    entry_price: float,
    quantity: float,
    stop_loss_price: float | None,
    take_profit_price: float | None,
    side: str,
    spread_bps: float | None,
    metadata: Mapping[str, Any] | None,
    profile: ConservativeRiskProfile = CONSERVATIVE_PROFILE,
) -> None:
    """Fail closed when order-level conservative invariants cannot be proven."""
    if not isinstance(metadata, Mapping):
        raise ConservativePolicyError("instrument_metadata_required")
    contract = _positive_number(metadata, "contract_multiplier")
    conversion = _positive_number(metadata, "quote_to_account_rate")
    step = _positive_number(metadata, "amount_step")
    min_amount = _positive_number(metadata, "min_amount")
    max_amount = _positive_number(metadata, "max_amount")
    _positive_number(metadata, "price_tick")
    if stop_loss_price is None or take_profit_price is None:
        raise ConservativePolicyError("protective_stop_and_target_required")
    sized = conservative_quantity(
        equity=equity, entry_price=entry_price, stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price, side=side, metadata=metadata, profile=profile,
    )
    quantity = float(quantity)
    if not math.isfinite(quantity) or quantity <= 0:
        raise ConservativePolicyError("invalid_quantity")
    if quantity > sized + max(step * 1e-8, 1e-10):
        raise ConservativePolicyError("quantity_exceeds_conservative_size")
    if quantity < min_amount or quantity > max_amount:
        raise ConservativePolicyError("quantity_outside_instrument_limits")
    if abs(quantity / step - round(quantity / step)) > 1e-7:
        raise ConservativePolicyError("quantity_not_aligned_to_instrument_step")
    if spread_bps is None or not math.isfinite(float(spread_bps)) or not 0 <= float(spread_bps) <= profile.max_spread_bps:
        raise ConservativePolicyError("spread_missing_or_exceeds_conservative_limit")
    notional = quantity * float(entry_price) * contract * conversion
    if notional > float(equity) * profile.max_leverage + 1e-8:
        raise ConservativePolicyError("maximum_leverage_exceeded")


def execution_aware_backtest(
    bars,
    *,
    side_column: str = "signal",
    fee_bps: float = 5.0,
    slippage_bps: float = 5.0,
    stop_column: str = "stop_loss",
    target_column: str = "take_profit",
):
    """Conservative OHLC bar simulator with next-open entry and adverse gap fills.

    A signal on bar t enters at bar t+1 open. If stop and target both touch in one
    bar, stop wins (pessimistic). A stop gap fills at the worse of stop and open.
    Returns a copy with entry/exit/return fields; it is research tooling, not a
    broker simulator and must not be treated as profitability proof.
    """
    required = {"open", "high", "low", "close", side_column, stop_column, target_column}
    missing = required - set(getattr(bars, "columns", ()))
    if missing:
        raise ConservativePolicyError("backtest_missing_columns:" + ",".join(sorted(missing)))
    if fee_bps < 0 or slippage_bps < 0 or not math.isfinite(fee_bps + slippage_bps):
        raise ConservativePolicyError("invalid_backtest_costs")
    out = bars.copy()
    out["entry_price"] = float("nan")
    out["exit_price"] = float("nan")
    out["trade_return"] = 0.0
    cost = (fee_bps + slippage_bps) / 10_000.0
    for i in range(len(out) - 1):
        row = out.iloc[i]
        raw_side = row[side_column]
        side = 1 if raw_side in (1, "buy", "long") else (-1 if raw_side in (-1, "sell", "short") else 0)
        if not side:
            continue
        stop, target = row[stop_column], row[target_column]
        if not (math.isfinite(float(stop)) and math.isfinite(float(target))):
            continue
        entry = float(out.iloc[i + 1]["open"])
        if not math.isfinite(entry) or entry <= 0:
            continue
        if (side == 1 and not stop < entry < target) or (side == -1 and not target < entry < stop):
            continue
        nxt = out.iloc[i + 1]
        low, high, op = float(nxt["low"]), float(nxt["high"]), float(nxt["open"])
        stop_hit = low <= stop if side == 1 else high >= stop
        target_hit = high >= target if side == 1 else low <= target
        if stop_hit:
            exit_price = min(op, float(stop)) if side == 1 else max(op, float(stop))
        elif target_hit:
            exit_price = float(target)
        else:
            exit_price = float(nxt["close"])
        gross = side * (exit_price / entry - 1.0)
        out.iloc[i, out.columns.get_loc("entry_price")] = entry
        out.iloc[i, out.columns.get_loc("exit_price")] = exit_price
        out.iloc[i, out.columns.get_loc("trade_return")] = gross - 2.0 * cost
    return out
