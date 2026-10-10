"""risk_gate must treat an order LARGER than the opposing position as new exposure, not as a reduce."""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.execution as execution
from app.config import settings
from app.db import AppState, Base, Position


def _run(side, quantity, stop_loss_price=None, position_qty=0.001):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                db.add(AppState(id=1, equity=10_000.0, cash_equity=10_000.0, peak_equity=10_000.0, daily_start_equity=10_000.0))
                db.add(Position(customer_id=None, exchange="paper", symbol="BTC/USDT", quantity=position_qty, mark_price=5000.0))
                await db.commit()
            old = execution.SessionLocal
            execution.SessionLocal = sessions
            try:
                return await execution.risk_gate("BTC/USDT", 5000.0, quantity, live=False, side=side, stop_loss_price=stop_loss_price)
            finally:
                execution.SessionLocal = old
        finally:
            await engine.dispose()
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _discipline(monkeypatch):
    monkeypatch.setattr(settings, "discipline_enabled", True)
    monkeypatch.setattr(settings, "discipline_enforce_risk_per_trade", True)
    monkeypatch.setattr(settings, "max_consecutive_losses", 0)


def test_true_reduce_without_stop_is_still_allowed():
    # closing/reducing the existing 0.001 long needs no protective stop
    _run("sell", 0.001)


def test_flip_far_larger_than_position_requires_protective_stop():
    # 1.0 BTC sell against a 0.001 BTC long flips to a ~0.999 BTC short: that is new exposure
    with pytest.raises(execution.RiskBlocked, match="protective stop"):
        _run("sell", 1.0)
