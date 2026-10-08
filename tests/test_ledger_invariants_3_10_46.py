"""ledger_invariant_report must pass on honest books and catch both failure classes. Run in CI (needs sqlalchemy/aiosqlite)."""
import asyncio
from decimal import Decimal

import pytest
from pathlib import Path
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.customer_funds import ledger_invariant_report, reserve_withdrawal, settle_realized_pnl, settle_trading_fee
from app.db import Base, CustomerLedgerAccount, LedgerJournal


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


def test_invariants_hold_after_normal_and_deficit_activity_then_detect_tampering():
    async def check(sessions):
        async with sessions() as db:
            # Fund through a journal (credit AVAILABLE) so balance and journals agree from the start.
            from app.customer_funds import post_journal, _customer_account, USDT, get_or_create_ledger
            ledger = await get_or_create_ledger(db, 1, USDT)
            await post_journal(db, currency=USDT, entry_type="DEPOSIT", reference_type="DEPOSIT", reference_id="d1", idempotency_key="dep:1",
                               lines=[{"account_code": "ASSET:CUSTODY:USDT", "debit": 100, "credit": 0},
                                      {"account_code": _customer_account(1, "AVAILABLE"), "customer_id": 1, "debit": 0, "credit": 100}],
                               description="seed")
            ledger.available = Decimal("100")
            await db.commit()
            await reserve_withdrawal(db, 1, 30, reference_id="w1")
            await settle_trading_fee(db, customer_id=1, fee=2, reference_id="f1", strict=False)
            await settle_realized_pnl(db, customer_id=1, amount=-500, reference_id="p1", strict=False)   # forces a booked deficit
            await db.commit()
            assert (await ledger_invariant_report(db))["ok"] is True
            # Tamper: change a balance with no journal -> must be reported as drift.
            row = (await db.execute(__import__("sqlalchemy").select(CustomerLedgerAccount))).scalar_one()
            row.available = Decimal(str(row.available)) + Decimal("5")
            await db.commit()
            bad = await ledger_invariant_report(db)
            assert bad["ok"] is False and bad["drift_count"] >= 1
    asyncio.run(with_database(check))


def test_settle_withdrawal_rechecks_idempotency_after_customer_ledger_lock():
    source = Path("app/customer_funds.py").read_text()
    start = source.index("async def settle_withdrawal(")
    end = source.index("async def customer_balance(", start)
    block = source[start:end]
    lock_pos = block.index("with_for_update")
    recheck_pos = block.index("pre-lock idempotency check is only a fast path")
    assert recheck_pos > lock_pos
    assert 'LedgerJournal.idempotency_key == idem' in block
    assert 'return fresh_ledger' in block[recheck_pos:]
