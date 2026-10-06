from __future__ import annotations

import httpx
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select

from .config import settings
from .db import AppState, CustomerLedgerAccount, Incident, Wallet
from .custody_locations import fresh_external_assets

USDT = "USDT"
TRON = "TRON"
D = Decimal


async def _upsert_incident(db, *, key: str, severity: str, category: str, summary: str, detail: dict) -> None:
    """Create or refresh an incident in the caller transaction."""
    import json
    now = datetime.now(timezone.utc)
    row = (await db.execute(select(Incident).where(Incident.incident_key == key).with_for_update())).scalar_one_or_none()
    if row is None:
        db.add(Incident(
            incident_key=key,
            severity=severity,
            status="OPEN",
            category=category,
            summary=summary[:500],
            detail_json=json.dumps(detail, default=str),
            opened_at=now,
            updated_at=now,
        ))
        return
    row.severity = severity
    row.status = "OPEN"
    row.category = category
    row.summary = summary[:500]
    row.detail_json = json.dumps(detail, default=str)
    row.updated_at = now


async def _resolve_incident(db, *, key: str) -> None:
    row = (await db.execute(select(Incident).where(Incident.incident_key == key).with_for_update())).scalar_one_or_none()
    if row is not None and row.status == "OPEN":
        row.status = "RESOLVED"
        row.resolved_at = datetime.now(timezone.utc)
        row.resolved_by = "custody-reconciliation"
        row.updated_at = datetime.now(timezone.utc)


def solvency_decision(*, assets: Decimal, liabilities: Decimal, minimum_ratio: Decimal = D("1.0")) -> dict:
    """Pure solvency decision used by both the worker and regression tests."""
    assets = D(str(assets))
    liabilities = D(str(liabilities))
    minimum_ratio = D(str(minimum_ratio))
    if liabilities <= 0:
        return {
            "status": "OK",
            "solvency_ratio": None,
            "coverage_gap": assets,
            "reason": "NO_CUSTOMER_LIABILITIES",
        }
    ratio = assets / liabilities
    gap = assets - liabilities
    return {
        "status": "OK" if ratio >= minimum_ratio else "SHORTFALL",
        "solvency_ratio": ratio,
        "coverage_gap": gap,
        "reason": "ABOVE_THRESHOLD" if ratio >= minimum_ratio else "BELOW_THRESHOLD",
    }


async def _address_balance(client: httpx.AsyncClient, address: str) -> Decimal:
    response = await client.get(
        f"{settings.usdt_trongrid_base_url.rstrip('/')}/v1/accounts/{address}/trc20/balance",
        params={"contract_address": settings.usdt_tron_usdt_contract},
        headers={
            "accept": "application/json",
            "TRON-PRO-API-KEY": settings.usdt_trongrid_api_key,
        },
    )
    response.raise_for_status()
    data = response.json().get("data", [])
    token = next(
        (
            item for item in data
            if str((item.get("token_info") or {}).get("address") or "").lower()
            == str(settings.usdt_tron_usdt_contract).lower()
        ),
        None,
    )
    if not token:
        return D("0")
    raw = D(str(token.get("balance") or "0"))
    decimals = int((token.get("token_info") or {}).get("decimals") or 6)
    return raw / (D("10") ** decimals)


