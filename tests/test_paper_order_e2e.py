"""End-to-end paper order through execute_signal: catches NameErrors and Decimal/float TypeErrors."""
import asyncio
import json
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


def test_paper_buy_then_sell_round_trip(monkeypatch):
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, Trade, Position, AuditLog

    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.10)
    sym = "BTC/USDT"
    ts = uuid.uuid4().hex
    buy_context = {
        "source": "ccxt_ohlcv", "asset": "crypto", "bar_volume": 1000.0,
        "volume_unit": "base_asset", "realized_volatility_per_bar": 0.01,
        "spread_bps": 4.0, "bar_timestamp": f"submit-buy-{ts}",
    }

    async def run():
        await init_db()
        with pytest.raises(execution.RiskBlocked, match="No explicit paper execution cost profile"):
            await execution.execute_signal(
                "XAU_USD", "buy", 1.0, 2000.0, {"score": 0.9}, "oanda", "1h",
                f"{ts}-commodity", force_paper=True, asset="commodity",
            )
        buy = await execution.execute_signal(
            sym, "buy", 0.01, 50000.0,
            {"score": 0.9, "paper_execution_context": buy_context},
            "binance", "1h", f"{ts}-b", force_paper=True,
            stop_loss_price=49000.0, take_profit_price=52000.0,
        )
        assert buy, buy
        assert buy["status"] == "OPEN"
        assert buy["filled"] == 0.0
        assert buy["remaining_quantity"] == pytest.approx(0.01)
        assert buy["paper_execution_model"]["status"] == "DYNAMIC_IMPACT_ESTIMATE"
        assert buy["paper_execution_model"]["calibration_status"] == "UNVALIDATED_HEURISTIC"
        assert buy["paper_execution_model"]["impact_inputs_complete"] is True
        assert buy["paper_execution_model"]["spread_inputs_complete"] is True
        first_step = await execution.advance_paper_orders([{
            "exchange": "binance", "symbol": sym, "price": 50000.0,
            "context": {**buy_context, "bar_timestamp": f"bar-buy-{ts}-1"},
        }])
        assert first_step["updated"]
        async with SessionLocal() as db:
            buy_row = await db.get(Trade, buy["trade_id"])
            assert buy_row.filled_quantity > 0.0
            assert buy_row.status == "SIMULATED"
            assert buy_row.remaining_quantity == pytest.approx(0.0)
            assert buy_row.average_fill_price > 50000.0

        sell_context = {
            "source": "ccxt_ohlcv", "asset": "crypto", "bar_volume": 1000.0,
            "volume_unit": "base_asset", "realized_volatility_per_bar": 0.01,
            "spread_bps": 4.0, "bar_timestamp": f"submit-sell-{ts}",
        }
        sell = await execution.execute_signal(
            sym, "sell", 0.01, 50500.0,
            {"score": 0.9, "paper_execution_context": sell_context},
            "binance", "1h", f"{ts}-s", force_paper=True,
            stop_loss_price=51500.0, take_profit_price=49000.0,
        )
        assert sell, sell
        assert sell["status"] == "OPEN" and sell["filled"] == 0.0
        await execution.advance_paper_orders([{
            "exchange": "binance", "symbol": sym, "price": 50500.0,
            "context": {**sell_context, "bar_timestamp": f"bar-sell-{ts}-1"},
        }])
        async with SessionLocal() as db:
            sell_row = await db.get(Trade, sell["trade_id"])
            assert sell_row.filled_quantity > 0.0
            assert sell_row.status == "SIMULATED"
            assert sell_row.average_fill_price < 50500.0
        async with SessionLocal() as db:
            trades = (await db.execute(select(Trade).where(Trade.client_order_id.like("ait-%")))).scalars().all()
            assert trades
            assert all(isinstance(t.filled_quantity, float) and isinstance(t.requested_price, float) for t in trades)
            audits = (await db.execute(select(AuditLog).where(AuditLog.event == "PAPER_ORDER"))).scalars().all()
            assert audits

    asyncio.run(run())


