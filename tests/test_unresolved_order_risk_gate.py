import asyncio
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import execution
from app.db import Base, Trade


def test_unresolved_live_order_blocks_new_entry_but_not_same_idempotent_retry(tmp_path, monkeypatch):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unresolved.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add(Trade(
                    customer_id=7, signal_id="signal-1", client_order_id="cid-1",
                    exchange="binance", symbol="BTC/USDT", side="buy",
                    quantity=Decimal("0.1"), requested_quantity=Decimal("0.1"),
                    remaining_quantity=Decimal("0.1"), mode="LIVE", status="UNKNOWN",
                ))
                await db.commit()
            async with sessions() as db:
                assert await execution._has_unresolved_live_orders(db, customer_id=7) is True
                assert await execution._has_unresolved_live_orders(
                    db, customer_id=7, signal_id="signal-1"
                ) is False
                assert await execution._has_unresolved_live_orders(db, customer_id=8) is False
                assert await execution._has_unresolved_live_orders(db, customer_id=None) is False
        finally:
            await engine.dispose()
    asyncio.run(run())
