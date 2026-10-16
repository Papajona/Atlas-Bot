"""Regression tests for the persistent bar-driven platform paper execution path."""
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
        async with SessionLocal() as db:
            state = await execution._get_state_locked(db)
            state.kill_switch = False
            await db.commit()
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


def test_platform_paper_order_is_unfilled_then_partially_filled_once_per_completed_bar(monkeypatch):
    import pandas as pd
    from sqlalchemy import select
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, Trade, Position, PaperOrderState, PaperFillEvent

    symbol = f"PAPER-{uuid.uuid4().hex[:10]}"
    now = pd.Timestamp.now(tz="UTC")
    current_floor = now.floor("5min")
    submit_bar = current_floor - pd.Timedelta(minutes=10)
    process_bar = current_floor - pd.Timedelta(minutes=5)
    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.10)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 24)

    async def run():
        await init_db()
        async with SessionLocal() as db:
            state = await execution._get_state_locked(db)
            state.kill_switch = False
            await db.commit()
        df0 = pd.DataFrame(
            {"open":[99.0, 99.5, 100.0], "high":[100.0,100.0,101.0],
             "low":[98.0,99.0,99.5], "close":[99.0,99.5,100.0], "volume":[1.0,1.0,1.0]},
            index=pd.DatetimeIndex([submit_bar-pd.Timedelta(minutes=10),
                                    submit_bar-pd.Timedelta(minutes=5), submit_bar]),
        )
        provenance0 = {
            "source":"ccxt_ohlcv", "symbol":symbol, "exchange":"binance", "timeframe":"5m",
            "fetched_at_utc":now.isoformat(), "row_count":len(df0), "sha256":"a"*64,
            "asset_class":"crypto", "market_type":"spot",
            "volume_unit":"base_asset", "volume_unit_verified":True,
        }
        df0.attrs["data_provenance"] = provenance0
        context = {
            "bar_timestamp":submit_bar.isoformat(), "data_provenance":provenance0,
            "asset":"crypto", "symbol":symbol, "exchange":"binance", "timeframe":"5m",
        }
        submitted = await execution.execute_signal(
            symbol, "buy", 0.3, 100.0,
            {"score":0.9, "_paper_execution_context":context},
            "binance", "5m", submit_bar.isoformat(),
            force_paper=True, stop_loss_price=99.0, take_profit_price=102.0,
        )
        assert submitted["status"] == "OPEN"
        assert submitted["filled_quantity"] == 0.0
        assert submitted["remaining_quantity"] == pytest.approx(0.3)

        async with SessionLocal() as db:
            trade = (await db.execute(select(Trade).where(
                Trade.symbol == symbol, Trade.mode == "PAPER"
            ))).scalar_one()
            order_state = (await db.execute(select(PaperOrderState).where(
                PaperOrderState.trade_id == trade.id
            ))).scalar_one()
            assert trade.status == "OPEN"
            assert trade.filled_quantity == 0.0
            assert order_state.submitted_bar_at is not None

        df1 = pd.DataFrame(
            {"open":[99.0,99.5,100.0,100.5], "high":[100.0,100.0,101.0,101.5],
             "low":[98.0,99.0,99.5,98.5], "close":[99.0,99.5,100.0,101.0],
             "volume":[1.0,1.0,1.0,1.0]},
            index=pd.DatetimeIndex([submit_bar-pd.Timedelta(minutes=10),
                                    submit_bar-pd.Timedelta(minutes=5), submit_bar, process_bar]),
        )
        provenance1 = dict(provenance0, fetched_at_utc=pd.Timestamp.now(tz="UTC").isoformat(),
                           row_count=len(df1), sha256="b"*64)
        df1.attrs["data_provenance"] = provenance1
        result = await execution.advance_platform_paper_orders(
            df1, exchange="binance", symbol=symbol, timeframe="5m", asset="crypto"
        )
        assert result["status"] == "PROCESSED"
        assert result["liquidity_verified"] is True
        assert result["fills"] == 1
        assert result["partial_fills"] == 1
        # The fill is modeled at bar close: do not trigger its stop from that bar's earlier low.
        assert result["stop_triggers"] == 0
        assert result["protective_exit_orders_created"] == 0

        async with SessionLocal() as db:
            trade = (await db.execute(select(Trade).where(
                Trade.symbol == symbol, Trade.mode == "PAPER"
            ))).scalar_one()
            position = (await db.execute(select(Position).where(
                Position.symbol == symbol, Position.exchange == "binance",
                Position.customer_id.is_(None),
            ))).scalar_one()
            events = (await db.execute(select(PaperFillEvent).where(
                PaperFillEvent.trade_id == trade.id
            ))).scalars().all()
            assert trade.status == "PARTIAL"
            assert trade.filled_quantity == pytest.approx(0.1)
            assert trade.remaining_quantity == pytest.approx(0.2)
            assert trade.average_fill_price > 101.0
            assert position.quantity == pytest.approx(0.1)
            assert len(events) == 1
            assert events[0].outcome == "PARTIAL_FILL"

        repeated = await execution.advance_platform_paper_orders(
            df1, exchange="binance", symbol=symbol, timeframe="5m", asset="crypto"
        )
        assert repeated["fills"] == 0
        assert repeated["attempts"] == 0

    asyncio.run(run())


