"""Sandbox-drill scenarios replayed against a scripted fake broker through the app's reconcile path.

These reproduce the failure modes listed in RELEASE_AUDIT_3_10_45 gate 7 deterministically, so they can run in CI on every
commit. They do NOT replace scripts/exchange_sandbox_drill.py, which proves the real venue's behaviour.
Authored without sqlalchemy locally -> run in CI.
"""
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import execution
from app.config import settings
from app.db import Base, CustomerLedgerAccount, LedgerJournal, Trade


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")
    monkeypatch.setattr(settings, "customer_cash_only_trading", False)


async def with_database(check):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await check(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def _snapshot(status, filled, avg, fee_cost):
    return {"id": "B1", "status": status, "filled": filled, "average": avg, "price": avg,
            "fee": {"currency": "USDT", "cost": fee_cost}}


def test_timeout_then_partial_then_full_fill_via_reconcile_is_exact_and_idempotent():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("10000")))
            t = Trade(customer_id=1, signal_id="s", client_order_id="c", exchange="binance", symbol="BTC/USDT", side="buy",
                      quantity=0.2, requested_quantity=0.2, remaining_quantity=0.2, mode="LIVE", status="UNKNOWN")  # submit timed out
            db.add(t)
            await db.commit()
            # reconcile #1 finds a partial fill; reconcile #2 the full fill; reconcile #3 replays #2 (worker restart / duplicate poll)
            for snap in (_snapshot("open", 0.1, 60000, 3.3), _snapshot("closed", 0.2, 60010, 6.6), _snapshot("closed", 0.2, 60010, 6.6)):
                await execution._apply_broker_snapshot(db, t, snap)
                await db.commit()
            assert t.status == "FILLED" and t.filled_quantity == pytest.approx(0.2)
            assert t.fee == pytest.approx(6.6)
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.available == Decimal("10000") - Decimal("6.6")      # fee charged once in total
            fee_journals = [j for j in (await db.execute(select(LedgerJournal))).scalars() if j.entry_type == "TRADING_FEE"]
            assert len(fee_journals) == 2                                       # one per distinct cumulative fee, none for the replay
    asyncio.run(with_database(check))


def test_order_rejected_after_timeout_charges_nothing():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("100")))
            t = Trade(customer_id=1, signal_id="s2", client_order_id="c2", exchange="binance", symbol="BTC/USDT", side="buy",
                      quantity=0.2, requested_quantity=0.2, remaining_quantity=0.2, mode="LIVE", status="UNKNOWN")
            db.add(t)
            await db.commit()
            await execution._apply_broker_snapshot(db, t, {"id": "B2", "status": "rejected", "filled": 0, "average": 0, "price": 60000})
            await db.commit()
            assert t.status == "REJECTED" and (t.fee or 0) == 0
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.available == Decimal("100")
    asyncio.run(with_database(check))

def test_closed_without_fill_remains_unknown_and_does_not_create_fill():
    async def check(sessions):
        async with sessions() as db:
            t = Trade(customer_id=1, signal_id="s3", client_order_id="c3", exchange="binance", symbol="BTC/USDT",
                      side="buy", quantity=0.2, requested_quantity=0.2, remaining_quantity=0.2,
                      mode="LIVE", status="UNKNOWN")
            db.add(t)
            await db.commit()
            await execution._apply_broker_snapshot(
                db, t, {"id": "B3", "status": "closed", "filled": 0, "remaining": 0, "average": 60000, "price": 60000}
            )
            assert t.status == "UNKNOWN"
            assert t.filled_quantity == pytest.approx(0.0)
            assert t.remaining_quantity == pytest.approx(0.2)
    asyncio.run(with_database(check))


def test_broker_quantity_conservation_is_fail_closed():
    async def check(sessions):
        async with sessions() as db:
            t = Trade(customer_id=1, signal_id="s4", client_order_id="c4", exchange="binance", symbol="BTC/USDT",
                      side="buy", quantity=0.2, requested_quantity=0.2, remaining_quantity=0.2,
                      mode="LIVE", status="UNKNOWN")
            db.add(t)
            await db.commit()
            with pytest.raises(ValueError, match="quantity conservation"):
                await execution._apply_broker_snapshot(
                    db, t, {"id": "B4", "status": "open", "filled": 0.1, "remaining": 0.2, "average": 60000}
                )
    asyncio.run(with_database(check))


def test_expired_partial_order_is_terminal_and_preserves_actual_fill_and_remaining():
    async def check(sessions):
        async with sessions() as db:
            t = Trade(customer_id=1, signal_id="s5", client_order_id="c5", exchange="binance", symbol="BTC/USDT",
                      side="buy", quantity=0.2, requested_quantity=0.2, remaining_quantity=0.2,
                      mode="LIVE", status="UNKNOWN")
            db.add(t)
            await db.commit()
            await execution._apply_broker_snapshot(
                db, t, {"id": "B5", "status": "expired", "filled": 0.1, "remaining": 0.1, "average": 60000}
            )
            assert t.status == "CANCELED"
            assert t.filled_quantity == pytest.approx(0.1)
            assert t.remaining_quantity == pytest.approx(0.1)
    asyncio.run(with_database(check))
