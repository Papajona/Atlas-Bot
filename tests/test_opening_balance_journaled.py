import asyncio
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from app.config import settings
from app.customer_funds import get_or_create_ledger, ledger_invariant_report
from app.db import Base, Wallet, LedgerJournal, LedgerEntry
import pytest

@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


def run(check):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
            await check(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()
    asyncio.run(go())

def test_legacy_wallet_migration_is_journaled():
    async def check(S):
        async with S() as db:
            db.add(Wallet(customer_id=7, currency="USDT", available_balance=50.0, status="ACTIVE"))
            await db.commit()
            led = await get_or_create_ledger(db, 7, "USDT")
            await db.commit()
            assert Decimal(str(led.available)) == Decimal("50.000000")
            rep = await ledger_invariant_report(db)
            assert rep["ok"] is True, rep
            journals = (await db.execute(select(LedgerJournal).where(LedgerJournal.entry_type == "OPENING_BALANCE"))).scalars().all()
            entries = (await db.execute(select(LedgerEntry).where(LedgerEntry.entry_type == "OPENING_BALANCE"))).scalars().all()
            assert len(journals) == 1
            assert len(entries) == 1
    run(check)

def test_opening_balance_is_idempotent_and_quantized():
    async def check(S):
        async with S() as db:
            db.add(Wallet(customer_id=9, currency="USDT", available_balance=0.1 + 0.2, status="ACTIVE"))
            await db.commit()
            led = await get_or_create_ledger(db, 9, "USDT")
            await db.commit()
            # Existing ledger rows do not re-run legacy migration.
            led2 = await get_or_create_ledger(db, 9, "USDT")
            await db.commit()
            assert Decimal(str(led.available)) == Decimal("0.300000")
            assert Decimal(str(led2.available)) == Decimal("0.300000")
            assert (await ledger_invariant_report(db))["ok"] is True
    run(check)
