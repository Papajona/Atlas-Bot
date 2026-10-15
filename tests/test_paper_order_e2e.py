"""End-to-end paper order through execute_signal: catches NameErrors and Decimal/float TypeErrors."""
import asyncio
import os
import uuid

import numpy as np
import pandas as pd

os.environ.setdefault("ENVIRONMENT", "development")

import pytest
from sqlalchemy import select


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.config import settings
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")
    monkeypatch.setattr(settings, "discipline_entry_cooldown_seconds", 0)
    monkeypatch.setattr(settings, "paper_execution_rejection_rate", 0.0)
    monkeypatch.setattr(settings, "paper_execution_latency_min_ms", 0)
    monkeypatch.setattr(settings, "paper_execution_latency_max_ms", 0)


def test_paper_buy_then_sell_round_trip():
    from app import execution
    from app.db import init_db, SessionLocal, Trade, Position, AuditLog
    from app.paper_execution import build_paper_market_context

    sym = f"BTC/USDT-{uuid.uuid4().hex[:8]}"
    idx = pd.date_range("2026-01-01", periods=25, freq="h", tz="UTC")
    prices = np.full(25, 50000.0)
    candles = pd.DataFrame({
        "open": prices, "high": prices + 20.0, "low": prices - 20.0,
        "close": prices, "volume": np.full(25, 100.0),
    }, index=idx)
    context = build_paper_market_context(candles, symbol=sym, timeframe="1h")

    async def run():
        await init_db()
        buy = await execution.execute_signal(
            sym, "buy", 0.01, 50000.0,
            {"score": 0.9, "paper_market_context": context}, "binance", "1h",
            (idx[-1] + pd.Timedelta(seconds=1)).isoformat(), force_paper=True,
            stop_loss_price=49000.0, take_profit_price=52000.0,
        )
        assert buy["status"] == "SIMULATED", buy
        assert buy["filled"] == pytest.approx(0.01)
        sell = await execution.execute_signal(
            sym, "sell", 0.01, 50000.0,
            {"score": 0.9, "paper_market_context": context}, "binance", "1h",
            (idx[-1] + pd.Timedelta(seconds=2)).isoformat(), force_paper=True,
            stop_loss_price=51500.0, take_profit_price=49000.0,
        )
        assert sell["status"] == "SIMULATED", sell
        assert sell["filled"] == pytest.approx(0.01)
        async with SessionLocal() as db:
            trades = (await db.execute(select(Trade).where(Trade.symbol == sym, Trade.client_order_id.like("ait-%")))).scalars().all()
            assert len(trades) >= 2
            assert all(isinstance(t.filled_quantity, float) and isinstance(t.requested_price, float) for t in trades)
            paper_positions = (await db.execute(select(Position).where(
                Position.symbol == sym, Position.customer_id.is_(None)
            ))).scalars().all()
            assert len(paper_positions) == 1
            assert paper_positions[0].quantity == pytest.approx(0.0)
            audits = (await db.execute(select(AuditLog).where(AuditLog.event == "PAPER_ORDER"))).scalars().all()
            assert len(audits) >= 2

    asyncio.run(run())
