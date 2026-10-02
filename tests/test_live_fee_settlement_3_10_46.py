"""Live fills must charge the customer ledger the broker fee exactly once, as a delta of the cumulative order fee.

Authored without sqlalchemy/aiosqlite available locally -> run in CI. Uses real async SQLite like the repo's other ledger tests.
"""
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import execution
from app.config import settings
from app.db import Base, CustomerLedgerAccount, Incident, LedgerJournal, Trade


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


def _trade(**kw):
    base = dict(customer_id=1, signal_id="s1", client_order_id="c1", exchange="binance", symbol="BTC/USDT", side="buy",
                quantity=0.2, requested_quantity=0.2, mode="LIVE", status="PARTIAL")
    base.update(kw)
    return Trade(**base)


def test_partial_then_full_fill_charges_fee_delta_once_and_replay_is_noop():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1000")))
            t = _trade()
            db.add(t)
            await db.commit()
            # 1) partial fill: cumulative fee 3.0
            d1, c1, q1, n1 = execution._prepare_live_fee(t, {"fee": {"currency": "USDT", "cost": 3.0}}, 0.1, 60000)
            await execution._post_live_fee(db, t, d1, c1, q1, n1)
            # 2) full fill: cumulative fee 6.0 -> only 3.0 more
            d2, c2, q2, n2 = execution._prepare_live_fee(t, {"fee": {"currency": "USDT", "cost": 6.0}}, 0.2, 60000)
            await execution._post_live_fee(db, t, d2, c2, q2, n2)
            # 3) same snapshot replayed (reconcile loop) -> nothing more
            d3, c3, q3, n3 = execution._prepare_live_fee(t, {"fee": {"currency": "USDT", "cost": 6.0}}, 0.2, 60000)
            await execution._post_live_fee(db, t, d3, c3, q3, n3)
            await db.commit()
            assert (d1, d2, d3) == (pytest.approx(3.0), pytest.approx(3.0), 0.0)
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.available == Decimal("994")
            assert len((await db.execute(select(LedgerJournal))).scalars().all()) == 2
            assert t.fee == pytest.approx(6.0)
    asyncio.run(with_database(check))


def test_missing_fee_data_is_estimated_charged_and_flagged():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1000")))
            t = _trade()
            db.add(t)
            await db.commit()
            d, c, q, n = execution._prepare_live_fee(t, {}, 0.1, 60000)
            await execution._post_live_fee(db, t, d, c, q, n)
            await db.commit()
            assert d == pytest.approx(60000 * 0.1 * 5.5 / 10_000)
            flagged = (await db.execute(select(Incident).where(Incident.incident_key == f"LIVE_FEE_ESTIMATED:{t.id}"))).scalar_one()
            assert flagged.severity == "HIGH"
    asyncio.run(with_database(check))


def test_fee_never_decreases_automatically():
    async def check(sessions):
        async with sessions() as db:
            t = _trade(fee=5.0)
            d, c, _, _ = execution._prepare_live_fee(t, {"fee": {"currency": "USDT", "cost": 4.0}}, 0.1, 60000)
            assert d == 0.0 and c == 5.0 and t.fee == 5.0
    asyncio.run(with_database(check))
