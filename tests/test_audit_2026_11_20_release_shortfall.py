"""release_trading must flag, not hide, a release larger than the reserved balance."""
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.customer_funds import release_trading
from app.db import Base, CustomerLedgerAccount, Incident


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


def test_release_larger_than_reserve_releases_only_reserve_and_opens_incident():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1"), trading_reserved=Decimal("10")))
            await db.commit()
            await release_trading(db, 1, 25, reference_id="shortfall:1")
            await db.commit()
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("0")
            assert ledger.available == Decimal("11")
            incident = (await db.execute(select(Incident))).scalar_one()
            assert incident.incident_key == "RELEASE_EXCEEDS_RESERVE:shortfall:1:1"
    asyncio.run(with_database(check))


def test_exact_release_opens_no_incident():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1"), trading_reserved=Decimal("10")))
            await db.commit()
            await release_trading(db, 1, 10, reference_id="exact:1")
            await db.commit()
            assert (await db.execute(select(Incident))).first() is None
    asyncio.run(with_database(check))



def test_one_micro_usdt_release_shortfall_is_not_hidden_by_tolerance():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=2, available=Decimal("1"), trading_reserved=Decimal("10")))
            await db.commit()
            await release_trading(db, 2, 10.000001, reference_id="shortfall:one-micro")
            await db.commit()
            incident = (await db.execute(select(Incident))).scalar_one()
            assert incident.incident_key == "RELEASE_EXCEEDS_RESERVE:shortfall:one-micro:2"
            assert "0.000001" in incident.detail_json
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("0")
            assert ledger.available == Decimal("11.000000")

    asyncio.run(with_database(check))


def test_release_replay_rejects_different_requested_amount_without_mutating_ledger():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(
                customer_id=3, available=Decimal("1"), trading_reserved=Decimal("10")
            ))
            await db.commit()

            await release_trading(db, 3, 4, reference_id="replay-mismatch:1")
            await db.commit()

            with pytest.raises(ValueError, match="idempotency key already used"):
                await release_trading(db, 3, 5, reference_id="replay-mismatch:1")
            await db.rollback()

            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("6.000000")
            assert ledger.available == Decimal("5.000000")

    asyncio.run(with_database(check))


def test_release_replay_accepts_same_request_and_preserves_shortfall_request_amount():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(
                customer_id=4, available=Decimal("1"), trading_reserved=Decimal("10")
            ))
            await db.commit()

            await release_trading(db, 4, 25, reference_id="replay-shortfall:1")
            await db.commit()
            await release_trading(db, 4, 25, reference_id="replay-shortfall:1")
            await db.commit()

            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.trading_reserved == Decimal("0.000000")
            assert ledger.available == Decimal("11.000000")

    asyncio.run(with_database(check))
