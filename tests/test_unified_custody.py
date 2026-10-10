"""Regression tests for unified custody observations and transfer safety."""
import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.custody_locations import (
    create_custody_transfer,
    custody_gate,
    fresh_external_assets,
    flag_stale_custody_transfers,
    location_for_binance,
    record_custody_observation,
    transition_custody_transfer,
)
from app.db import Base, CustodyTransfer, utcnow


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


async def with_database(check):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await check(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def test_binance_location_is_customer_scoped():
    assert location_for_binance(42) == "EXCHANGE:BINANCE:CUSTOMER:42"
    with pytest.raises(ValueError):
        location_for_binance(0)


def test_observation_upsert_and_decimal_asset_sum():
    async def check(sessions):
        async with sessions() as db:
            await record_custody_observation(
                db, location=location_for_binance(1), provider="binance",
                currency="USDT", balance=Decimal("100.25"), source_reference="balance:1"
            )
            await record_custody_observation(
                db, location=location_for_binance(2), provider="binance",
                currency="USDT", balance=Decimal("50.75"), source_reference="balance:2"
            )
            await db.commit()
        async with sessions() as db:
            report = await fresh_external_assets(db)
            assert report["assets"] == Decimal("151.00")
            assert report["observation_count"] == 2
            assert report["fresh_observation_available"] is True
    asyncio.run(with_database(check))


def test_stale_exchange_observation_is_excluded():
    async def check(sessions):
        async with sessions() as db:
            await record_custody_observation(
                db, location=location_for_binance(1), provider="binance",
                currency="USDT", balance=100, source_reference="old",
                observed_at=utcnow() - timedelta(seconds=settings.custody_observation_max_age_seconds + 1)
            )
            await db.commit()
        async with sessions() as db:
            report = await fresh_external_assets(db)
            assert report["assets"] == Decimal("0")
            assert report["fresh_observation_available"] is False
    asyncio.run(with_database(check))


def test_enabled_custody_gate_fails_closed_without_fresh_observation(monkeypatch):
    monkeypatch.setattr(settings, "binance_custody_reconciliation_enabled", True)
    async def check(sessions):
        async with sessions() as db:
            report = await custody_gate(db, customer_liabilities=100)
            assert report["status"] == "UNKNOWN"
            assert report["reason"] == "NO_FRESH_EXTERNAL_CUSTODY_OBSERVATION"
    asyncio.run(with_database(check))


def test_custody_transfer_is_idempotent_and_state_machine_is_strict():
    async def check(sessions):
        async with sessions() as db:
            transfer = await create_custody_transfer(
                db, customer_id=7, currency="USDT", amount=25,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(7),
                idempotency_key="transfer:7:1",
            )
            replay = await create_custody_transfer(
                db, customer_id=7, currency="USDT", amount=25,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(7),
                idempotency_key="transfer:7:1",
            )
            assert replay.id == transfer.id
            await transition_custody_transfer(db, transfer.id, status="SUBMITTED", provider_reference="provider-1")
            await transition_custody_transfer(db, transfer.id, status="CONFIRMED", provider_reference="provider-1")
            with pytest.raises(ValueError, match="invalid custody transfer transition"):
                await transition_custody_transfer(db, transfer.id, status="SUBMITTED")
            await db.commit()
        async with sessions() as db:
            row = (await db.execute(select(CustodyTransfer))).scalar_one()
            assert row.status == "CONFIRMED"
            assert row.confirmed_at is not None
    asyncio.run(with_database(check))


def test_in_flight_transfer_does_not_count_as_exchange_asset():
    async def check(sessions):
        async with sessions() as db:
            await create_custody_transfer(
                db, customer_id=9, currency="USDT", amount=100,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(9),
                idempotency_key="transfer:9:1",
            )
            await db.commit()
        async with sessions() as db:
            report = await fresh_external_assets(db)
            assert report["assets"] == Decimal("0")
    asyncio.run(with_database(check))


def test_stale_custody_transfer_requires_reconciliation_without_release():
    async def check(sessions):
        old = utcnow() - timedelta(hours=25)
        async with sessions() as db:
            requested = await create_custody_transfer(
                db, customer_id=11, currency="USDT", amount=75,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(11),
                idempotency_key="transfer:11:stale-requested",
            )
            submitted = await create_custody_transfer(
                db, customer_id=12, currency="USDT", amount=80,
                source_location="CUSTODY:TRON",
                destination_location=location_for_binance(12),
                idempotency_key="transfer:12:stale-submitted",
            )
            requested.requested_at = old
            await transition_custody_transfer(
                db, submitted.id, status="SUBMITTED", provider_reference="provider-pending"
            )
            submitted.submitted_at = old
            await db.commit()

        async with sessions() as db:
            changed = await flag_stale_custody_transfers(db, timeout_hours=24)
            assert changed == 2
            await db.commit()

        async with sessions() as db:
            rows = (await db.execute(
                select(CustodyTransfer).where(CustodyTransfer.id.in_([requested.id, submitted.id]))
            )).scalars().all()
            assert {row.status for row in rows} == {"RECONCILIATION_REQUIRED"}
            for row in rows:
                detail = __import__("json").loads(row.detail_json)
                assert detail["timeout_review"]["action"] == "RECONCILIATION_REQUIRED_NO_AUTOMATIC_RELEASE"
            # A timeout must not manufacture provider confirmation or erase provider evidence.
            submitted_row = next(row for row in rows if row.id == submitted.id)
            assert submitted_row.provider_reference == "provider-pending"
            assert submitted_row.confirmed_at is None
    asyncio.run(with_database(check))