def test_platform_paper_submission_rejects_fx_and_unverified_derivatives():
    import pandas as pd
    from app import execution
    from app.db import init_db, SessionLocal, Trade

    fx_symbol = f"FXPAPER-{uuid.uuid4().hex[:8]}"
    derivative_symbol = f"SWAPPAPER-{uuid.uuid4().hex[:8]}"
    now = pd.Timestamp.now(tz="UTC")
    bar_at = now.floor("5min") - pd.Timedelta(minutes=10)

    async def run():
        await init_db()
        async with SessionLocal() as db:
            state = await execution._get_state_locked(db)
            state.kill_switch = False
            await db.commit()
        fx_provenance = {
            "source":"oanda_v20_mid", "symbol":"EUR_USD", "exchange":"oanda", "timeframe":"5m",
            "fetched_at_utc":now.isoformat(), "row_count":3, "sha256":"c"*64,
            "asset_class":"forex", "market_type":"otc", "volume_unit":"unknown",
            "volume_unit_verified":False,
        }
        fx_context = {
            "bar_timestamp":bar_at.isoformat(), "data_provenance":fx_provenance,
            "asset":"forex", "symbol":fx_symbol, "exchange":"oanda", "timeframe":"5m",
        }
        with pytest.raises(execution.RiskBlocked, match="limited to verified crypto spot"):
            await execution.execute_signal(
                fx_symbol, "buy", 0.3, 1.1,
                {"score":0.9, "_paper_execution_context":fx_context},
                "oanda", "5m", bar_at.isoformat(),
                force_paper=True, stop_loss_price=1.09, take_profit_price=1.12, asset="forex",
            )

        derivative_provenance = {
            "source":"ccxt_ohlcv", "symbol":derivative_symbol, "exchange":"binance", "timeframe":"5m",
            "fetched_at_utc":now.isoformat(), "row_count":3, "sha256":"d"*64,
            "asset_class":"crypto", "market_type":"derivative_or_unknown",
            "volume_unit":"unknown", "volume_unit_verified":False,
        }
        derivative_context = {
            "bar_timestamp":bar_at.isoformat(), "data_provenance":derivative_provenance,
            "asset":"crypto", "symbol":derivative_symbol, "exchange":"binance", "timeframe":"5m",
        }
        with pytest.raises(execution.RiskBlocked, match="verified CCXT crypto spot market"):
            await execution.execute_signal(
                derivative_symbol, "buy", 0.3, 50000.0,
                {"score":0.9, "_paper_execution_context":derivative_context},
                "binance", "5m", bar_at.isoformat(),
                force_paper=True, stop_loss_price=49000.0, take_profit_price=52000.0, asset="crypto",
            )

        async with SessionLocal() as db:
            fx_trades = (await db.execute(select(Trade).where(Trade.symbol == fx_symbol))).scalars().all()
            derivative_trades = (await db.execute(select(Trade).where(Trade.symbol == derivative_symbol))).scalars().all()
            assert fx_trades == []
            assert derivative_trades == []

    asyncio.run(run())

