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
            assert by_entry[reopen_id].exit_price == pytest.approx(0.0)

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


def test_partial_closes_allocate_only_current_fill_and_remaining_entry_fees():
    from decimal import Decimal
    from app import execution
    from app.db import (
        init_db, SessionLocal, Trade, Position, StrategyOutcome,
    )

    suffix = uuid.uuid4().hex
    symbol = f"FEE-BASIS-{suffix[:10]}"

    async def run():
        await init_db()
        async with SessionLocal() as db:
            entry = Trade(
                signal_id=f"fee-entry-{suffix}", client_order_id=f"fee-entry-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="buy",
                quantity=2.0, requested_quantity=2.0, filled_quantity=2.0,
                remaining_quantity=0.0, requested_price=100.0,
                average_fill_price=100.0, fee=2.0, notional=200.0,
                mode="PAPER", status="FILLED",
                reason='{"strategy":"fee-regression","asset":"crypto"}',
            )
            first_close = Trade(
                signal_id=f"fee-close-1-{suffix}", client_order_id=f"fee-close-1-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="sell",
                quantity=1.0, requested_quantity=1.0, filled_quantity=0.0,
                remaining_quantity=1.0, requested_price=110.0,
                average_fill_price=0.0, fee=1.5, notional=0.0,
                mode="PAPER", status="OPEN",
                reason='{"strategy":"fee-regression","asset":"crypto"}',
            )
            second_close = Trade(
                signal_id=f"fee-close-2-{suffix}", client_order_id=f"fee-close-2-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="sell",
                quantity=1.0, requested_quantity=1.0, filled_quantity=0.0,
                remaining_quantity=1.0, requested_price=110.0,
                average_fill_price=0.0, fee=0.25, notional=0.0,
                mode="PAPER", status="OPEN",
                reason='{"strategy":"fee-regression","asset":"crypto"}',
            )
            db.add_all([entry, first_close, second_close])
            await db.flush()
            db.add(Position(
                customer_id=None, trading_account_id=None, exchange="binance",
                symbol=symbol, quantity=2.0, average_entry_price=100.0,
                mark_price=100.0, realized_pnl=0.0, unrealized_pnl=0.0,
                reserved_capital=0.0, entry_fee=Decimal("2.00"),
                    entry_fee_basis_known=True,
                strategy="fee-regression", entry_regime="UNKNOWN",
                entry_trade_id=entry.id,
            ))
            await db.commit()
            first_id, second_id = first_close.id, second_close.id

        async with SessionLocal() as db:
            close = (await db.execute(
                select(Trade).where(Trade.id == first_id)
            )).scalar_one()
            await execution._apply_fill_to_position(
                db, close, 1.0, 110.0, current_fill_fee=Decimal("0.50"),
                current_fill_fee_verified=True
            )
            await db.commit()

        async with SessionLocal() as db:
            position = (await db.execute(
                select(Position).where(Position.symbol == symbol)
            )).scalar_one()
            assert position.quantity == pytest.approx(1.0)
            assert Decimal(str(position.entry_fee)) == Decimal("1.00")
            first_outcome = (await db.execute(
                select(StrategyOutcome).where(StrategyOutcome.closing_trade_id == first_id)
            )).scalar_one()
            assert Decimal(str(first_outcome.fee)) == Decimal("1.50")
            assert Decimal(str(first_outcome.net_pnl)) == Decimal("8.50")
            assert first_outcome.lifecycle_status == "OPEN"

        async with SessionLocal() as db:
            close = (await db.execute(
                select(Trade).where(Trade.id == second_id)
            )).scalar_one()
            await execution._apply_fill_to_position(
                db, close, 1.0, 110.0, current_fill_fee=Decimal("0.25"),
                current_fill_fee_verified=True
            )
            await db.commit()

        async with SessionLocal() as db:
            outcomes = (await db.execute(
                select(StrategyOutcome)
                .where(StrategyOutcome.trade_id == entry.id)
                .order_by(StrategyOutcome.sequence)
            )).scalars().all()
            assert len(outcomes) == 2
            assert outcomes[0].lifecycle_status == "PARTIAL_CLOSED"
            assert outcomes[1].lifecycle_status == "CLOSED"
            assert Decimal(str(outcomes[1].closing_quantity)) == Decimal("2")
            assert Decimal(str(outcomes[1].closing_notional)) == Decimal("220")
            assert Decimal(str(outcomes[1].fee)) == Decimal("2.75")
            assert Decimal(str(outcomes[1].net_pnl)) == Decimal("17.25")
            assert outcomes[1].net_return_bps == pytest.approx(17.25 / 220 * 10000)

    asyncio.run(run())