def test_stress_many_paper_orders_cannot_overconsume_one_bar_liquidity(monkeypatch):
    """Integration stress: 50 simultaneous orders share one 2-unit synthetic bar budget."""
    from app import execution
    from app.config import settings
    from app.db import init_db, SessionLocal, Trade
    from sqlalchemy import select

    monkeypatch.setattr(settings, "paper_queue_fill_probability", 1.0)
    monkeypatch.setattr(settings, "paper_rejection_probability", 0.0)
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.10)
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    prefix = f"stress-{uuid.uuid4().hex}"
    symbol = f"STRESS-{uuid.uuid4().hex[:8]}/USDT"
    exchange = "stress-simulator"
    previous_bar = "2026-10-15T11:00:00Z"
    current_bar = "2026-10-15T12:00:00Z"
    order_count = 50
    requested_each = 100.0
    context = {
        "source": "ccxt_ohlcv", "asset": "crypto", "bar_volume": 20.0,
        "volume_unit": "base_asset", "realized_volatility_per_bar": 0.03,
        "spread_bps": 6.0, "bar_timestamp": current_bar,
    }

    async def run():
        await init_db()
        async with SessionLocal() as db:
            for i in range(order_count):
                order_key = f"{prefix}-{i:03d}"
                db.add(Trade(
                    signal_id=f"{order_key}-signal",
                    client_order_id=order_key,
                    exchange=exchange,
                    symbol=symbol,
                    timeframe="1h",
                    side="buy",
                    quantity=requested_each,
                    requested_quantity=requested_each,
                    filled_quantity=0.0,
                    remaining_quantity=requested_each,
                    requested_price=100.0,
                    mode="PAPER",
                    status="OPEN",
                    reason=json.dumps({
                        "asset": "crypto",
                        "strategy": "synthetic-liquidity-stress",
                        "paper_execution_state": {
                            "last_processed_bar_timestamp": previous_bar,
                            "bars_waited": 0,
                            "events": 0,
                            "last_event": "ORDER_ACCEPTED",
                        },
                    }),
                ))
            await db.commit()

        snapshot = [{
            "exchange": exchange,
            "symbol": symbol,
            "price": 100.0,
            "context": context,
        }]
        first = await execution.advance_paper_orders(snapshot, max_orders=100)
        assert first["checked"] == order_count
        assert first["updated"]
        async with SessionLocal() as db:
            rows = (await db.execute(
                select(Trade).where(Trade.client_order_id.like(f"{prefix}-%"))
            )).scalars().all()
            assert len(rows) == order_count
            total_filled_first = sum(float(row.filled_quantity or 0.0) for row in rows)
            total_remaining_first = sum(float(row.remaining_quantity or 0.0) for row in rows)
            assert any(row.status == "PARTIAL" for row in rows)
            assert all(row.mode == "PAPER" and row.customer_id is None for row in rows)

        # The aggregate fill cannot exceed 10% of the single bar's 20 units of volume.
        aggregate_capacity = float(context["bar_volume"]) * settings.paper_max_bar_volume_participation
        assert 0.0 < total_filled_first <= aggregate_capacity + 1e-9
        assert total_remaining_first == pytest.approx(order_count * requested_each - total_filled_first)

        # Retrying the same bar must be idempotent and must not spend its liquidity twice.
        repeated = await execution.advance_paper_orders(snapshot, max_orders=100)
        assert repeated["updated"] == []
        async with SessionLocal() as db:
            rows_after = (await db.execute(
                select(Trade).where(Trade.client_order_id.like(f"{prefix}-%"))
            )).scalars().all()
            total_filled_second = sum(float(row.filled_quantity or 0.0) for row in rows_after)
        assert total_filled_second == pytest.approx(total_filled_first)

    asyncio.run(run())
