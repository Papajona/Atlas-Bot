from __future__ import annotations

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
    target=risk_group(proposed_symbol); existing=0.0
    members=[]
    for p in positions or []:
        if risk_group(getattr(p,"symbol", "")) == target:
            notional=abs(float(getattr(p,"quantity",0) or 0)*float(getattr(p,"mark_price",0) or 0))
            existing += notional; members.append((getattr(p,"symbol",""),notional))
    projected=existing+abs(float(proposed_notional or 0))
    return {"group":target,"existing_notional":round(existing,8),"proposed_notional":round(abs(float(proposed_notional or 0)),8),"projected_notional":round(projected,8),"cap_usd":float(cap_usd),"allowed":projected <= float(cap_usd)+1e-9,"members":members}