def test_paper_protective_stop_triggers_on_completed_bar_and_fills_later(monkeypatch):
    import pandas as pd
    from sqlalchemy import select
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, Trade, Position, PaperOrderState, PaperFillEvent

    symbol = f"STOP-{uuid.uuid4().hex[:10]}"
    now = pd.Timestamp.now(tz="UTC")
    current_floor = now.floor("5min")
    trigger_bar = current_floor - pd.Timedelta(minutes=10)
    fill_bar = current_floor - pd.Timedelta(minutes=5)
    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.10)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 1)

    async def run():
        await init_db()
        async with SessionLocal() as db:
            state = await execution._get_state_locked(db)
            state.kill_switch = False
            entry = Trade(
                signal_id=f"seed-stop-{uuid.uuid4().hex}",
                client_order_id=f"seed-stop-{uuid.uuid4().hex}",
                exchange="binance", symbol=symbol, timeframe="5m", side="buy",
                quantity=1.0, requested_quantity=1.0, filled_quantity=1.0,
                remaining_quantity=0.0, requested_price=100.0, average_fill_price=100.0,
                fee=0.0, notional=100.0, mode="PAPER", status="FILLED",
                reason='{"strategy":"test","asset":"crypto"}',
                stop_loss_price=95.0, take_profit_price=110.0,
            )
            db.add(entry)
            await db.flush()
            db.add(Position(
                customer_id=None, trading_account_id=None, exchange="binance",
                symbol=symbol, quantity=1.0, average_entry_price=100.0,
                mark_price=100.0, realized_pnl=0.0, unrealized_pnl=0.0,
                reserved_capital=0.0, strategy="test", entry_regime="UNKNOWN",
                entry_trade_id=entry.id,
            ))
            await db.commit()
            entry_id = entry.id

        history = [
            trigger_bar - pd.Timedelta(minutes=10),
            trigger_bar - pd.Timedelta(minutes=5),
            trigger_bar,
        ]
        df_trigger = pd.DataFrame(
            {"open":[100.0,99.0,94.0], "high":[101.0,100.0,96.0],
             "low":[99.0,98.0,93.0], "close":[100.0,99.0,94.0],
             "volume":[100.0,100.0,100.0]},
            index=pd.DatetimeIndex(history),
        )
        provenance = {
            "source":"ccxt_ohlcv", "symbol":symbol, "exchange":"binance", "timeframe":"5m",
            "fetched_at_utc":now.isoformat(), "row_count":len(df_trigger), "sha256":"e"*64,
            "asset_class":"crypto", "market_type":"spot",
            "volume_unit":"base_asset", "volume_unit_verified":True,
        }
        df_trigger.attrs["data_provenance"] = provenance
        triggered = await execution.advance_platform_paper_orders(
            df_trigger, exchange="binance", symbol=symbol, timeframe="5m", asset="crypto"
        )
        assert triggered["stop_triggers"] == 1
        assert triggered["take_profit_triggers"] == 0
        assert triggered["protective_exit_orders_created"] == 1

        async with SessionLocal() as db:
            exits = (await db.execute(select(Trade).where(
                Trade.symbol == symbol, Trade.mode == "PAPER",
                Trade.side == "sell", Trade.id != entry_id,
            ))).scalars().all()
            assert len(exits) == 1
            exit_trade = exits[0]
            assert exit_trade.status == "OPEN"
            assert exit_trade.filled_quantity == 0.0
            assert exit_trade.remaining_quantity == pytest.approx(1.0)
            assert (await db.execute(select(PaperOrderState).where(
                PaperOrderState.trade_id == exit_trade.id
            ))).scalar_one().submitted_bar_at is not None
            trigger_event = (await db.execute(select(PaperFillEvent).where(
                PaperFillEvent.trade_id == exit_trade.id
            ))).scalars().all()
            assert trigger_event[0].outcome == "STOP_TRIGGERED"

        df_fill = pd.DataFrame(
            {"open":[100.0,99.0,94.0,92.0], "high":[101.0,100.0,96.0,93.0],
             "low":[99.0,98.0,93.0,91.0], "close":[100.0,99.0,94.0,92.0],
             "volume":[100.0,100.0,100.0,100.0]},
            index=pd.DatetimeIndex(history + [fill_bar]),
        )
        df_fill.attrs["data_provenance"] = dict(
            provenance, fetched_at_utc=pd.Timestamp.now(tz="UTC").isoformat(),
            row_count=len(df_fill), sha256="f"*64,
        )
        fill_result = await execution.advance_platform_paper_orders(
            df_fill, exchange="binance", symbol=symbol, timeframe="5m", asset="crypto"
        )
        assert fill_result["fills"] == 1

        async with SessionLocal() as db:
            exit_trade = (await db.execute(select(Trade).where(
                Trade.symbol == symbol, Trade.mode == "PAPER", Trade.side == "sell",
                Trade.id != entry_id,
            ))).scalar_one()
            position = (await db.execute(select(Position).where(
                Position.symbol == symbol, Position.exchange == "binance",
                Position.customer_id.is_(None),
            ))).scalar_one()
            assert exit_trade.status == "FILLED"
            # The next bar opened below the stop: model the worse open plus adverse slippage.
            assert exit_trade.average_fill_price < 92.0
            assert position.quantity == pytest.approx(0.0)

    asyncio.run(run())



