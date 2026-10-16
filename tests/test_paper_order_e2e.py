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
             "low":[98.0,99.0,99.5,100.0], "close":[99.0,99.5,100.0,101.0],
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


def test_paper_simulator_refuses_unverified_fx_volume(monkeypatch):
    import pandas as pd
    from sqlalchemy import select
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, Trade

    symbol = f"FXPAPER-{uuid.uuid4().hex[:8]}"
    now = pd.Timestamp.now(tz="UTC")
    current_floor = now.floor("5min")
    submit_bar = current_floor - pd.Timedelta(minutes=10)
    process_bar = current_floor - pd.Timedelta(minutes=5)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 1)

    async def run():
        await init_db()
        provenance0 = {
            "source":"oanda_v20_mid", "symbol":"EUR_USD", "exchange":"oanda", "timeframe":"5m",
            "fetched_at_utc":now.isoformat(), "row_count":3, "sha256":"c"*64,
            "asset_class":"forex", "market_type":"otc", "volume_unit":"unknown",
            "volume_unit_verified":False,
        }
        context = {
            "bar_timestamp":submit_bar.isoformat(), "data_provenance":provenance0,
            "asset":"forex", "symbol":symbol, "exchange":"oanda", "timeframe":"5m",
        }
        # The symbol in the context is server-bound to the order; the provider instrument
        # can have its own normalized OANDA name. No OTC candle count is treated as base volume.
        submitted = await execution.execute_signal(
            symbol, "buy", 0.3, 100.0,
            {"score":0.9, "_paper_execution_context":context},
            "oanda", "5m", submit_bar.isoformat(),
            force_paper=True, stop_loss_price=99.0, take_profit_price=102.0, asset="forex",
        )
        assert submitted["status"] == "OPEN"
        df = pd.DataFrame(
            {"open":[99.0,99.5,100.0,100.5], "high":[100.0,100.0,101.0,101.5],
             "low":[98.0,99.0,99.5,100.0], "close":[99.0,99.5,100.0,101.0],
             "volume":[100000.0,100000.0,100000.0,100000.0]},
            index=pd.DatetimeIndex([submit_bar-pd.Timedelta(minutes=10),
                                    submit_bar-pd.Timedelta(minutes=5), submit_bar, process_bar]),
        )
        provenance1 = dict(provenance0, fetched_at_utc=pd.Timestamp.now(tz="UTC").isoformat(),
                           row_count=len(df), sha256="d"*64)
        df.attrs["data_provenance"] = provenance1
        result = await execution.advance_platform_paper_orders(
            df, exchange="oanda", symbol=symbol, timeframe="5m", asset="forex"
        )
        assert result["liquidity_verified"] is False
        assert result["fills"] == 0
        assert result["expired"] == 1
        async with SessionLocal() as db:
            trade = (await db.execute(select(Trade).where(
                Trade.symbol == symbol, Trade.mode == "PAPER"
            ))).scalar_one()
            assert trade.status == "CANCELED"
            assert trade.filled_quantity == 0.0
            assert trade.remaining_quantity == 0.0

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
