from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TradingRequirement:
    venue: str
    symbol: str
    status: str
    currency: str | None
    minimum_order_quantity: float | None
    minimum_order_notional: float | None
    minimum_balance: float | None
    balance_basis: str
    risk_required_balance: float | None
    reasons: tuple[str, ...]


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def spot_requirement(*, venue: str, symbol: str, price: float, market: dict[str, Any], stop_loss_price: float | None, risk_per_trade: float, risk_tolerance: float, currency: str = "USDT") -> TradingRequirement:
    limits = market.get("limits") or {}
    amount_min = _positive((limits.get("amount") or {}).get("min"))
    cost_min = _positive((limits.get("cost") or {}).get("min"))
    min_notional = cost_min
    if amount_min is not None:
        min_notional = max(min_notional or 0.0, amount_min * price)
    risk_balance = None
    reasons: list[str] = []
    if stop_loss_price is not None and amount_min is not None and risk_per_trade > 0:
        distance = abs(price - stop_loss_price)
        if distance > 0:
            risk_balance = distance * amount_min / (risk_per_trade * (1.0 + risk_tolerance))
        else:
            reasons.append("stop_loss_price_equals_entry_price")
    elif stop_loss_price is None:
        reasons.append("risk_required_balance_not_calculated_without_stop")
    else:
        reasons.append("risk_required_balance_not_calculated_without_exchange_minimum_quantity")
    candidates = [x for x in (min_notional, risk_balance) if x is not None]
    minimum_balance = max(candidates) if candidates else None
    if minimum_balance is None:
        reasons.append("provider_market_constraints_incomplete")
    return TradingRequirement(
        venue=venue, symbol=symbol,
        status="CALCULATED" if minimum_balance is not None else "INSUFFICIENT_PROVIDER_DATA",
        currency=currency, minimum_order_quantity=amount_min,
        minimum_order_notional=min_notional, minimum_balance=minimum_balance,
        balance_basis="exchange minimum plus risk-per-trade minimum; fees/slippage are not invented",
        risk_required_balance=risk_balance, reasons=tuple(reasons),
    )


def oanda_requirement(*, symbol: str, price: float, minimum_trade_size: float, stop_loss_price: float | None, risk_per_trade: float, risk_tolerance: float, account_currency: str) -> TradingRequirement:
    risk_balance = None
    reasons: list[str] = []
    if stop_loss_price is not None and risk_per_trade > 0 and abs(price - stop_loss_price) > 0:
        risk_balance = abs(price - stop_loss_price) * minimum_trade_size / (risk_per_trade * (1.0 + risk_tolerance))
    else:
        reasons.append("risk_required_balance_not_calculated_without_valid_stop")
    quote = symbol.split("_")[-1].upper() if "_" in symbol else ""
    notional = minimum_trade_size * price if quote == account_currency.upper() else None
    if notional is None:
        reasons.append("quote_to_account_currency_conversion_required")
    candidates = [x for x in (notional, risk_balance) if x is not None]
    return TradingRequirement(
        venue="oanda", symbol=symbol,
        status="CALCULATED" if candidates else "CONVERSION_REQUIRED",
        currency=account_currency, minimum_order_quantity=minimum_trade_size,
        minimum_order_notional=notional, minimum_balance=max(candidates) if candidates else None,
        balance_basis="OANDA minimum units plus risk-per-trade minimum; cross-currency conversion is not guessed",
        risk_required_balance=risk_balance, reasons=tuple(reasons),
    )


def deriv_requirement() -> TradingRequirement:
    return TradingRequirement(
        venue="deriv", symbol="", status="PROVIDER_QUOTE_REQUIRED", currency=None,
        minimum_order_quantity=None, minimum_order_notional=None, minimum_balance=None,
        balance_basis="Deriv exact contract pricing/proposal must be queried; Atlas does not hard-code a stake minimum",
        risk_required_balance=None,
        reasons=("Deriv live execution is currently disabled; no customer-live minimum is asserted",),
    )