async def reconcile_usdt_custody(db) -> dict:
    """Reconcile all USDT customer liabilities against configured TRON custody.

    This never credits customer wallets and never sends funds. A confirmed shortfall
    opens a CRITICAL incident and engages the existing application kill switch so
    new live execution cannot continue on an under-collateralized book.
    """
    rows = (
        await db.execute(
            select(CustomerLedgerAccount).where(CustomerLedgerAccount.currency == USDT)
        )
    ).scalars().all()
    liabilities = (
        sum((D(str(row.available)) for row in rows), D("0"))
        + sum((D(str(row.trading_reserved)) for row in rows), D("0"))
        + sum((D(str(row.withdrawal_reserved)) for row in rows), D("0"))
    )

    wallets = (
        await db.execute(
            select(Wallet).where(
                Wallet.currency == USDT,
                Wallet.network == TRON,
                Wallet.status == "ACTIVE",
            )
        )
    ).scalars().all()
    addresses = []
    seen = set()
    if settings.usdt_tron_treasury_address:
        seen.add(settings.usdt_tron_treasury_address)
        addresses.append(settings.usdt_tron_treasury_address)
    for wallet in wallets:
        address = str(wallet.deposit_address or "").strip()
        if address and address not in seen:
            seen.add(address)
            addresses.append(address)

    if not settings.usdt_trongrid_api_key or not settings.usdt_tron_treasury_address:
        report = {
            "status": "ERROR",
            "asset": USDT,
            "network": TRON,
            "customer_liabilities": liabilities,
            "custody_onchain_balance": None,
            "coverage_gap": None,
            "solvency_ratio": None,
            "addresses_checked": len(addresses),
            "error": "TRON custody credentials or treasury address are not configured",
        }
        await _upsert_incident(
            db,
            key="CUSTODY_RECONCILIATION:USDT:TRON:CONFIG",
            severity="HIGH",
            category="CUSTODY_RECONCILIATION",
            summary="Custody reconciliation is not configured",
            detail=report,
        )
        return report

    balances = {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for address in addresses:
                balances[address] = await _address_balance(client, address)
    except Exception as exc:
        report = {
            "status": "ERROR",
            "asset": USDT,
            "network": TRON,
            "customer_liabilities": liabilities,
            "custody_onchain_balance": None,
            "coverage_gap": None,
            "solvency_ratio": None,
            "addresses_checked": len(balances),
            "error": str(exc),
        }
        await _upsert_incident(
            db,
            key="CUSTODY_RECONCILIATION:USDT:TRON:ERROR",
            severity="HIGH",
            category="CUSTODY_RECONCILIATION",
            summary="Custody reconciliation failed to read TRON balances",
            detail=report,
        )
        return report

    assets = sum(balances.values(), D("0"))
    exchange_report = None
    if settings.binance_custody_reconciliation_enabled:
        exchange_report = await fresh_external_assets(db, currency=USDT)
        if not exchange_report["fresh_observation_available"]:
            report = {
                "status": "UNKNOWN",
                "asset": USDT,
                "network": "UNIFIED_CUSTODY",
                "customer_liabilities": liabilities,
                "tron_custody_onchain_balance": assets,
                "exchange_assets": None,
                "coverage_gap": None,
                "solvency_ratio": None,
                "error": "No fresh Binance custody observation is available; exchange assets are not assumed to be zero",
            }
            state = await db.get(AppState, 1)
            if state:
                state.kill_switch = True
                state.live_enabled = False
            await _upsert_incident(
                db,
                key="CUSTODY_RECONCILIATION:USDT:BINANCE:UNKNOWN",
                severity="CRITICAL",
                category="CUSTODY_RECONCILIATION",
                summary="Unified custody reconciliation has no fresh Binance observation",
                detail=report,
            )
            return report
        assets += D(str(exchange_report["assets"]))
    # A prior UNKNOWN result must not permanently pin the kill switch once a
    # fresh external custody observation has actually been obtained. A fresh
    # observation can still produce SHORTFALL; that state is represented by the
    # dedicated solvency incident below.
    await _resolve_incident(db, key="CUSTODY_RECONCILIATION:USDT:BINANCE:UNKNOWN")
    decision = solvency_decision(
        assets=assets,
        liabilities=liabilities,
        minimum_ratio=D(str(settings.custody_solvency_min_ratio)),
    )
    report = {
        **decision,
        "asset": USDT,
        "network": TRON,
        "customer_liabilities": liabilities,
        "custody_onchain_balance": assets,
        "tron_custody_onchain_balance": sum(balances.values(), D("0")),
        "exchange_assets": exchange_report["assets"] if exchange_report else None,
        "exchange_observation_count": exchange_report["observation_count"] if exchange_report else 0,
        "addresses_checked": len(balances),
        "treasury_balance": balances.get(settings.usdt_tron_treasury_address, D("0")),
        "virtual_wallet_balance": sum(balances.values(), D("0")) - balances.get(settings.usdt_tron_treasury_address, D("0")),
    }

    if decision["status"] == "OK":
        for incident_key in (
            "CUSTODY_RECONCILIATION:USDT:TRON:CONFIG",
            "CUSTODY_RECONCILIATION:USDT:TRON:ERROR",
            "CUSTODY_SOLVENCY:USDT:TRON",
        ):
            await _resolve_incident(db, key=incident_key)

    if decision["status"] == "SHORTFALL":
        state = await db.get(AppState, 1)
        if state:
            state.kill_switch = True
            state.live_enabled = False
        await _upsert_incident(
            db,
            key="CUSTODY_SOLVENCY:USDT:TRON",
            severity="CRITICAL",
            category="CUSTODY_SOLVENCY",
            summary="USDT custody is below customer ledger liabilities",
            detail=report,
        )
    return report
