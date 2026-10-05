import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.customer_funds import ledger_invariant_report
from app.db import AuditLog, Base, CustomerLedgerAccount, LedgerJournal
from app.ledger_backfill import backfill_opening_balance, compute_bucket_drifts


@pytest.fixture(autouse=True)
def key(monkeypatch):
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


async def _seed(db, cid=7, available="50", trading="0", withdrawal="0"):
    db.add(CustomerLedgerAccount(customer_id=cid, currency="USDT", available=Decimal(available),
                                 trading_reserved=Decimal(trading), withdrawal_reserved=Decimal(withdrawal)))
    await db.commit()


def test_dry_run_is_default_and_writes_nothing():
    async def check(S):
        async with S() as db:
            await _seed(db)
            plan = await backfill_opening_balance(db, customer_id=7, expected_amount="50", operator="op", note="reviewed")
            assert plan["applied"] is False and plan["amount"] == "50.000000"
            assert (await db.execute(select(func.count()).select_from(LedgerJournal))).scalar_one() == 0
            assert len(await compute_bucket_drifts(db, 7)) == 1
    run(check)


def test_apply_fixes_invariant_audits_and_cannot_run_twice():
    async def check(S):
        async with S() as db:
            await _seed(db)
            assert (await ledger_invariant_report(db))["ok"] is False
        async with S() as db:
            res = await backfill_opening_balance(db, customer_id=7, expected_amount="50.0", operator="jonathan",
                                                 note="legacy wallet reviewed", apply=True)
            assert res["applied"] is True
        async with S() as db:
            assert (await ledger_invariant_report(db))["ok"] is True
            audit = (await db.execute(select(AuditLog.event, AuditLog.actor_id).where(
                AuditLog.event == "LEDGER_OPENING_BALANCE_BACKFILL"))).all()
            assert audit == [("LEDGER_OPENING_BALANCE_BACKFILL", "jonathan")]
            with pytest.raises(ValueError, match="no ledger drift"):
                await backfill_opening_balance(db, customer_id=7, expected_amount="50", operator="x", note="y", apply=True)
    run(check)


@pytest.mark.parametrize("kwargs,seed,match", [
    (dict(expected_amount="49", operator="op", note="n"), {}, "does not match"),
    (dict(expected_amount="50", operator="", note="n"), {}, "operator is required"),
    (dict(expected_amount="50", operator="op", note=""), {}, "note is required"),
    (dict(expected_amount="0", operator="op", note="n"), {}, "must be positive"),
    (dict(expected_amount="50", operator="op", note="n"), {"trading": "5"}, "reserved-bucket drift"),
])
def test_refuses_on_any_doubt(kwargs, seed, match):
    async def check(S):
        async with S() as db:
            await _seed(db, **seed)
            with pytest.raises(ValueError, match=match):
                await backfill_opening_balance(db, customer_id=7, apply=True, **kwargs)
            assert (await db.execute(select(func.count()).select_from(LedgerJournal))).scalar_one() == 0
    run(check)


def test_negative_drift_is_not_treated_as_an_opening_balance():
    async def check(S):
        async with S() as db:
            await _seed(db, available="50")
            await backfill_opening_balance(db, customer_id=7, expected_amount="50", operator="op", note="n", apply=True)
            row = (await db.execute(select(CustomerLedgerAccount).where(CustomerLedgerAccount.customer_id == 7))).scalar_one()
            row.available = Decimal("30")            # balance now below the journal => missing debit
            await db.commit()
            with pytest.raises(ValueError, match="not positive"):
                await backfill_opening_balance(db, customer_id=7, expected_amount="20", operator="op", note="n", apply=True)
    run(check)
