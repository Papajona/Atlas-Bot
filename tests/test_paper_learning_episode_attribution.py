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


def test_reopening_flat_position_starts_a_new_learning_episode():
    from app import execution
    from app.db import init_db, SessionLocal, Trade, Position, TradeLearningEpisode

    suffix = uuid.uuid4().hex
    symbol = f"EPISODE-REOPEN-{suffix[:10]}"

    async def run():
        await init_db()
        async with SessionLocal() as db:
            entry = Trade(
                signal_id=f"reopen-old-entry-{suffix}",
                client_order_id=f"reopen-old-entry-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="buy",
                quantity=1.0, requested_quantity=1.0, filled_quantity=1.0,
                remaining_quantity=0.0, requested_price=100.0,
                average_fill_price=100.0, fee=0.1, notional=100.0,
                mode="PAPER", status="FILLED",
                reason='{"strategy":"old-strategy","regime":"OLD"}',
            )
            closing = Trade(
                signal_id=f"reopen-close-{suffix}",
                client_order_id=f"reopen-close-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="sell",
                quantity=1.0, requested_quantity=1.0, filled_quantity=0.0,
                remaining_quantity=1.0, requested_price=99.0,
                average_fill_price=0.0, fee=0.1, notional=0.0,
                mode="PAPER", status="OPEN",
                reason='{"strategy":"close-strategy","regime":"CLOSE"}',
            )
            reopening = Trade(
                signal_id=f"reopen-new-entry-{suffix}",
                client_order_id=f"reopen-new-entry-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="buy",
                quantity=1.0, requested_quantity=1.0, filled_quantity=0.0,
                remaining_quantity=1.0, requested_price=101.0,
                average_fill_price=0.0, fee=0.1, notional=0.0,
                mode="PAPER", status="OPEN",
                reason='{"strategy":"new-strategy","regime":"NEW"}',
            )
            db.add_all([entry, closing, reopening])
            await db.flush()
            db.add(Position(
                customer_id=None, trading_account_id=None, exchange="binance",
                symbol=symbol, quantity=1.0, average_entry_price=100.0,
                mark_price=100.0, realized_pnl=0.0, unrealized_pnl=0.0,
                reserved_capital=0.0, strategy="old-strategy",
                entry_regime="OLD", entry_trade_id=entry.id,
            ))
            db.add_all([
                TradeLearningEpisode(
                    entry_trade_id=entry.id, customer_id=None, exchange="binance",
                    symbol=symbol, timeframe="1h", side="buy",
                    strategy="old-strategy", status="OPEN",
                    entry_quantity=1.0, entry_price=100.0,
                ),
                TradeLearningEpisode(
                    entry_trade_id=closing.id, customer_id=None, exchange="binance",
                    symbol=symbol, timeframe="1h", side="sell",
                    strategy="close-strategy", status="OPEN",
                    entry_quantity=0.0, entry_price=0.0,
                ),
                TradeLearningEpisode(
                    entry_trade_id=reopening.id, customer_id=None, exchange="binance",
                    symbol=symbol, timeframe="1h", side="buy",
                    strategy="new-strategy", status="OPEN",
                    entry_quantity=0.0, entry_price=0.0,
                ),
            ])
            await db.commit()
            old_entry_id, close_id, reopen_id = entry.id, closing.id, reopening.id

        async with SessionLocal() as db:
            close_trade = (await db.execute(
                select(Trade).where(Trade.id == close_id)
            )).scalar_one()
            await execution._apply_fill_to_position(db, close_trade, 1.0, 99.0)
            await db.commit()

        async with SessionLocal() as db:
            new_trade = (await db.execute(
                select(Trade).where(Trade.id == reopen_id)
            )).scalar_one()
            await execution._apply_fill_to_position(db, new_trade, 1.0, 101.0)
            await db.commit()

        async with SessionLocal() as db:
            position = (await db.execute(
                select(Position).where(Position.symbol == symbol)
            )).scalar_one()
            episodes = (await db.execute(
                select(TradeLearningEpisode).where(
                    TradeLearningEpisode.entry_trade_id.in_([old_entry_id, close_id, reopen_id])
                )
            )).scalars().all()
            by_entry = {row.entry_trade_id: row for row in episodes}
            assert position.quantity == pytest.approx(1.0)
            assert position.entry_trade_id == reopen_id
            assert position.strategy == "new-strategy"
            assert position.entry_regime == "NEW"
            assert by_entry[old_entry_id].status == "COMPLETED"
            assert by_entry[close_id].status == "NOT_POSITION_ENTRY"
            assert by_entry[reopen_id].status == "OPEN"
            assert by_entry[reopen_id].entry_quantity == pytest.approx(1.0)
            assert by_entry[reopen_id].entry_price == pytest.approx(101.0)

    asyncio.run(run())


