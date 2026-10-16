"""End-to-end paper order through execute_signal: catches NameErrors and Decimal/float TypeErrors."""
import asyncio
import os
import uuid

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


def test_legacy_platform_paper_execution_fails_closed_without_bar_simulator():
    """Never record a perfect instant fill while the bar-driven simulator is unavailable."""
    from app import execution
    from app.db import init_db, SessionLocal, Trade, Position

    sym = f"BLOCKED/{uuid.uuid4().hex[:12]}"

    async def run():
        await init_db()
        with pytest.raises(execution.RiskBlocked, match="bar-driven paper simulator"):
            await execution.execute_signal(
                sym, "buy", 0.01, 50000.0, {"score": 0.9}, "binance", "1h",
                uuid.uuid4().hex, force_paper=True,
                stop_loss_price=49000.0, take_profit_price=52000.0,
            )
        async with SessionLocal() as db:
            trades = (await db.execute(select(Trade).where(Trade.symbol == sym))).scalars().all()
            positions = (await db.execute(select(Position).where(Position.symbol == sym))).scalars().all()
            assert trades == []
            assert positions == []

    asyncio.run(run())
