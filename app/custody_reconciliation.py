from __future__ import annotations

import httpx
from decimal import Decimal
from datetime import datetime, timezone
from sqlalchemy import select

from .config import settings
from .db import AppState, CustomerLedgerAccount, Incident, Wallet
from .custody_locations import fresh_external_assets
from .tron_reconciliation import parse_tron_usdt_balance

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


async def _halt_live_for_unverified_custody(db) -> None:
    """Fail closed when custody cannot be verified; re-enabling stays an operator action."""
    state = await db.get(AppState, 1)
    if state:
        state.kill_switch = True
        state.live_enabled = False


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


def _custody_addresses(*, wallets, treasury_address: str, treasury_address_role: str) -> list[str]:
    """List only on-chain addresses demonstrated to be Atlas-controlled.

    A Binance deposit address is external custody even if a historical setting
    names it as the treasury; it must never be added to Atlas assets as well.
    """
    role = str(treasury_address_role or "").strip().lower()
    if role not in {"atlas_controlled", "binance_deposit"}:
        raise ValueError("TRON treasury address role is not verified")
    addresses: list[str] = []
    seen: set[str] = set()
    if role == "atlas_controlled" and treasury_address:
        seen.add(treasury_address)
        addresses.append(treasury_address)
    for wallet in wallets:
        address = str(wallet.deposit_address or "").strip()
        if not address or (role == "binance_deposit" and address == treasury_address):
            continue
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return addresses


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
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("TRON provider returned a malformed JSON balance response")
    return parse_tron_usdt_balance(payload.get("data"), settings.usdt_tron_usdt_contract)


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

    treasury_role = str(settings.usdt_tron_treasury_address_role or "").strip().lower()
    if treasury_role not in {"atlas_controlled", "binance_deposit"}:
        report = {
            "status": "UNKNOWN",
            "asset": USDT,
            "network": TRON,
            "customer_liabilities": liabilities,
            "custody_onchain_balance": None,
            "coverage_gap": None,
            "solvency_ratio": None,
            "addresses_checked": 0,
            "treasury_address_role": treasury_role or "unknown",
            "error": "TRON treasury address ownership is unverified; configure an independently evidenced address role before custody reconciliation",
        }
        await _halt_live_for_unverified_custody(db)
        await _upsert_incident(
            db,
            key="CUSTODY_RECONCILIATION:USDT:TRON:ADDRESS_ROLE",
            severity="CRITICAL",
            category="CUSTODY_RECONCILIATION",
            summary="TRON treasury address ownership is unverified",
            detail=report,
        )
        return report

    wallets = (
        await db.execute(
            select(Wallet).where(
                Wallet.currency == USDT,
                Wallet.network == TRON,
                Wallet.status == "ACTIVE",
            )
        )
    ).scalars().all()
    addresses = _custody_addresses(
        wallets=wallets,
        treasury_address=settings.usdt_tron_treasury_address,
        treasury_address_role=treasury_role,
    )

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
        await _halt_live_for_unverified_custody(db)
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
        await _halt_live_for_unverified_custody(db)
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
        "treasury_balance": balances.get(settings.usdt_tron_treasury_address, D("0")) if treasury_role == "atlas_controlled" else None,
        "virtual_wallet_balance": (
            sum(balances.values(), D("0")) - balances.get(settings.usdt_tron_treasury_address, D("0"))
            if treasury_role == "atlas_controlled" else sum(balances.values(), D("0"))
        ),
        "treasury_address_role": treasury_role,
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