def test_partial_reversal_order_reopens_learning_episode_after_flattening_old_position():
    from app import execution
    from app.db import init_db, SessionLocal, Trade, Position, TradeLearningEpisode

    suffix = uuid.uuid4().hex
    symbol = f"EPISODE-PARTIAL-FLIP-{suffix[:10]}"

    async def run():
        await init_db()
        async with SessionLocal() as db:
            entry = Trade(
                signal_id=f"partial-flip-old-entry-{suffix}",
                client_order_id=f"partial-flip-old-entry-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="buy",
                quantity=1.0, requested_quantity=1.0, filled_quantity=1.0,
                remaining_quantity=0.0, requested_price=100.0,
                average_fill_price=100.0, fee=0.1, notional=100.0,
                mode="PAPER", status="FILLED",
                reason='{"strategy":"old-strategy","regime":"OLD"}',
            )
            reversal = Trade(
                signal_id=f"partial-flip-reversal-{suffix}",
                client_order_id=f"partial-flip-reversal-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="sell",
                quantity=2.0, requested_quantity=2.0, filled_quantity=0.0,
                remaining_quantity=2.0, requested_price=99.0,
                average_fill_price=0.0, fee=0.0, notional=0.0,
                mode="PAPER", status="OPEN",
                reason='{"strategy":"reversal-strategy","regime":"NEW_DOWN"}',
            )
            db.add_all([entry, reversal])
            await db.flush()
            db.add(Position(
                customer_id=None, trading_account_id=None, exchange="binance",
                symbol=symbol, quantity=1.0, average_entry_price=100.0,
                mark_price=100.0, realized_pnl=0.0, unrealized_pnl=0.0,
                reserved_capital=0.0, strategy="old-strategy",
                entry_regime="OLD", entry_trade_id=entry.id,
            ))
            db.add_all([
                TradeLearningEpisode(
                    entry_trade_id=entry.id, customer_id=None, exchange="binance",
                    symbol=symbol, timeframe="1h", side="buy",
                    strategy="old-strategy", status="OPEN",
                    entry_quantity=1.0, entry_price=100.0,
                ),
                TradeLearningEpisode(
                    entry_trade_id=reversal.id, customer_id=None, exchange="binance",
                    symbol=symbol, timeframe="1h", side="sell",
                    strategy="reversal-strategy", status="OPEN",
                    entry_quantity=0.0, entry_price=0.0,
                ),
            ])
            await db.commit()
            old_entry_id, reversal_id = entry.id, reversal.id

        async with SessionLocal() as db:
            order = (await db.execute(
                select(Trade).where(Trade.id == reversal_id)
            )).scalar_one()
            await execution._apply_fill_to_position(db, order, 1.0, 99.0)
            await db.commit()

        async with SessionLocal() as db:
            order = (await db.execute(
                select(Trade).where(Trade.id == reversal_id)
            )).scalar_one()
            assert order.filled_quantity == pytest.approx(1.0)
            await execution._apply_fill_to_position(db, order, 2.0, 98.0)
            await db.commit()

        async with SessionLocal() as db:
            position = (await db.execute(
                select(Position).where(Position.symbol == symbol)
            )).scalar_one()
            episodes = (await db.execute(
                select(TradeLearningEpisode).where(
                    TradeLearningEpisode.entry_trade_id.in_([old_entry_id, reversal_id])
                )
            )).scalars().all()
            by_entry = {row.entry_trade_id: row for row in episodes}
            reopened = by_entry[reversal_id]
            assert position.quantity == pytest.approx(-1.0)
            assert position.entry_trade_id == reversal_id
            assert position.strategy == "reversal-strategy"
            assert position.entry_regime == "NEW_DOWN"
            assert by_entry[old_entry_id].status == "COMPLETED"
            assert reopened.status == "OPEN"
            assert reopened.entry_quantity == pytest.approx(1.0)
            assert reopened.entry_price == pytest.approx(98.0)
            assert reopened.exit_at is None
            assert reopened.closing_trade_id is None

    asyncio.run(run())
