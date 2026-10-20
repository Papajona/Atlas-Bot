import asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.execution import _projected_exposure
from app.db import Base, Position


def test_platform_exposure_only_reads_platform_owned_positions(tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scope.db'}")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add_all([
                    Position(
                        customer_id=None, exchange="binance", symbol="BTC/USDT",
                        quantity=1.0, average_entry_price=100.0, mark_price=100.0,
                        unrealized_pnl=0.0,
                    ),
                    Position(
                        customer_id=7, exchange="binance", symbol="BTC/USDT",
                        quantity=100.0, average_entry_price=100.0, mark_price=100.0,
                        unrealized_pnl=0.0,
                    ),
                ])
                await db.commit()
            async with sessions() as db:
                platform_exposure = await _projected_exposure(
                    db, "BTC/USDT", 100.0, 1.0, "buy", None
                )
                customer_exposure = await _projected_exposure(
                    db, "BTC/USDT", 100.0, 1.0, "buy", 7
                )
                assert platform_exposure == 200.0
                assert customer_exposure == 10100.0
        finally:
            await engine.dispose()
    asyncio.run(run())
