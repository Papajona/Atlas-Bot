"""Pure helpers for turning a ccxt order's fee data into a quote-currency amount.

ccxt unified orders carry ``order["fee"]`` ({"currency","cost","rate"}) and/or ``order["fees"]`` (a list). Both are
optional: several venues omit fees from create_order/fetch_order responses (they live in fetch_my_trades), and a fee can be
charged in the base asset or a third asset (e.g. an exchange token). The values are cumulative for the order, so callers
must settle only the delta against what has already been charged.
"""
from __future__ import annotations

from typing import Any

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


def extract_order_fee_quote(order: dict[str, Any], symbol: str, fill_price: float, filled: float, *,
                            fallback_taker_bps: float) -> tuple[float, str, str]:
    """Return (cumulative_fee_in_quote, quality, note).

    quality is EXACT (fee already in the quote currency), CONVERTED (base-asset fee priced at the fill) or ESTIMATED
    (no usable fee data, or a fee currency that cannot be priced here -> conservative taker-fee estimate).
    A negative fee (maker rebate) is treated as zero: rebates are not credited to customers automatically.
    """
    if filled <= 0 or fill_price <= 0:
        return 0.0, EXACT, ""
    base, quote = split_symbol(symbol)
    entries = _fee_entries(order or {})
    total, quality, note = 0.0, EXACT, ""
    for entry in entries:
        try:
            cost = float(entry.get("cost"))
        except (TypeError, ValueError):
            continue
        cur = str(entry.get("currency") or "").upper()
        if cost <= 0:
            continue
        if cur == quote or (not cur and quote):
            total += cost
        elif cur == base:
            total += cost * fill_price
            quality = CONVERTED if quality == EXACT else quality
        else:
            note = f"fee currency {cur or '?'} cannot be priced in {quote or '?'}"
            quality = ESTIMATED
    if quality == ESTIMATED or not entries:
        est = filled * fill_price * (float(fallback_taker_bps) / 10_000.0)
        if not entries:
            note = "exchange returned no fee data"
        return max(total, est), ESTIMATED, note
    return total, quality, note