def test_single_fill_reversal_episode_records_only_new_opposite_leg():
    from app import execution
    from app.db import (
        init_db, SessionLocal, Trade, Position, TradeLearningEpisode, StrategyOutcome,
    )

    suffix = uuid.uuid4().hex
    symbol = f"EPISODE-SINGLE-FLIP-{suffix[:10]}"

    async def run():
        await init_db()
        async with SessionLocal() as db:
            entry = Trade(
                signal_id=f"single-flip-old-entry-{suffix}",
                client_order_id=f"single-flip-old-entry-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="buy",
                quantity=1.0, requested_quantity=1.0, filled_quantity=1.0,
                remaining_quantity=0.0, requested_price=100.0,
                average_fill_price=100.0, fee=0.1, notional=100.0,
                mode="PAPER", status="FILLED",
                reason='{"strategy":"old-strategy","regime":"OLD"}',
            )
            reversal = Trade(
                signal_id=f"single-flip-reversal-{suffix}",
                client_order_id=f"single-flip-reversal-{suffix}",
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
            await execution._apply_fill_to_position(db, order, 2.0, 99.0)
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
            outcome = (await db.execute(
                select(StrategyOutcome).where(
                    StrategyOutcome.trade_id == old_entry_id,
                    StrategyOutcome.closing_trade_id == reversal_id,
                )
            )).scalar_one()

            assert position.quantity == pytest.approx(-1.0)
            assert position.entry_trade_id == reversal_id
            assert by_entry[old_entry_id].status == "COMPLETED"
            assert by_entry[reversal_id].status == "OPEN"
            assert by_entry[reversal_id].entry_quantity == pytest.approx(1.0)
            assert outcome.lifecycle_status == "CLOSED"
            assert outcome.closing_quantity == pytest.approx(1.0)

    asyncio.run(run())

@pytest.mark.parametrize(
    "entry_basis_known,close_fee_verified,expected_status",
    [
        (True, True, "COMPLETED"),
        (False, False, "FEE_RECONCILIATION_PENDING"),
    ],
)
def test_breakeven_close_records_fees_and_gates_learning_replay(
    entry_basis_known, close_fee_verified, expected_status
):
    from decimal import Decimal
    from app import execution
    from app.db import (
        init_db, SessionLocal, Trade, Position, TradeLearningEpisode,
        StrategyOutcome,
    )

    suffix = uuid.uuid4().hex
    symbol = f"EPISODE-BREAKEVEN-{suffix[:10]}"

    async def run():
        await init_db()
        async with SessionLocal() as db:
            entry = Trade(
                signal_id=f"breakeven-entry-{suffix}",
                client_order_id=f"breakeven-entry-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="buy",
                quantity=1.0, requested_quantity=1.0, filled_quantity=1.0,
                remaining_quantity=0.0, requested_price=100.0,
                average_fill_price=100.0, fee=0.10, notional=100.0,
                mode="PAPER", status="FILLED",
                reason='{"strategy":"breakeven-regression","asset":"crypto"}',
            )
            closing = Trade(
                signal_id=f"breakeven-close-{suffix}",
                client_order_id=f"breakeven-close-{suffix}",
                exchange="binance", symbol=symbol, timeframe="1h", side="sell",
                quantity=1.0, requested_quantity=1.0, filled_quantity=0.0,
                remaining_quantity=1.0, requested_price=100.0,
                average_fill_price=0.0, fee=0.25, notional=0.0,
                mode="PAPER", status="OPEN",
                reason='{"strategy":"breakeven-regression","asset":"crypto"}',
            )
            db.add_all([entry, closing])
            await db.flush()
            db.add(Position(
                customer_id=None, trading_account_id=None, exchange="binance",
                symbol=symbol, quantity=1.0, average_entry_price=100.0,
                mark_price=100.0, realized_pnl=0.0, unrealized_pnl=0.0,
                reserved_capital=0.0, entry_fee=Decimal("0.10"),
                entry_fee_basis_known=entry_basis_known, strategy="breakeven-regression",
                entry_regime="UNKNOWN", entry_trade_id=entry.id,
            ))
            db.add(TradeLearningEpisode(
                entry_trade_id=entry.id, customer_id=None, exchange="binance",
                symbol=symbol, timeframe="1h", side="buy",
                strategy="breakeven-regression", status="OPEN",
                entry_quantity=1.0, entry_price=100.0,
                gross_pnl=Decimal("0"), total_fees=Decimal("0.10"),
                net_pnl=Decimal("-0.10"),
            ))
            await db.commit()
            entry_id, close_id = entry.id, closing.id

        async with SessionLocal() as db:
            close = (await db.execute(
                select(Trade).where(Trade.id == close_id)
            )).scalar_one()
            await execution._apply_fill_to_position(
                db, close, 1.0, 100.0,
                current_fill_fee=Decimal("0.25"),
                current_fill_fee_verified=close_fee_verified,
            )
            await db.commit()

        async with SessionLocal() as db:
            episode = (await db.execute(
                select(TradeLearningEpisode).where(
                    TradeLearningEpisode.entry_trade_id == entry_id
                )
            )).scalar_one()
            outcome = (await db.execute(
                select(StrategyOutcome).where(
                    StrategyOutcome.trade_id == entry_id,
                    StrategyOutcome.closing_trade_id == close_id,
                )
            )).scalar_one()

            assert episode.status == expected_status
            assert episode.gross_pnl == pytest.approx(0.0)
            assert Decimal(str(episode.total_fees)) == Decimal("0.35")
            if expected_status == "COMPLETED":
                assert Decimal(str(episode.net_pnl)) == Decimal("-0.35")
                assert outcome.fee_basis_complete is True
                assert Decimal(str(outcome.fee)) == Decimal("0.35")
                assert Decimal(str(outcome.net_pnl)) == Decimal("-0.35")
            else:
                assert outcome.fee_basis_complete is False
                assert outcome.net_pnl is None
                assert outcome.net_return_bps is None

    asyncio.run(run())