def test_paper_simulator_stress_shared_capacity_idempotency_and_fee_accounting(monkeypatch):
    """Stress a busy bar: shared liquidity cap, partial fills, retry safety, and fee accounting."""
    import asyncio
    import json
    import pandas as pd
    from decimal import Decimal
    from sqlalchemy import select
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, AppState, Trade, Position, PaperOrderState, PaperFillEvent, PaperBarState

    suffix = uuid.uuid4().hex[:12]
    symbol = f"STRESS-{suffix}"
    now = pd.Timestamp.now(tz="UTC")
    current_floor = now.floor("5min")
    submitted_bar = current_floor - pd.Timedelta(minutes=10)
    processed_bar = current_floor - pd.Timedelta(minutes=5)
    order_count = 25
    order_quantity = 0.75
    volume = 100.0
    expected_capacity = volume * 0.10

    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.10)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 24)
    monkeypatch.setattr(settings, "paper_market_impact_coefficient", 0.10)
    monkeypatch.setattr(settings, "paper_max_market_impact_bps", 250.0)

    async def run():
        await init_db()
        trade_ids = []
        async with SessionLocal() as db:
            state = await execution._get_state_locked(db)
            state.kill_switch = False
            state.cash_equity = 10000.0
            for index in range(order_count):
                unique = f"{suffix}-{index:03d}"
                trade = Trade(
                    signal_id=f"stress-signal-{unique}",
                    client_order_id=f"stress-client-{unique}",
                    exchange="binance",
                    symbol=symbol,
                    timeframe="5m",
                    side="buy",
                    quantity=order_quantity,
                    requested_quantity=order_quantity,
                    filled_quantity=0.0,
                    remaining_quantity=order_quantity,
                    requested_price=100.0,
                    average_fill_price=0.0,
                    fee=0.0,
                    notional=0.0,
                    mode="PAPER",
                    status="OPEN",
                    reason=json.dumps({"strategy": "paper-stress-test", "asset": "crypto"}),
                    error="",
                    stop_loss_price=90.0,
                    take_profit_price=120.0,
                )
                db.add(trade)
                await db.flush()
                db.add(PaperOrderState(
                    trade_id=trade.id,
                    exchange="binance",
                    symbol=symbol,
                    timeframe="5m",
                    submitted_bar_at=submitted_bar.to_pydatetime(),
                    last_processed_bar_at=None,
                    bars_elapsed=0,
                    status="OPEN",
                ))
                trade_ids.append(trade.id)
            await db.commit()

        index = pd.DatetimeIndex([
            current_floor - pd.Timedelta(minutes=15),
            submitted_bar,
            processed_bar,
        ])
        df = pd.DataFrame({
            "open": [99.0, 100.0, 100.5],
            "high": [100.0, 101.0, 101.5],
            "low": [98.0, 99.0, 100.0],
            "close": [99.0, 100.0, 101.0],
            "volume": [150.0, 150.0, volume],
        }, index=index)
        provenance = {
            "source": "ccxt_ohlcv",
            "symbol": symbol,
            "exchange": "binance",
            "timeframe": "5m",
            "fetched_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "row_count": len(df),
            "sha256": "a" * 64,
            "asset_class": "crypto",
            "market_type": "spot",
            "volume_unit": "base_asset",
            "volume_unit_verified": True,
        }
        df.attrs["data_provenance"] = provenance

        result = await execution.advance_platform_paper_orders(
            df, exchange="binance", symbol=symbol, timeframe="5m", asset="crypto"
        )
        assert result["status"] == "PROCESSED"
        assert result["liquidity_verified"] is True
        assert result["orders_seen"] == order_count
        assert result["fills"] == 14
        assert result["partial_fills"] == 1
        assert result["queue_misses"] == 0
        assert result["rejected"] == 0

        async with SessionLocal() as db:
            trades = (await db.execute(
                select(Trade).where(Trade.id.in_(trade_ids)).order_by(Trade.id)
            )).scalars().all()
            positions = (await db.execute(select(Position).where(
                Position.symbol == symbol,
                Position.exchange == "binance",
                Position.customer_id.is_(None),
            ))).scalars().all()
            events = (await db.execute(select(PaperFillEvent).where(
                PaperFillEvent.trade_id.in_(trade_ids)
            ))).scalars().all()
            bar = (await db.execute(select(PaperBarState).where(
                PaperBarState.exchange == "binance",
                PaperBarState.symbol == symbol,
                PaperBarState.timeframe == "5m",
            ))).scalar_one()
            state = (await db.execute(select(AppState).where(AppState.id == 1))).scalar_one()

            filled_total = sum(Decimal(str(trade.filled_quantity or 0.0)) for trade in trades)
            fee_total = sum(Decimal(str(trade.fee or 0.0)) for trade in trades)
            event_filled_total = sum(Decimal(str(event.filled_quantity or 0.0)) for event in events)
            event_fee_total = sum(Decimal(str(event.fee or 0.0)) for event in events)
            assert len(positions) == 1
            assert filled_total == Decimal("10.0")
            assert event_filled_total == Decimal("10.0")
            assert Decimal(str(bar.capacity)) == Decimal(str(expected_capacity))
            assert Decimal(str(bar.consumed)) == Decimal("10.0")
            assert sum(trade.status == "FILLED" for trade in trades) == 13
            partial = [trade for trade in trades if trade.status == "PARTIAL"]
            assert len(partial) == 1
            assert partial[0].filled_quantity == pytest.approx(0.25)
            assert sum(trade.status == "OPEN" for trade in trades) == 11
            assert event_fee_total == fee_total
            assert state.cash_equity == pytest.approx(10000.0 - float(fee_total), abs=1e-6)
            # Adverse cost modelling is exercised; fill prices are not the ideal close.
            assert all(trade.average_fill_price > 101.0 for trade in trades if trade.filled_quantity > 0)

        # A retry of the same logical bar must not add fills, events, or fees.
        repeated = await execution.advance_platform_paper_orders(
            df, exchange="binance", symbol=symbol, timeframe="5m", asset="crypto"
        )
        assert repeated["status"] == "PROCESSED"
        assert repeated["attempts"] == 0
        assert repeated["fills"] == 0

        # Reusing a bar identity with a different data hash fails closed.
        conflict_df = df.copy()
        conflict_df.attrs["data_provenance"] = dict(
            provenance,
            fetched_at_utc=pd.Timestamp.now(tz="UTC").isoformat(),
            sha256="b" * 64,
        )
        conflict = await execution.advance_platform_paper_orders(
            conflict_df, exchange="binance", symbol=symbol, timeframe="5m", asset="crypto"
        )
        assert conflict["status"] == "BAR_PROVENANCE_CONFLICT"
        async with SessionLocal() as db:
            trades = (await db.execute(select(Trade).where(Trade.id.in_(trade_ids)))).scalars().all()
            events = (await db.execute(select(PaperFillEvent).where(
                PaperFillEvent.trade_id.in_(trade_ids)
            ))).scalars().all()
            assert sum(Decimal(str(trade.filled_quantity or 0.0)) for trade in trades) == Decimal("10.0")
            assert len(events) == order_count
            assert sum(Decimal(str(event.fee or 0.0)) for event in events) == fee_total

    asyncio.run(run())


