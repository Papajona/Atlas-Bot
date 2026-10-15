"""Regression test for recovering a timed-out customer order from exchange history."""
import asyncio
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import execution
from app.config import settings
from app.db import Base, CustomerLedgerAccount, Trade


def test_unknown_order_resolves_from_order_history(monkeypatch):
    monkeypatch.setattr(settings, "customer_cash_only_trading", False)

    filled = {
        "id": "B9", "clientOrderId": "cid-timeout", "status": "closed",
        "filled": 0.1, "average": 60000, "price": 60000,
        "fee": {"currency": "USDT", "cost": 0},
    }

    class FakeBroker:
        def fetch_order(self, *args):
            return None

        def fetch_open_orders(self, *args):
            return []

        def fetch_orders(self, *args):
            return [filled]

    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            monkeypatch.setattr(execution, "SessionLocal", sessions)
            monkeypatch.setattr(execution, "build_customer_binance_broker", lambda *a, **k: FakeBroker())
            async with sessions() as db:
                db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("10000")))
                db.add(Trade(
                    customer_id=1, signal_id="s", client_order_id="cid-timeout",
                    exchange="binance", symbol="BTC/USDT", side="buy",
                    quantity=0.1, requested_quantity=0.1, remaining_quantity=0.1,
                    mode="LIVE", status="UNKNOWN",
                ))
                await db.commit()
            return await execution.reconcile_customer_live_orders()
        finally:
            await engine.dispose()

    out = asyncio.run(run())
    assert out["results"][0]["resolved"] is True
    assert out["results"][0]["status"] == "FILLED"
