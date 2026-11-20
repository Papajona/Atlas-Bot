"""Live fills must charge the customer ledger the broker fee exactly once, as a delta of the cumulative order fee.

Authored without sqlalchemy/aiosqlite available locally -> run in CI. Uses real async SQLite like the repo's other ledger tests.
"""
import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import execution
from app.config import settings
from app.db import Base, CustomerLedgerAccount, Incident, LedgerJournal, Position, StrategyOutcome, Trade, TradeLearningEpisode
from app.live_fees import fee_quality_is_verified, UNVERIFIED


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(settings, "app_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "app_encryption_keys_json", "")
    monkeypatch.setattr(settings, "app_encryption_active_key_id", "v1")


async def with_database(check):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await check(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def _trade(**kw):
    base = dict(customer_id=1, signal_id="s1", client_order_id="c1", exchange="binance", symbol="BTC/USDT", side="buy",
                quantity=0.2, requested_quantity=0.2, mode="LIVE", status="PARTIAL")
    base.update(kw)
    return Trade(**base)


def test_partial_then_full_fill_charges_fee_delta_once_and_replay_is_noop():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1000")))
            t = _trade()
            db.add(t)
            await db.commit()
            # 1) partial fill: cumulative fee 3.0
            d1, c1, q1, n1 = execution._prepare_live_fee(t, {"fee": {"currency": "USDT", "cost": 3.0}}, 0.1, 60000)
            await execution._post_live_fee(db, t, d1, c1, q1, n1)
            # 2) full fill: cumulative fee 6.0 -> only 3.0 more
            d2, c2, q2, n2 = execution._prepare_live_fee(t, {"fee": {"currency": "USDT", "cost": 6.0}}, 0.2, 60000)
            await execution._post_live_fee(db, t, d2, c2, q2, n2)
            # 3) same snapshot replayed (reconcile loop) -> nothing more
            d3, c3, q3, n3 = execution._prepare_live_fee(t, {"fee": {"currency": "USDT", "cost": 6.0}}, 0.2, 60000)
            await execution._post_live_fee(db, t, d3, c3, q3, n3)
            await db.commit()
            assert (d1, d2, d3) == (pytest.approx(3.0), pytest.approx(3.0), 0.0)
            ledger = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert ledger.available == Decimal("994")
            assert len((await db.execute(select(LedgerJournal))).scalars().all()) == 2
            assert t.fee == pytest.approx(6.0)
    asyncio.run(with_database(check))


def test_missing_fee_data_is_estimated_but_withheld_until_authoritative_true_up():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1000")))
            t = _trade()
            db.add(t)
            await db.flush()
            estimated, cumulative, quality, note = execution._prepare_live_fee(
                t, {}, 0.1, 60000
            )
            assert estimated == Decimal("0")
            assert cumulative == Decimal("0")
            assert quality == "estimated"
            assert t.fee_reconciliation_pending is True
            await execution._post_live_fee(
                db, t, estimated, cumulative, quality, note
            )
            await db.commit()

            balance = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert balance.available == Decimal("1000")
            assert not (await db.execute(select(LedgerJournal))).scalars().all()
            flagged = (await db.execute(
                select(Incident).where(
                    Incident.incident_key == f"LIVE_FEE_ESTIMATED:{t.id}"
                )
            )).scalar_one()
            assert flagged.severity == "HIGH"

            exact, final_total, final_quality, final_note = execution._prepare_live_fee(
                t,
                {"fee": {"currency": "USDT", "cost": "3.30"}},
                0.1,
                60000,
            )
            assert exact == Decimal("3.300000")
            assert final_total == Decimal("3.300000")
            assert final_quality == "exact"
            assert t.fee_reconciliation_pending is False
            await execution._post_live_fee(
                db, t, exact, final_total, final_quality, final_note
            )
            await db.commit()

            balance = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert balance.available == Decimal("996.700000")
            assert len((await db.execute(select(LedgerJournal))).scalars().all()) == 1
            flagged = (await db.execute(
                select(Incident).where(
                    Incident.incident_key == f"LIVE_FEE_ESTIMATED:{t.id}"
                )
            )).scalar_one()
            assert flagged.status == "RESOLVED"
    asyncio.run(with_database(check))


def test_fee_never_decreases_automatically():
    async def check(sessions):
        async with sessions():
            t = _trade(fee=5.0)
            d, c, _, _ = execution._prepare_live_fee(t, {"fee": {"currency": "USDT", "cost": 4.0}}, 0.1, 60000)
            assert d == 0.0 and c == 5.0 and t.fee == 5.0
    asyncio.run(with_database(check))



def test_fee_without_currency_cannot_create_fee_complete_outcome():
    async def check(sessions):
        async with sessions() as db:
            entry = _trade(
                signal_id="fee-denomination-entry",
                client_order_id="fee-denomination-entry",
                customer_id=None,
                side="buy",
                fee=0.0,
            )
            db.add(entry)
            await db.flush()
            entry_delta, _entry_cumulative, entry_quality, _entry_note = execution._prepare_live_fee(
                entry,
                {"fee": {"currency": "USDT", "cost": "0.10"}},
                0.2,
                100.0,
            )
            await execution._apply_fill_to_position(
                db,
                entry,
                0.2,
                100.0,
                current_fill_fee=entry_delta,
                current_fill_fee_verified=fee_quality_is_verified(entry_quality),
            )
            await db.flush()

            closing = _trade(
                signal_id="fee-denomination-close",
                client_order_id="fee-denomination-close",
                customer_id=None,
                side="sell",
                fee=0.0,
            )
            db.add(closing)
            await db.flush()
            close_delta, _close_cumulative, close_quality, close_note = execution._prepare_live_fee(
                closing,
                {"fee": {"cost": "0.25"}},
                0.2,
                110.0,
            )
            assert close_quality == UNVERIFIED
            assert close_delta == Decimal("0")
            assert not fee_quality_is_verified(close_quality)
            assert "customer charge withheld" in close_note
            await execution._apply_fill_to_position(
                db,
                closing,
                0.2,
                110.0,
                current_fill_fee=close_delta,
                current_fill_fee_verified=fee_quality_is_verified(close_quality),
            )
            await db.commit()

            outcome = (await db.execute(
                select(StrategyOutcome).where(
                    StrategyOutcome.closing_trade_id == closing.id
                )
            )).scalar_one()
            assert outcome.lifecycle_status == "CLOSED"
            assert outcome.fee_basis_complete is False

    asyncio.run(with_database(check))



def test_unknown_fee_currency_is_withheld_then_authoritative_snapshot_settles_exact_delta():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1000")))
            trade = _trade()
            db.add(trade)
            await db.flush()

            withheld, cumulative, quality, note = execution._prepare_live_fee(
                trade, {"fee": {"cost": "0.25"}}, 0.1, 60000
            )
            assert withheld == Decimal("0")
            assert cumulative == Decimal("0")
            assert quality == UNVERIFIED
            await execution._post_live_fee(db, trade, withheld, cumulative, quality, note)
            await db.commit()

            balance = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert balance.available == Decimal("1000")
            incident = (await db.execute(
                select(Incident).where(
                    Incident.incident_key == f"LIVE_FEE_UNVERIFIED:{trade.id}"
                )
            )).scalar_one()
            assert incident.severity == "HIGH"

            corrected, final_total, final_quality, final_note = execution._prepare_live_fee(
                trade, {"fee": {"currency": "USDT", "cost": "0.25"}}, 0.1, 60000
            )
            assert corrected == Decimal("0.25")
            assert final_total == Decimal("0.25")
            assert final_quality == "exact"
            await execution._post_live_fee(
                db, trade, corrected, final_total, final_quality, final_note
            )
            await db.commit()

            balance = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert balance.available == Decimal("999.75")
            assert trade.fee_reconciliation_pending is False
            incident = (await db.execute(
                select(Incident).where(
                    Incident.incident_key == f"LIVE_FEE_UNVERIFIED:{trade.id}"
                )
            )).scalar_one()
            assert incident.status == "RESOLVED"

    asyncio.run(with_database(check))


def test_zero_fill_snapshot_does_not_clear_pending_fee_or_close_incident():
    async def check(sessions):
        async with sessions() as db:
            db.add(CustomerLedgerAccount(customer_id=1, available=Decimal("1000")))
            trade = _trade()
            db.add(trade)
            await db.flush()
            withheld, cumulative, quality, note = execution._prepare_live_fee(
                trade, {"fee": {"cost": "0.25"}}, 0.1, 60000
            )
            assert quality == UNVERIFIED
            assert trade.fee_reconciliation_pending is True
            await execution._post_live_fee(
                db, trade, withheld, cumulative, quality, note
            )
            await db.commit()

            zero_delta, zero_total, zero_quality, zero_note = execution._prepare_live_fee(
                trade,
                {"fee": {"currency": "USDT", "cost": "0"}},
                0,
                60000,
            )
            assert zero_delta == Decimal("0")
            assert zero_quality == UNVERIFIED
            assert trade.fee_reconciliation_pending is True
            await execution._post_live_fee(
                db, trade, zero_delta, zero_total, zero_quality, zero_note
            )
            await db.commit()
            incident = (await db.execute(
                select(Incident).where(
                    Incident.incident_key == f"LIVE_FEE_UNVERIFIED:{trade.id}"
                )
            )).scalar_one()
            assert incident.status not in {"RESOLVED", "CLOSED"}
            balance = (await db.execute(select(CustomerLedgerAccount))).scalar_one()
            assert balance.available == Decimal("1000")

    asyncio.run(with_database(check))


def test_terminal_fee_reconciliation_is_included_in_customer_scheduler_query():
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    scheduler = source[
        source.index("async def reconcile_customers"):
        source.index("# Always run the crypto scope", source.index("async def reconcile_customers"))
    ]
    assert "Trade.fee_reconciliation_pending.is_(True)" in scheduler
    assert 'Trade.status.in_(["PENDING", "OPEN", "PARTIAL", "UNKNOWN"])' in scheduler
    assert "or_(" in scheduler


def test_reconciled_fee_updates_each_partial_fill_and_final_aggregate_once():
    async def check(sessions):
        async with sessions() as db:
            entry = _trade(
                customer_id=None, side="buy", quantity=0.2,
                requested_quantity=0.2, filled_quantity=0.2, fee=0.1,
                status="FILLED",
            )
            closing = _trade(
                customer_id=None, signal_id="aggregate-close",
                client_order_id="aggregate-close", side="sell", quantity=0.2,
                requested_quantity=0.2, filled_quantity=0.2, fee=0,
                status="FILLED", fee_reconciliation_pending=False,
            )
            db.add_all([entry, closing])
            await db.flush()
            db.add(Position(
                customer_id=None, exchange="binance", symbol="BTC/USDT",
                quantity=0, average_entry_price=100, entry_trade_id=entry.id,
                entry_fee=0, entry_fee_basis_known=True,
            ))
            db.add(TradeLearningEpisode(
                entry_trade_id=entry.id, customer_id=None, exchange="binance",
                symbol="BTC/USDT", side="buy", entry_quantity=0.2,
                entry_price=100, gross_pnl=3, total_fees=0.1,
                net_pnl=2.9, status="COMPLETED",
            ))
            first = StrategyOutcome(
                trade_id=entry.id, closing_trade_id=closing.id,
                customer_id=None, strategy="test", regime="UNKNOWN",
                asset="crypto", symbol="BTC/USDT", side="buy",
                realized_pnl=1, fee=0.05, return_bps=909.0909,
                net_pnl=0.95, net_return_bps=863.6363,
                closing_quantity=0.1, closing_notional=11,
                fee_basis_complete=True, lifecycle_status="PARTIAL_CLOSED",
                sequence=1,
                metadata_json='{"allocated_entry_fee":"0.05","closing_fee":"0","fee_basis_complete":true}',
            )
            final = StrategyOutcome(
                trade_id=entry.id, closing_trade_id=closing.id,
                customer_id=None, strategy="test", regime="UNKNOWN",
                asset="crypto", symbol="BTC/USDT", side="buy",
                realized_pnl=3, fee=0.1, return_bps=1304.3478,
                net_pnl=2.9, net_return_bps=1260.8695,
                closing_quantity=0.2, closing_notional=23,
                fee_basis_complete=True, lifecycle_status="CLOSED",
                sequence=2,
                metadata_json=(
                    '{"allocated_entry_fee":"0.05","closing_fee":"0",'
                    '"aggregate_fill_count":2,"final_fill_closing_quantity":"0.1",'
                    '"final_fill_closing_notional":"12","total_closing_notional":"23"}'
                ),
            )
            db.add_all([first, final])
            await db.commit()

            await execution._apply_reconciled_live_fee_basis(
                db, closing, Decimal("0.2")
            )
            await db.commit()
            await db.refresh(first)
            await db.refresh(final)
            episode = (await db.execute(
                select(TradeLearningEpisode).where(
                    TradeLearningEpisode.entry_trade_id == entry.id
                )
            )).scalar_one()

            assert first.fee == Decimal("0.15")
            assert final.fee == Decimal("0.30")
            assert first.net_pnl == Decimal("0.85")
            assert final.net_pnl == Decimal("2.70")
            assert final.closing_quantity == Decimal("0.2")
            assert final.closing_notional == Decimal("23")
            assert final.net_return_bps == pytest.approx(2.7 / 23 * 10000)
            assert episode.total_fees == Decimal("0.30")
            assert episode.net_pnl == Decimal("2.70")

    asyncio.run(with_database(check))


def test_reversal_order_fee_true_up_accounts_for_closed_opposite_leg():
    async def check(sessions):
        async with sessions() as db:
            old_entry = _trade(
                customer_id=None, signal_id="reversal-old-entry",
                client_order_id="reversal-old-entry", side="buy", quantity=1,
                requested_quantity=1, filled_quantity=1, fee=0,
                status="FILLED",
            )
            reversal = _trade(
                customer_id=None, signal_id="reversal-order",
                client_order_id="reversal-order", side="sell", quantity=1.5,
                requested_quantity=1.5, filled_quantity=1.5, fee=0,
                status="FILLED", fee_reconciliation_pending=False,
            )
            short_close = _trade(
                customer_id=None, signal_id="reversal-short-close",
                client_order_id="reversal-short-close", side="buy", quantity=0.5,
                requested_quantity=0.5, filled_quantity=0.5, fee=0,
                status="FILLED",
            )
            db.add_all([old_entry, reversal, short_close])
            await db.flush()
            db.add_all([
                TradeLearningEpisode(
                    entry_trade_id=old_entry.id, customer_id=None,
                    exchange="binance", symbol="BTC/USDT", side="buy",
                    entry_quantity=1, entry_price=100, gross_pnl=0,
                    status="COMPLETED",
                ),
                TradeLearningEpisode(
                    entry_trade_id=reversal.id, customer_id=None,
                    exchange="binance", symbol="BTC/USDT", side="sell",
                    entry_quantity=0.5, entry_price=100, gross_pnl=2.5,
                    status="COMPLETED",
                ),
            ])
            db.add_all([
                StrategyOutcome(
                    trade_id=old_entry.id, closing_trade_id=reversal.id,
                    customer_id=None, strategy="test", regime="UNKNOWN",
                    asset="crypto", symbol="BTC/USDT", side="buy",
                    realized_pnl=0, fee=0, return_bps=0, net_pnl=0,
                    net_return_bps=0, closing_quantity=1,
                    closing_notional=100, fee_basis_complete=True,
                    lifecycle_status="CLOSED", sequence=1,
                    metadata_json='{"allocated_entry_fee":"0","closing_fee":"0"}',
                ),
                StrategyOutcome(
                    trade_id=reversal.id, closing_trade_id=short_close.id,
                    customer_id=None, strategy="test", regime="UNKNOWN",
                    asset="crypto", symbol="BTC/USDT", side="sell",
                    realized_pnl=2.5, fee=0, return_bps=454.5454,
                    net_pnl=2.5, net_return_bps=454.5454,
                    closing_quantity=0.5, closing_notional=55,
                    fee_basis_complete=True, lifecycle_status="CLOSED",
                    sequence=1,
                    metadata_json='{"allocated_entry_fee":"0","closing_fee":"0"}',
                ),
            ])
            await db.commit()

            await execution._apply_reconciled_live_fee_basis(
                db, reversal, Decimal("0.15")
            )
            await db.commit()

            old_row = (await db.execute(
                select(StrategyOutcome).where(
                    StrategyOutcome.trade_id == old_entry.id
                )
            )).scalar_one()
            new_row = (await db.execute(
                select(StrategyOutcome).where(
                    StrategyOutcome.trade_id == reversal.id
                )
            )).scalar_one()
            old_episode = (await db.execute(
                select(TradeLearningEpisode).where(
                    TradeLearningEpisode.entry_trade_id == old_entry.id
                )
            )).scalar_one()
            new_episode = (await db.execute(
                select(TradeLearningEpisode).where(
                    TradeLearningEpisode.entry_trade_id == reversal.id
                )
            )).scalar_one()

            assert old_row.fee == pytest.approx(0.10)
            assert new_row.fee == pytest.approx(0.05)
            assert old_episode.total_fees == pytest.approx(0.10)
            assert new_episode.total_fees == pytest.approx(0.05)
            assert new_episode.net_pnl == pytest.approx(2.45)

    asyncio.run(with_database(check))


def test_open_add_on_fee_true_up_updates_aggregate_position_and_learning_episode():
    async def check(sessions):
        async with sessions() as db:
            entry = _trade(
                customer_id=None, side="buy", quantity=1.0,
                requested_quantity=1.0, filled_quantity=1.0, fee=0.10,
                status="FILLED",
            )
            add_on = _trade(
                customer_id=None, signal_id="open-position-add-on",
                client_order_id="open-position-add-on", side="buy", quantity=0.5,
                requested_quantity=0.5, filled_quantity=0.5, fee=0,
                status="FILLED", fee_reconciliation_pending=False,
            )
            db.add_all([entry, add_on])
            await db.flush()
            position = Position(
                customer_id=None, exchange="binance", symbol="BTC/USDT",
                quantity=1.5, average_entry_price=100, entry_trade_id=entry.id,
                entry_fee=Decimal("0.10"), entry_fee_basis_known=False,
            )
            episode = TradeLearningEpisode(
                entry_trade_id=entry.id, customer_id=None, exchange="binance",
                symbol="BTC/USDT", side="buy", entry_quantity=1.5,
                entry_price=100, gross_pnl=0, total_fees=Decimal("0.10"),
                net_pnl=Decimal("-0.10"), status="OPEN",
            )
            db.add_all([position, episode])
            await db.commit()

            await execution._apply_reconciled_live_fee_basis(
                db, add_on, Decimal("0.05")
            )
            await db.commit()
            await db.refresh(position)
            await db.refresh(episode)

            assert position.entry_trade_id == entry.id
            assert position.entry_fee == Decimal("0.15")
            assert position.entry_fee_basis_known is True
            assert episode.total_fees == Decimal("0.15")
            assert episode.net_pnl == Decimal("-0.15")

    asyncio.run(with_database(check))


def test_corrected_add_on_fee_survives_the_later_completed_close():
    async def check(sessions):
        async with sessions() as db:
            entry = _trade(
                customer_id=None, signal_id="entry-with-add-on",
                client_order_id="entry-with-add-on", side="buy",
                quantity=1.0, requested_quantity=1.0,
                filled_quantity=1.0, fee=Decimal("0.10"),
                fee_reconciliation_pending=False, status="FILLED",
            )
            add_on = _trade(
                customer_id=None, signal_id="corrected-add-on",
                client_order_id="corrected-add-on", side="buy",
                quantity=0.5, requested_quantity=0.5,
                filled_quantity=0.5, fee=Decimal("0"),
                fee_reconciliation_pending=False, status="FILLED",
            )
            closing = _trade(
                customer_id=None, signal_id="close-after-add-on-correction",
                client_order_id="close-after-add-on-correction", side="sell",
                quantity=1.5, requested_quantity=1.5,
                filled_quantity=0, fee=Decimal("0.02"),
                fee_reconciliation_pending=False, status="OPEN",
            )
            db.add_all([entry, add_on, closing])
            await db.flush()
            position = Position(
                customer_id=None, exchange="binance", symbol="BTC/USDT",
                quantity=Decimal("1.5"), average_entry_price=100,
                entry_trade_id=entry.id, entry_fee=Decimal("0.10"),
                # The original entry fee is known; an earlier unknown add-on
                # fee made the aggregate basis incomplete until this true-up.
                entry_fee_basis_known=False,
            )
            episode = TradeLearningEpisode(
                entry_trade_id=entry.id, customer_id=None, exchange="binance",
                symbol="BTC/USDT", side="buy", entry_quantity=Decimal("1.5"),
                entry_price=100, gross_pnl=0, total_fees=Decimal("0.10"),
                net_pnl=Decimal("-0.10"), status="OPEN",
            )
            db.add_all([position, episode])
            await db.commit()

            await execution._apply_reconciled_live_fee_basis(
                db, add_on, Decimal("0.05")
            )
            assert position.entry_fee_basis_known is True
            assert position.entry_fee == Decimal("0.15")

            await execution._apply_fill_to_position(
                db,
                closing,
                1.5,
                110.0,
                current_fill_fee=Decimal("0.02"),
                current_fill_fee_verified=True,
            )
            await db.commit()
            await db.refresh(episode)

            outcome = (await db.execute(
                select(StrategyOutcome).where(
                    StrategyOutcome.trade_id == entry.id,
                    StrategyOutcome.lifecycle_status == "CLOSED",
                )
            )).scalar_one()
            metadata = __import__("json").loads(outcome.metadata_json or "{}")
            assert outcome.fee_basis_complete is True
            assert outcome.fee == Decimal("0.17")
            assert metadata["allocated_entry_fee"] == "0.15"
            assert metadata["closing_fee"] == "0.02"
            assert episode.total_fees == Decimal("0.17")
            assert episode.net_pnl == Decimal("14.83")

    asyncio.run(with_database(check))

def test_reused_partial_entry_order_episode_fees_exclude_prior_lifecycles():
    async def check(sessions):
        async with sessions() as db:
            # This entry order has already closed one position and then continued
            # filling. Its prior CLOSED outcome must not enter the new episode.
            entry = _trade(
                customer_id=None, signal_id="reused-partial-entry",
                client_order_id="reused-partial-entry", side="buy",
                quantity=2.0, requested_quantity=2.0, filled_quantity=1.5,
                fee=Decimal("0.10"), fee_reconciliation_pending=False,
                status="PARTIAL",
            )
            prior_close = _trade(
                customer_id=None, signal_id="prior-lifecycle-close",
                client_order_id="prior-lifecycle-close", side="sell",
                quantity=1.0, requested_quantity=1.0, filled_quantity=1.0,
                fee=Decimal("0.80"), status="FILLED",
            )
            current_partial_close = _trade(
                customer_id=None, signal_id="current-lifecycle-partial-close",
                client_order_id="current-lifecycle-partial-close", side="sell",
                quantity=0.5, requested_quantity=0.5, filled_quantity=0.5,
                fee=Decimal("0.05"), status="FILLED",
            )
            current_final_close = _trade(
                customer_id=None, signal_id="current-lifecycle-final-close",
                client_order_id="current-lifecycle-final-close", side="sell",
                quantity=0.5, requested_quantity=0.5, filled_quantity=0,
                fee=Decimal("0.02"), fee_reconciliation_pending=False,
                status="OPEN",
            )
            db.add_all([
                entry, prior_close, current_partial_close, current_final_close
            ])
            await db.flush()

            position = Position(
                customer_id=None, exchange="binance", symbol="BTC/USDT",
                quantity=Decimal("0.5"), average_entry_price=100,
                entry_trade_id=entry.id, entry_fee=Decimal("0.05"),
                entry_fee_basis_known=True,
            )
            episode = TradeLearningEpisode(
                entry_trade_id=entry.id, customer_id=None, exchange="binance",
                symbol="BTC/USDT", side="buy", entry_quantity=Decimal("1.0"),
                entry_price=100, gross_pnl=Decimal("5"),
                total_fees=Decimal("0.15"), net_pnl=Decimal("4.85"),
                status="OPEN",
            )
            prior_lifecycle = StrategyOutcome(
                trade_id=entry.id, closing_trade_id=prior_close.id,
                customer_id=None, strategy="test", regime="UNKNOWN",
                asset="crypto", symbol="BTC/USDT", side="buy",
                realized_pnl=Decimal("10"), fee=Decimal("0.90"),
                return_bps=1000.0, net_pnl=Decimal("9.10"),
                net_return_bps=910.0, closing_quantity=Decimal("1.0"),
                closing_notional=Decimal("100"), fee_basis_complete=True,
                lifecycle_status="CLOSED", sequence=1,
                metadata_json=(
                    '{"allocated_entry_fee":"0.10","closing_fee":"0.80",'
                    '"fee_basis_complete":true}'
                ),
            )
            current_lifecycle_partial = StrategyOutcome(
                trade_id=entry.id, closing_trade_id=current_partial_close.id,
                customer_id=None, strategy="test", regime="UNKNOWN",
                asset="crypto", symbol="BTC/USDT", side="buy",
                realized_pnl=Decimal("5"), fee=Decimal("0.10"),
                return_bps=1000.0, net_pnl=Decimal("4.90"),
                net_return_bps=980.0, closing_quantity=Decimal("0.5"),
                closing_notional=Decimal("50"), fee_basis_complete=True,
                lifecycle_status="OPEN", sequence=2,
                metadata_json=(
                    '{"allocated_entry_fee":"0.05","closing_fee":"0.05",'
                    '"fee_basis_complete":true}'
                ),
            )
            db.add_all([position, episode, prior_lifecycle, current_lifecycle_partial])
            await db.commit()

            await execution._apply_fill_to_position(
                db,
                current_final_close,
                0.5,
                110.0,
                current_fill_fee=Decimal("0.02"),
                current_fill_fee_verified=True,
            )
            await db.commit()
            await db.refresh(episode)

            current_final = (await db.execute(
                select(StrategyOutcome).where(
                    StrategyOutcome.trade_id == entry.id,
                    StrategyOutcome.sequence == 3,
                )
            )).scalar_one()
            await db.refresh(prior_lifecycle)

            # Only current-lifecycle fees count: entry 0.10, prior current
            # close 0.05, and this final close 0.02. The older 0.90 is excluded.
            assert episode.total_fees == Decimal("0.17")
            assert episode.net_pnl == Decimal("9.83")
            assert current_final.fee == Decimal("0.17")
            assert current_final.net_pnl == Decimal("9.83")
            assert current_final.lifecycle_status == "CLOSED"
            assert prior_lifecycle.fee == Decimal("0.90")
            assert prior_lifecycle.lifecycle_status == "CLOSED"

    asyncio.run(with_database(check))