def test_existing_position_stop_preempts_same_bar_entry_fill(monkeypatch):
    """An existing protective trigger must defer ambiguous same-bar entry fills."""
    import asyncio
    import json
    import pandas as pd
    from sqlalchemy import select
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, Trade, Position, PaperOrderState, PaperFillEvent

    symbol = f"STOP-STRESS-{uuid.uuid4().hex[:10]}"
    now = pd.Timestamp.now(tz="UTC")
    current_floor = now.floor("5min")
    trigger_bar = current_floor - pd.Timedelta(minutes=10)
    submitted_bar = trigger_bar - pd.Timedelta(minutes=5)
    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.10)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 24)

    async def run():
        await init_db()
        async with SessionLocal() as db:
            state = await execution._get_state_locked(db)
            state.kill_switch = False
            state.cash_equity = 10000.0
            entry = Trade(
                signal_id=f"stop-stress-entry-{uuid.uuid4().hex}",
                client_order_id=f"stop-stress-entry-{uuid.uuid4().hex}",
                exchange="binance", symbol=symbol, timeframe="5m", side="buy",
                quantity=1.0, requested_quantity=1.0, filled_quantity=1.0,
                remaining_quantity=0.0, requested_price=100.0, average_fill_price=100.0,
                fee=0.0, notional=100.0, mode="PAPER", status="FILLED",
                reason=json.dumps({"strategy": "stop-stress", "asset": "crypto"}),
                stop_loss_price=95.0, take_profit_price=110.0,
            )
            db.add(entry)
            await db.flush()
            db.add(Position(
                customer_id=None, trading_account_id=None, exchange="binance",
                symbol=symbol, quantity=1.0, average_entry_price=100.0,
                mark_price=100.0, realized_pnl=0.0, unrealized_pnl=0.0,
                reserved_capital=0.0, strategy="stop-stress", entry_regime="UNKNOWN",
                entry_trade_id=entry.id,
            ))
            pending = Trade(
                signal_id=f"stop-stress-pending-{uuid.uuid4().hex}",
                client_order_id=f"stop-stress-pending-{uuid.uuid4().hex}",
                exchange="binance", symbol=symbol, timeframe="5m", side="buy",
                quantity=0.5, requested_quantity=0.5, filled_quantity=0.0,
                remaining_quantity=0.5, requested_price=100.0, average_fill_price=0.0,
                fee=0.0, notional=0.0, mode="PAPER", status="OPEN",
                reason=json.dumps({"strategy": "stop-stress-pending", "asset": "crypto"}),
                stop_loss_price=90.0, take_profit_price=120.0,
            )
            db.add(pending)
            await db.flush()
            db.add(PaperOrderState(
                trade_id=pending.id, exchange="binance", symbol=symbol, timeframe="5m",
                submitted_bar_at=submitted_bar.to_pydatetime(), last_processed_bar_at=None,
                bars_elapsed=0, status="OPEN",
            ))
            pending_id = pending.id
            entry_id = entry.id
            await db.commit()

        index = pd.DatetimeIndex([
            trigger_bar - pd.Timedelta(minutes=10),
            submitted_bar,
            trigger_bar,
        ])
        df = pd.DataFrame({
            "open": [100.0, 99.0, 94.0],
            "high": [101.0, 100.0, 96.0],
            "low": [99.0, 98.0, 90.0],
            "close": [100.0, 99.0, 92.0],
            "volume": [100.0, 100.0, 100.0],
        }, index=index)
        df.attrs["data_provenance"] = {
            "source": "ccxt_ohlcv", "symbol": symbol, "exchange": "binance", "timeframe": "5m",
            "fetched_at_utc": pd.Timestamp.now(tz="UTC").isoformat(), "row_count": len(df),
            "sha256": "f" * 64, "asset_class": "crypto", "market_type": "spot",
            "volume_unit": "base_asset", "volume_unit_verified": True,
        }

        result = await execution.advance_platform_paper_orders(
            df, exchange="binance", symbol=symbol, timeframe="5m", asset="crypto"
        )
        # Stop trigger has priority; do not infer entry-fill-before-stop from OHLC.
        assert result["fills"] == 0
        assert result["attempts"] == 1
        assert result["stop_triggers"] == 1
        assert result["protective_exit_orders_created"] == 1

        async with SessionLocal() as db:
            pending_after = await db.get(Trade, pending_id)
            assert pending_after.status == "OPEN"
            assert pending_after.filled_quantity == 0.0
            assert pending_after.remaining_quantity == pytest.approx(0.5)
            position = (await db.execute(select(Position).where(
                Position.symbol == symbol, Position.exchange == "binance",
                Position.customer_id.is_(None),
            ))).scalar_one()
            assert position.quantity == pytest.approx(1.0)
            exit_trade = (await db.execute(select(Trade).where(
                Trade.symbol == symbol, Trade.side == "sell", Trade.mode == "PAPER",
                Trade.id != entry_id,
            ))).scalar_one()
            assert exit_trade.status == "OPEN"
            assert exit_trade.filled_quantity == 0.0
            assert exit_trade.remaining_quantity == pytest.approx(1.0)
            trigger_event = (await db.execute(select(PaperFillEvent).where(
                PaperFillEvent.trade_id == exit_trade.id,
            ))).scalars().all()
            assert len(trigger_event) == 1
            assert trigger_event[0].outcome == "STOP_TRIGGERED"

    asyncio.run(run())
