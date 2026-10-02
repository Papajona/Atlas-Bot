"""Periodic re-verification of customer exchange-key permissions against the exchange itself.

Provisioning and order-time checks use flags stored when the key was verified. If someone later edits the key on Binance (enables
withdrawals, internal/universal transfer, margin or futures), the stored flags go stale. This audit re-reads GET /sapi/v1/account/apiRestrictions
(documented response fields: enableWithdrawals, enableInternalTransfer, permitsUniversalTransfer, enableMargin, enableFutures, enableVanillaOptions,
enableSpotAndMarginTrading, ipRestrict) and suspends any VERIFIED account whose live permissions violate the least-privilege policy. The existing live gate
already refuses status != VERIFIED, so suspension needs no change in the order path and no network call under a row lock.
A fetch failure never suspends (a Binance hiccup must not flap customers) but leaves a HIGH incident; stored flags keep protecting orders meanwhile.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

# Present-and-true on the live key means the key is more powerful than policy allows.
FORBIDDEN_WHEN_TRUE = {
    "enableWithdrawals": "withdrawals enabled",
    "enableInternalTransfer": "internal transfer enabled",
    "permitsUniversalTransfer": "universal transfer permitted",
    "enableMargin": "margin enabled",
    "enableFutures": "futures enabled",
    "enableVanillaOptions": "options enabled",
}


def evaluate_binance_restrictions(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    """Return {"violations": [...], "warnings": [...]}. Missing fields are treated as unverifiable, never as safe."""
    violations: list[str] = []
    warnings: list[str] = []
    for field, text in FORBIDDEN_WHEN_TRUE.items():
        if field not in payload:
            warnings.append(f"{field} missing from exchange response")
        elif bool(payload.get(field)):
            violations.append(text)
    if "enableSpotAndMarginTrading" in payload and not bool(payload.get("enableSpotAndMarginTrading")):
        violations.append("spot trading permission missing")
    if "ipRestrict" in payload and not bool(payload.get("ipRestrict")):
        warnings.append("no IP restriction on key")
    return {"violations": violations, "warnings": warnings}


Fetcher = Callable[[Any], Awaitable[Mapping[str, Any]]]


async def audit_customer_binance_keys(db, fetch_restrictions: Fetcher, *, limit: int = 200) -> dict[str, Any]:
    from sqlalchemy import select
    from .customer_funds import record_ledger_incident
    from .db import CustomerBinanceAccount, utcnow

    rows = (await db.execute(select(CustomerBinanceAccount).where(CustomerBinanceAccount.status == "VERIFIED")
                             .order_by(CustomerBinanceAccount.last_verified_at.asc().nullsfirst()).limit(limit))).scalars().all()
    out = {"checked": 0, "suspended": [], "unverified": [], "warnings": 0}
    for acct in rows:
        try:
            payload = await fetch_restrictions(acct)
        except Exception as exc:
            logger.warning("key_audit_fetch_failed customer=%s", acct.customer_id, exc_info=True)
            out["unverified"].append(int(acct.customer_id))
            await record_ledger_incident(db, key=f"KEY_AUDIT_UNVERIFIED:{acct.customer_id}", severity="HIGH",
                                         summary="Could not re-verify customer exchange-key permissions",
                                         detail={"customer_id": int(acct.customer_id), "error": type(exc).__name__}, customer_id=int(acct.customer_id))
            continue
        out["checked"] += 1
        verdict = evaluate_binance_restrictions(payload)
        out["warnings"] += len(verdict["warnings"])
        acct.enable_withdrawals = bool(payload.get("enableWithdrawals", acct.enable_withdrawals))
        acct.universal_transfer = bool(payload.get("permitsUniversalTransfer", acct.universal_transfer))
        acct.last_verified_at = utcnow()
        if verdict["violations"]:
            acct.status = "SUSPENDED"
            out["suspended"].append(int(acct.customer_id))
            logger.critical("key_audit_suspended customer=%s violations=%s", acct.customer_id, verdict["violations"])
            await record_ledger_incident(db, key=f"KEY_PERMISSIONS:{acct.customer_id}", severity="CRITICAL",
                                         summary="Customer exchange key exceeds least-privilege policy; account suspended",
                                         detail={"customer_id": int(acct.customer_id), "violations": verdict["violations"]},
                                         customer_id=int(acct.customer_id))
    await db.commit()
    return out


async def fetch_binance_restrictions(account: Any) -> Mapping[str, Any]:
    """Live call. Builds the customer's own broker and signs GET /sapi/v1/account/apiRestrictions (weight 1)."""
    from .customer_binance_execution import build_customer_binance_broker

    broker = build_customer_binance_broker(account, sandbox=False)
    client = broker._c()
    return await asyncio.to_thread(client.request, "account/apiRestrictions", "sapi", "GET", {})
