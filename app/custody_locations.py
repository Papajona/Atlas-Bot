"""Unified custody-location state and reconciliation primitives.

This module deliberately does not move money or call an exchange. External observers must
prove an actual balance and persist the observation before it can contribute to solvency.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from .config import settings
from .db import CustodyAssetObservation, CustodyTransfer, utcnow

D = Decimal

TRANSFER_STATES = {
    "REQUESTED", "SUBMITTED", "CONFIRMED", "FAILED", "UNKNOWN", "RECONCILIATION_REQUIRED",
}
_ALLOWED = {
    "REQUESTED": {"SUBMITTED", "FAILED", "UNKNOWN", "RECONCILIATION_REQUIRED"},
    "SUBMITTED": {"CONFIRMED", "FAILED", "UNKNOWN", "RECONCILIATION_REQUIRED"},
    "CONFIRMED": set(),
    "FAILED": set(),
    "UNKNOWN": {"SUBMITTED", "CONFIRMED", "FAILED", "RECONCILIATION_REQUIRED"},
    "RECONCILIATION_REQUIRED": {"SUBMITTED", "CONFIRMED", "FAILED", "UNKNOWN"},
}

def _amount(value) -> Decimal:
    amount = D(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("custody amount must be finite and non-negative")
    return amount

def location_for_binance(customer_id: int) -> str:
    if int(customer_id) <= 0:
        raise ValueError("customer_id must be positive")
    return f"EXCHANGE:BINANCE:CUSTOMER:{int(customer_id)}"

async def record_custody_observation(db, *, location: str, provider: str, currency: str,
                                     balance, source_reference: str, customer_id: int | None = None,
                                     observed_at: datetime | None = None) -> CustodyAssetObservation:
    location = str(location or "").strip()
    provider = str(provider or "").strip().lower()
    currency = str(currency or "").strip().upper()
    source_reference = str(source_reference or "").strip()
    if not location or not provider or not currency or not source_reference:
        raise ValueError("location, provider, currency, and source_reference are required")
    amount = _amount(balance)
    observed = observed_at or utcnow()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    row = (await db.execute(
        select(CustodyAssetObservation).where(
            CustodyAssetObservation.location == location,
            CustodyAssetObservation.currency == currency,
        ).with_for_update()
    )).scalar_one_or_none()
    if row is None:
        row = CustodyAssetObservation(location=location, provider=provider, customer_id=customer_id,
                                      currency=currency, balance=float(amount), status="ACTIVE",
                                      source_reference=source_reference, observed_at=observed)
        db.add(row)
    else:
        row.provider = provider
        row.customer_id = customer_id
        row.balance = float(amount)
        row.status = "ACTIVE"
        row.source_reference = source_reference
        row.observed_at = observed
    return row

async def create_custody_transfer(db, *, customer_id: int | None, currency: str, amount,
                                  source_location: str, destination_location: str,
                                  idempotency_key: str) -> CustodyTransfer:
    amount_d = _amount(amount)
    if amount_d <= 0:
        raise ValueError("custody transfer amount must be positive")
    if not source_location or not destination_location or source_location == destination_location:
        raise ValueError("custody transfer locations are invalid")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("custody transfer idempotency_key is required")
    existing = (await db.execute(
        select(CustodyTransfer).where(CustodyTransfer.idempotency_key == key).with_for_update()
    )).scalar_one_or_none()
    if existing:
        if (D(str(existing.amount)) != amount_d or existing.currency != str(currency).upper()
                or existing.source_location != source_location
                or existing.destination_location != destination_location
                or existing.customer_id != customer_id):
            raise ValueError("custody transfer idempotency conflict")
        return existing
    row = CustodyTransfer(customer_id=customer_id, currency=str(currency).upper(),
                          amount=float(amount_d), source_location=source_location,
                          destination_location=destination_location, status="REQUESTED",
                          idempotency_key=key)
    db.add(row)
    await db.flush()
    return row

async def transition_custody_transfer(db, transfer_id: int, *, status: str,
                                      provider_reference: str = "", detail: dict | None = None) -> CustodyTransfer:
    target = str(status or "").upper()
    if target not in TRANSFER_STATES:
        raise ValueError("unknown custody transfer state")
    row = (await db.execute(
        select(CustodyTransfer).where(CustodyTransfer.id == transfer_id).with_for_update()
    )).scalar_one()
    if target != row.status and target not in _ALLOWED.get(row.status, set()):
        raise ValueError(f"invalid custody transfer transition {row.status}->{target}")
    now = utcnow()
    if target == "SUBMITTED" and row.submitted_at is None:
        row.submitted_at = now
    if target == "CONFIRMED":
        if not str(provider_reference or row.provider_reference).strip():
            raise ValueError("confirmed custody transfer requires provider_reference")
        if row.confirmed_at is None:
            row.confirmed_at = now
    if provider_reference:
        row.provider_reference = str(provider_reference)
    if detail is not None:
        row.detail_json = json.dumps(detail, default=str, separators=(",", ":"))
    row.status = target
    row.updated_at = now
    return row

async def fresh_external_assets(db, *, currency: str = "USDT", provider: str = "binance") -> dict:
    """Only fresh observations count; in-flight transfers never count as assets."""
    cutoff = utcnow() - timedelta(seconds=max(1, int(settings.custody_observation_max_age_seconds)))
    rows = (await db.execute(
        select(CustodyAssetObservation).where(
            CustodyAssetObservation.currency == currency.upper(),
            CustodyAssetObservation.provider == provider.lower(),
            CustodyAssetObservation.status == "ACTIVE",
            CustodyAssetObservation.observed_at >= cutoff,
        )
    )).scalars().all()
    assets = sum((D(str(row.balance)) for row in rows), D("0"))
    return {
        "assets": assets,
        "observations": [
            {"location": row.location, "provider": row.provider, "customer_id": row.customer_id,
             "currency": row.currency, "balance": str(D(str(row.balance))),
             "source_reference": row.source_reference,
             "observed_at": row.observed_at.isoformat() if row.observed_at else None}
            for row in rows
        ],
        "observation_count": len(rows),
        "fresh_observation_available": bool(rows),
    }

async def custody_gate(db, *, customer_liabilities, currency: str = "USDT") -> dict:
    """Exchange-custody solvency decision. UNKNOWN never unlocks live execution."""
    liabilities = _amount(customer_liabilities)
    if not settings.binance_custody_reconciliation_enabled:
        return {"status": "DISABLED", "customer_liabilities": liabilities,
                "exchange_assets": None, "reason": "BINANCE_CUSTODY_RECONCILIATION_DISABLED"}
    report = await fresh_external_assets(db, currency=currency)
    if not report["fresh_observation_available"]:
        return {"status": "UNKNOWN", "customer_liabilities": liabilities, "exchange_assets": None,
                "reason": "NO_FRESH_EXTERNAL_CUSTODY_OBSERVATION", **report}
    assets = report["assets"]
    ratio = None if liabilities <= 0 else assets / liabilities
    status = "OK" if liabilities <= 0 or ratio >= D(str(settings.custody_solvency_min_ratio)) else "SHORTFALL"
    return {"status": status, "customer_liabilities": liabilities, "exchange_assets": assets,
            "solvency_ratio": ratio, "coverage_gap": assets - liabilities,
            "reason": "ABOVE_THRESHOLD" if status == "OK" else "BELOW_THRESHOLD", **report}
