"""Regression for paper-order episodes that do not open a new position lifecycle."""
import asyncio
import uuid

import pytest
from sqlalchemy import select


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    from app.config import settings
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


def test_closing_paper_order_episode_is_not_replayed_as_position_entry():
    from app import execution
    from app.db import (
        init_db,
        SessionLocal,
        Trade,
        Position,
        TradeLearningEpisode,
    )

    suffix = uuid.uuid4().hex
    symbol = f"EPISODE-{suffix[:12]}"

    async def run():
        await init_db()
        async with SessionLocal() as db:
            entry = Trade(
                signal_id=f"episode-entry-{suffix}",
                client_order_id=f"episode-entry-{suffix}",
                exchange="binance",
                symbol=symbol,
                timeframe="1h",
                side="buy",
                quantity=1.0,
                requested_quantity=1.0,
                filled_quantity=1.0,
                remaining_quantity=0.0,
                requested_price=100.0,
                average_fill_price=100.0,
                fee=0.1,
                notional=100.0,
                mode="PAPER",
                status="FILLED",
                reason='{"strategy":"regression-test","asset":"crypto"}',
            )
            closing = Trade(
                signal_id=f"episode-close-{suffix}",
                client_order_id=f"episode-close-{suffix}",
                exchange="binance",
                symbol=symbol,
                timeframe="1h",
                side="sell",
                quantity=1.0,
                requested_quantity=1.0,
                filled_quantity=0.0,
                remaining_quantity=1.0,
                requested_price=99.0,
                average_fill_price=0.0,
                fee=0.1,
                notional=0.0,
                mode="PAPER",
                status="OPEN",
                reason='{"strategy":"regression-test","asset":"crypto"}',
            )
            db.add_all([entry, closing])
            await db.flush()
            db.add(Position(
                customer_id=None,
                trading_account_id=None,
                exchange="binance",
                symbol=symbol,
                quantity=1.0,
                average_entry_price=100.0,
                mark_price=100.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                reserved_capital=0.0,
                strategy="regression-test",
                entry_regime="UNKNOWN",
                entry_trade_id=entry.id,
            ))
            db.add_all([
                TradeLearningEpisode(
                    entry_trade_id=entry.id,
                    customer_id=None,
                    exchange="binance",
                    symbol=symbol,
                    timeframe="1h",
                    side="buy",
                    strategy="regression-test",
                    status="OPEN",
                    entry_quantity=1.0,
                    entry_price=100.0,
                ),
                TradeLearningEpisode(
                    entry_trade_id=closing.id,
                    customer_id=None,
                    exchange="binance",
                    symbol=symbol,
                    timeframe="1h",
                    side="sell",
                    strategy="regression-test",
                    status="OPEN",
                    entry_quantity=0.0,
                    entry_price=0.0,
                ),
            ])
            await db.commit()
            entry_id, closing_id = entry.id, closing.id

        async with SessionLocal() as db:
            closing = (await db.execute(
                select(Trade).where(Trade.id == closing_id)
            )).scalar_one()
            await execution._apply_fill_to_position(db, closing, 1.0, 99.0)
            await db.commit()

        async with SessionLocal() as db:
            episodes = (await db.execute(
                select(TradeLearningEpisode).where(
                    TradeLearningEpisode.entry_trade_id.in_([entry_id, closing_id])
                )
            )).scalars().all()
            by_entry = {row.entry_trade_id: row for row in episodes}
            assert by_entry[entry_id].status == "COMPLETED"
            assert by_entry[entry_id].closing_trade_id == closing_id
            assert by_entry[closing_id].status == "NOT_POSITION_ENTRY"
            position = (await db.execute(
                select(Position).where(Position.symbol == symbol)
            )).scalar_one()
            assert position.quantity == pytest.approx(0.0)

    asyncio.run(run())
