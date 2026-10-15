"""Pure helpers for turning a ccxt order's fee data into a quote-currency amount.

ccxt unified orders carry ``order["fee"]`` ({"currency","cost","rate"}) and/or ``order["fees"]`` (a list). Both are
optional: several venues omit fees from create_order/fetch_order responses (they live in fetch_my_trades), and a fee can be
charged in the base asset or a third asset (e.g. an exchange token). The values are cumulative for the order, so callers
must settle only the delta against what has already been charged.
"""
from __future__ import annotations

from typing import Any
from decimal import Decimal, localcontext

EXACT, CONVERTED, ESTIMATED = "exact", "converted", "estimated"


def split_symbol(symbol: str) -> tuple[str, str]:
    """'BTC/USDT' or 'BTC/USDT:USDT' -> ('BTC', 'USDT')."""
    pair = str(symbol or "").split(":", 1)[0]
    if "/" in pair:
        base, quote = pair.split("/", 1)
        return base.strip().upper(), quote.strip().upper()
    return pair.strip().upper(), ""


def _fee_entries(order: dict[str, Any]) -> list[dict[str, Any]]:
    fees = order.get("fees")
    if isinstance(fees, list) and any(isinstance(f, dict) and f.get("cost") is not None for f in fees):
        return [f for f in fees if isinstance(f, dict) and f.get("cost") is not None]
    fee = order.get("fee")
    if isinstance(fee, dict) and fee.get("cost") is not None:
        return [fee]
    return []


def extract_order_fee_quote_decimal(
    order: dict[str, Any],
    symbol: str,
    fill_price: Any,
    filled: Any,
    *,
    fallback_taker_bps: Any,
) -> tuple[Decimal, str, str]:
    """Convert cumulative provider fees to quote currency with Decimal arithmetic."""
    try:
        fill_price_d = Decimal(str(fill_price))
        filled_d = Decimal(str(filled))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ValueError("fill price and quantity must be valid decimal numbers") from exc
    if not fill_price_d.is_finite() or not filled_d.is_finite():
        raise ValueError("fill price and quantity must be finite")
    if filled_d <= 0 or fill_price_d <= 0:
        return Decimal("0"), EXACT, ""
    base, quote = split_symbol(symbol)
    source_order = order or {}
    entries = _fee_entries(source_order)
    raw_fee_list = source_order.get("fees")
    malformed_fee_records = (
        isinstance(raw_fee_list, list)
        and any(not isinstance(item, dict) or item.get("cost") is None for item in raw_fee_list)
    )
    total = Decimal("0")
    quality, note = EXACT, ""
    if malformed_fee_records:
        quality = ESTIMATED
        note = "one or more exchange fee records have no usable cost"
    for entry in entries:
        try:
            cost = Decimal(str(entry.get("cost")))
        except (TypeError, ValueError, ArithmeticError):
            note = "exchange returned an unparsable fee cost"
            quality = ESTIMATED
            continue
        cur = str(entry.get("currency") or "").upper()
        if not cost.is_finite():
            note = "exchange returned a non-finite fee cost"
            quality = ESTIMATED
            continue
        if cost <= 0:
            # Negative provider values are maker rebates; do not credit them.
            continue
        with localcontext() as context:
            context.prec = 50
            if cur == quote or (not cur and quote):
                total = total + cost
            elif cur == base:
                total = total + cost * fill_price_d
                quality = CONVERTED if quality == EXACT else quality
            else:
                note = f"fee currency {cur or '?'} cannot be priced in {quote or '?'}"
                quality = ESTIMATED
    if quality == ESTIMATED or not entries or malformed_fee_records:
        try:
            bps = Decimal(str(fallback_taker_bps))
        except (TypeError, ValueError, ArithmeticError):
            bps = Decimal("0")
            note = (note + "; " if note else "") + "invalid fallback fee rate"
        if not bps.is_finite() or bps < 0:
            bps = Decimal("0")
            note = (note + "; " if note else "") + "invalid fallback fee rate"
        with localcontext() as context:
            context.prec = 50
            estimated = filled_d * fill_price_d * (bps / Decimal("10000"))
        if not estimated.is_finite():
            return Decimal("0"), ESTIMATED, "fee estimate is non-finite; manual reconciliation required"
        if not entries:
            note = "exchange returned no fee data"
        return max(total, estimated), ESTIMATED, note
    return total, quality, note


def extract_order_fee_quote(order: dict[str, Any], symbol: str, fill_price: float, filled: float, *,
                            fallback_taker_bps: float) -> tuple[float, str, str]:
    """Backward-compatible float wrapper; money-moving execution uses the Decimal variant."""
    fee, quality, note = extract_order_fee_quote_decimal(
        order, symbol, fill_price, filled, fallback_taker_bps=fallback_taker_bps
    )
    return float(fee), quality, note
