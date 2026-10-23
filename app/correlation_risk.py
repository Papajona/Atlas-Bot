from __future__ import annotations

import math

# Conservative risk buckets. These are not claimed statistical correlations; they prevent
# obvious concentration across highly related crypto/FX exposures when a live correlation
# matrix is unavailable. A research-time matrix can override the bucket coefficient.
_GROUPS={
 "BTC":"BTC_BETA","ETH":"L1_BETA","SOL":"L1_BETA","BNB":"L1_BETA","XRP":"L1_BETA","ADA":"L1_BETA","DOGE":"ALT_BETA",
 "EUR_USD":"USD_FX","GBP_USD":"USD_FX","AUD_USD":"USD_FX","NZD_USD":"USD_FX","USD_JPY":"USD_FX","USD_CHF":"USD_FX","USD_CAD":"USD_FX",
 "XAU_USD":"METALS","XAG_USD":"METALS","WTICO_USD":"ENERGY","BCO_USD":"ENERGY","NATGAS_USD":"ENERGY",
}

def risk_group(symbol: str) -> str:
    raw=str(symbol or "").upper().replace("/","_").replace(":USDT","")
    # Crypto keys in _GROUPS are base-asset tickers ("BTC"); FX/metals/energy keys are the
    # full pair ("EUR_USD", "XAU_USD"). Matching only the pre-underscore segment (the old
    # behavior) means every FX/metals/energy symbol's first segment is a bare currency code
    # ("EUR", "XAU", "WTICO") that never appears as a dict key -- every non-crypto symbol
    # silently fell through to "OTHER", and worse, ALL of them landed in that same bucket
    # together (gold counted against JPY exposure, etc.) instead of being correctly grouped
    # or correctly left ungrouped. Try the full normalized symbol first, then fall back to
    # the base-asset segment for the crypto case.
    if raw in _GROUPS:
        return _GROUPS[raw]
    base=raw.split("_")[0]
    return _GROUPS.get(base, "OTHER")

def adjusted_group_exposure(positions, *, proposed_symbol: str, proposed_notional: float, cap_usd: float) -> dict:
    target = risk_group(proposed_symbol)
    existing = 0.0
    members = []
    valuation_complete = True
    for p in positions or []:
        if risk_group(getattr(p, "symbol", "")) != target:
            continue
        quantity = float(getattr(p, "quantity", 0) or 0)
        if math.isfinite(quantity) and quantity == 0:
            members.append((getattr(p, "symbol", ""), 0.0))
            continue
        raw_price = getattr(p, "mark_price", 0)
        try:
            price = float(raw_price or 0)
        except (TypeError, ValueError):
            price = 0.0
        if not math.isfinite(price) or price <= 0:
            try:
                price = float(getattr(p, "average_entry_price", 0) or 0)
            except (TypeError, ValueError):
                price = 0.0
        if not math.isfinite(quantity) or not math.isfinite(price) or price <= 0:
            # Unknown exposure cannot safely be treated as zero.
            valuation_complete = False
            members.append((getattr(p, "symbol", ""), None))
            continue
        notional = abs(quantity * price)
        existing += notional
        members.append((getattr(p, "symbol", ""), notional))
    proposed = abs(float(proposed_notional or 0))
    cap = float(cap_usd)
    projected = existing + proposed
    allowed = valuation_complete and projected <= cap + 1e-9
    return {
        "group": target,
        "existing_notional": round(existing, 8),
        "proposed_notional": round(proposed, 8),
        "projected_notional": round(projected, 8),
        "cap_usd": cap,
        "allowed": allowed,
        "valuation_complete": valuation_complete,
        "members": members,
    }


def cap_quantity_preserving_reduction(
    quantity: float,
    opposing_quantity: float,
    *,
    existing_notional: float,
    reducing_notional: float,
    cap_usd: float,
    unit_price: float,
) -> float:
    """Cap only the increasing part of an order; never shrink its reducing part."""
    try:
        requested = float(quantity)
        opposing = float(opposing_quantity)
        existing = float(existing_notional)
        closing = float(reducing_notional)
        cap = float(cap_usd)
        price = float(unit_price)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(requested) or not math.isfinite(opposing):
        return 0.0
    reducing = min(max(0.0, requested), max(0.0, opposing))
    increasing = max(0.0, requested - reducing)
    if increasing <= 0:
        return reducing
    # Unknown cap/valuation/price is not a reason to block risk reduction, but it
    # must not authorize the increasing portion.
    if (
        not all(math.isfinite(value) for value in (existing, closing, cap, price))
        or existing < 0 or closing < 0 or cap < 0 or price <= 0
    ):
        return reducing
    exposure_after_close = max(0.0, existing - min(existing, closing))
    increasing_headroom = max(0.0, cap - exposure_after_close)
    allowed_increasing = min(increasing, increasing_headroom / price)
    return reducing + allowed_increasing
