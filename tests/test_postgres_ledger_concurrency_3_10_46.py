"""PostgreSQL concurrency drill against the REAL ledger code (the older 3_10_39 test only exercises a probe table).

Runs when DATABASE_URL (as set in CI) or ATLAS_POSTGRES_URL points at PostgreSQL that has had `alembic upgrade head`
applied, so the production CHECK constraints and deferred journal-balance triggers are active.
Invariants asserted after contention:
  * no bucket negative, total money conserved, every journal balanced, retries/duplicates post exactly once.
"""
import asyncio
import os
import uuid
from decimal import Decimal

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.config import settings
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


def _pg_url():
    for name in ("ATLAS_POSTGRES_URL", "DATABASE_URL"):
        v = os.getenv(name, "").strip()
        if v.startswith("postgresql"):
            return v
    pytest.skip("Set ATLAS_POSTGRES_URL/DATABASE_URL to a migrated PostgreSQL database")


async def _sessions():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    engine = create_async_engine(_pg_url(), pool_size=30, max_overflow=0,
                                 connect_args={"statement_cache_size": 0, "server_settings": {"lock_timeout": "20000"}})
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _customer(sessions, available):
    from app.db import CustomerProfile, CustomerLedgerAccount
    async with sessions() as db:
        p = CustomerProfile(auth_user_id=f"drill-{uuid.uuid4().hex}", email="drill@example.invalid", display_name="drill", status="ACTIVE")
        db.add(p)
        await db.flush()
        db.add(CustomerLedgerAccount(customer_id=p.id, available=Decimal(available)))
        await db.commit()
        return p.id


async def _balances(sessions, cid):
    from sqlalchemy import select
    from app.db import CustomerLedgerAccount, LedgerJournal, LedgerJournalLine
    async with sessions() as db:
        l = (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.customer_id == cid))).scalar_one()
        lines = (await db.execute(select(LedgerJournalLine).where(LedgerJournalLine.customer_id == cid))).scalars().all()
        return l, lines


@pytest.mark.asyncio
async def test_concurrent_withdrawal_reserves_never_overdraw():
    from app.customer_funds import reserve_withdrawal
    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "100")

        async def attempt(i):
            async with sessions() as db:
                try:
                    await reserve_withdrawal(db, cid, 30, reference_id=f"drill-{cid}-{i}")
                    await db.commit()
                    return True
                except ValueError:
                    await db.rollback()
                    return False

        results = await asyncio.gather(*[attempt(i) for i in range(25)])
        ledger, lines = await _balances(sessions, cid)
        assert sum(results) == 3                                   # 3 x 30 fits in 100; the other 22 must fail
        assert ledger.available == Decimal("10") and ledger.withdrawal_reserved == Decimal("90")
        assert ledger.available + ledger.withdrawal_reserved == Decimal("100")  # money conserved
        assert sum(l.debit for l in lines) == sum(l.credit for l in lines)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_duplicate_reference_under_contention_posts_once():
    from app.customer_funds import reserve_withdrawal
    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "100")

        async def attempt():
            async with sessions() as db:
                await reserve_withdrawal(db, cid, 40, reference_id=f"same-{cid}")
                await db.commit()

        await asyncio.gather(*[attempt() for _ in range(20)])
        ledger, _ = await _balances(sessions, cid)
        assert ledger.available == Decimal("60") and ledger.withdrawal_reserved == Decimal("40")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_fee_and_loss_settlement_never_goes_negative_and_books_deficit():
    from app.customer_funds import settle_realized_pnl, settle_trading_fee
    engine, sessions = await _sessions()
    try:
        cid = await _customer(sessions, "50")

        async def loss(i):
            async with sessions() as db:
                await settle_realized_pnl(db, customer_id=cid, amount=-20, reference_id=f"loss-{cid}-{i}", strict=False)
                await db.commit()

        async def fee(i):
            async with sessions() as db:
                await settle_trading_fee(db, customer_id=cid, fee=5, reference_id=f"fee-{cid}-{i}", strict=False)
                await db.commit()

        await asyncio.gather(*([loss(i) for i in range(5)] + [fee(i) for i in range(5)]))
        ledger, lines = await _balances(sessions, cid)
        assert ledger.available == Decimal("0") and ledger.trading_reserved == Decimal("0")   # CHECK >= 0 held
        deficit = sum(l.debit for l in lines if l.account_code.startswith("ASSET:RECEIVABLE:CUSTOMER_DEFICIT"))
        assert deficit == Decimal(5 * 20 + 5 * 5) - Decimal("50")                              # 125 owed - 50 held = 75
        assert sum(l.debit for l in lines) == sum(l.credit for l in lines)
    finally:
        await engine.dispose()
