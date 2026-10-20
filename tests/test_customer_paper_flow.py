from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from app.config import settings
from app.customer_paper_flow import (
    CustomerPaperFlowBlocked,
    execute_customer_paper_spot_signal,
    recover_customer_paper_spot_flow,
    validate_customer_paper_market,
)
from app.customer_paper_ledger import get_customer_paper_balances
from app.customer_paper_lifecycle import get_customer_paper_positions, submit_customer_paper_order


def _market_fixture(*, timestamp: datetime | None = None, low: float = 98.0, high: float = 102.0,
                    bid: float = 99.9, ask: float = 100.1, symbol: str = "BTC/USDT",
                    exchange: str = "binance", timeframe: str = "1h"):
    now = datetime.now(timezone.utc)
    bar_at = timestamp or (now - timedelta(hours=2))
    index = pd.date_range(end=pd.Timestamp(bar_at), periods=21, freq="1h")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 21,
            "high": [max(high, 100.0)] * 21,
            "low": [min(low, 100.0)] * 21,
            "close": [100.0] * 21,
            "volume": [1000.0] * 21,
        },
        index=index,
    )
    digest = hashlib.sha256(
        f"{symbol}|{exchange}|{timeframe}|{index[-1].isoformat()}".encode("utf-8")
    ).hexdigest()
    frame.attrs["data_provenance"] = {
        "source": "ccxt_ohlcv",
        "symbol": symbol,
        "exchange": exchange,
        "timeframe": timeframe,
        "asset_class": "crypto",
        "market_type": "spot",
        "volume_unit": "base_asset",
        "volume_unit_verified": True,
        "sha256": digest,
        "row_count": len(frame),
    }
    request = SimpleNamespace(
        asset="crypto", symbol=symbol, exchange=exchange, timeframe=timeframe, days=5,
    )
    quote_at = now - timedelta(seconds=1)
    context = {
        "context_source": "server_ccxt_closed_spot_ohlcv_and_ticker",
        "market_type": "spot",
        "exchange": exchange,
        "symbol": symbol,
        "ohlcv_sha256": digest,
        "bid": bid,
        "ask": ask,
        "spread_bps": (ask - bid) / ((ask + bid) / 2) * 10_000,
        "quote_timestamp_utc": quote_at.isoformat(),
        "quote_age_seconds": 1.0,
        "last_completed_bar_timestamp_utc": index[-1].isoformat(),
    }
    return request, frame, context


def test_customer_paper_market_context_requires_verified_closed_spot_data(monkeypatch):
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    monkeypatch.setattr(settings, "customer_paper_initial_currency", "USDT")
    req, frame, context = _market_fixture()

    verified = validate_customer_paper_market(req, frame, context)
    assert verified["currency"] == "USDT"
    assert verified["data_sha256"] == frame.attrs["data_provenance"]["sha256"]
    assert verified["bid"] == 99.9

    bad_context = dict(context, quote_age_seconds=6)
    with pytest.raises(CustomerPaperFlowBlocked, match="older than five seconds"):
        validate_customer_paper_market(req, frame, bad_context)

    bad_frame = frame.copy()
    bad_frame.attrs["data_provenance"] = {
        **frame.attrs["data_provenance"],
        "market_type": "derivative_or_unknown",
        "volume_unit": "unknown",
        "volume_unit_verified": False,
    }
    with pytest.raises(CustomerPaperFlowBlocked, match="Market data provenance mismatch"):
        validate_customer_paper_market(req, bad_frame, context)


def test_customer_paper_market_blocks_wrong_quote_currency(monkeypatch):
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    monkeypatch.setattr(settings, "customer_paper_initial_currency", "USDT")
    req, frame, context = _market_fixture(symbol="BTC/BTC")
    with pytest.raises(CustomerPaperFlowBlocked, match="virtual quote currency"):
        validate_customer_paper_market(req, frame, context)


@pytest.mark.postgres
@pytest.mark.skipif(
    not (os.getenv("ATLAS_PAPER_TEST_DATABASE_URL") and os.getenv("ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL")),
    reason="requires CI-provisioned isolated customer paper and primary PostgreSQL databases",
)
def test_customer_paper_request_fill_protective_exit_and_restart_recovery(monkeypatch):
    monkeypatch.setattr(settings, "database_url", os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_database_url", os.environ["ATLAS_PAPER_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    monkeypatch.setattr(settings, "customer_paper_shared_database_enabled", False)
    monkeypatch.setattr(settings, "customer_paper_initial_balance", 10000.0)
    monkeypatch.setattr(settings, "customer_paper_initial_currency", "USDT")

    async def run():
        customer_id = 99231
        req, frame, context = _market_fixture(low=98, high=102)
        first_bar = frame.index[-1].to_pydatetime()
        opened = await execute_customer_paper_spot_signal(
            customer_id=customer_id,
            req=req,
            df=frame,
            market_context=context,
            side="BUY",
            quantity="1",
            stop_loss_price="95",
            take_profit_price="110",
            strategy="paper-flow-regression",
            signal_metadata={"model_version": "fixture-model"},
        )
        assert opened["decision"] == "TRADE"
        assert opened["status"] == "APPENDED"
        assert opened["real_money_ledger_written"] is False
        assert opened["paper_account"]["real_funds_used"] is False
        positions = await get_customer_paper_positions(customer_id=customer_id)
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC/USDT"
        assert positions[0]["stop_loss_price"] == "95"
        assert positions[0]["take_profit_price"] == "110"

        # The following completed candle crossed the stop; the simulator submits a
        # compensating SELL and fills it from the fresh post-bar quote.
        second_at = first_bar + timedelta(hours=1)
        req2, frame2, context2 = _market_fixture(
            timestamp=second_at, low=90, high=101, bid=90.0, ask=90.2,
        )
        closed = await recover_customer_paper_spot_flow(
            customer_id=customer_id, req=req2, df=frame2, market_context=context2,
        )
        assert closed is not None
        assert closed["status"] == "PROTECTIVE_EXIT"
        assert closed["decision"] == "TRADE"
        assert closed["execution"]["fill"]["status"] == "APPENDED"
        assert await get_customer_paper_positions(customer_id=customer_id) == []
        balances = await get_customer_paper_balances(customer_id=customer_id)
        by_code = {x["account_code"]: Decimal(x["balance"]) for x in balances}
        assert by_code["PAPER_FEES"] > 0
        assert by_code["PAPER_CASH"] > 0

        # Simulate a worker crash after order intent commit but before fill commit.
        recovery_customer = 99232
        recovery_req, recovery_frame, recovery_context = _market_fixture()
        from app.customer_paper_flow import ensure_customer_paper_account

        await ensure_customer_paper_account(recovery_customer, "USDT")
        signal_bar = recovery_frame.index[-1].isoformat()
        order = await submit_customer_paper_order(
            customer_id=recovery_customer,
            client_order_id="crash-recovery-order-99232",
            venue="binance",
            market_type="spot",
            symbol="BTC/USDT",
            side="BUY",
            quantity="0.25",
            currency="USDT",
            order_type="MARKET",
            metadata={
                "paper_flow_version": 1,
                "order_role": "ENTRY",
                "timeframe": str(recovery_req.timeframe),
                "signal_bar_timestamp": signal_bar,
                "data_sha256": recovery_frame.attrs["data_provenance"]["sha256"],
                "stop_loss_price": "95",
                "take_profit_price": "110",
            },
        )
        assert order["status"] == "APPENDED"
        recovered = await recover_customer_paper_spot_flow(
            customer_id=recovery_customer, req=recovery_req,
            df=recovery_frame, market_context=recovery_context,
        )
        assert recovered is not None
        assert recovered["status"] == "RECOVERY_PROCESSED"
        assert recovered["execution"]["fill"]["status"] == "APPENDED"
        recovered_positions = await get_customer_paper_positions(customer_id=recovery_customer)
        assert len(recovered_positions) == 1
        assert recovered_positions[0]["quantity"] == "0.250000000000000000"

    asyncio.run(run())


def test_customer_paper_virtual_capital_configuration_is_finite_positive_and_usdt_only():
    from pydantic import ValidationError
    from app.config import Settings

    for invalid in (0, -1, float("nan"), float("inf"), 1_000_000_000_001):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, environment="test", customer_paper_initial_balance=invalid)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="test", customer_paper_initial_currency="USD")


def test_controller_paper_gate_requires_all_paper_only_flags_and_passed_preflight(monkeypatch):
    from app import main
    from app.config import settings
    from app import customer_paper_ledger

    for field, value in (
        ("paper_trading", True),
        ("live_trading_enabled", False),
        ("customer_live_trading_enabled", False),
        ("payout_live_enabled", False),
        ("broker_sandbox", True),
        ("deriv_live_enabled", False),
        ("usdt_tron_enabled", False),
        ("customer_paper_ledger_enabled", True),
        ("customer_paper_database_url", "postgresql+asyncpg://paper:secret@localhost/atlas_paper"),
    ):
        monkeypatch.setattr(settings, field, value)

    calls = []

    async def passed_preflight(*, require_enabled=True):
        calls.append(require_enabled)
        return {"status": "PASS"}

    monkeypatch.setattr(customer_paper_ledger, "preflight_isolated_customer_paper_ledger", passed_preflight)
    result = asyncio.run(main._customer_paper_isolation_preflight())
    assert result["status"] == "PASS"
    assert calls == [True]

    monkeypatch.setattr(settings, "usdt_tron_enabled", True)
    blocked = asyncio.run(main._customer_paper_isolation_preflight())
    assert blocked["status"] == "BLOCKED"
    assert blocked["reason"] == "CUSTOMER_PAPER_SAFETY_FLAGS_NOT_READY"
    assert calls == [True]


def test_customer_bot_paper_execution_uses_isolated_flow_and_keeps_real_executor_for_non_customer_paths():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    cycle_start = source.index("async def _run_persisted_adaptive_bot_cycle(")
    cycle_end = source.index("\n\nasync def ", cycle_start)
    cycle = source[cycle_start:cycle_end]
    route_start = source.index("async def customer_start_bot(")
    route_end = source.index("\n\nasync def _require_feature", route_start)
    route = source[route_start:route_end]

    assert "customer_paper_ledger_preflight" in cycle
    assert "recover_customer_paper_spot_flow(" in cycle
    assert "execute_customer_paper_spot_signal(" in cycle
    assert "execute_customer_paper_spot_signal(" in route
    assert "force_paper=paper_mode" in cycle
    assert "force_paper=paper_mode" in route
    assert "customer_paper_ledger_enabled: bool = False" in (Path(__file__).resolve().parents[1] / "app" / "config.py").read_text()


def test_paper_position_reader_recovers_timeframe_from_immutable_order_metadata():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "customer_paper_lifecycle.py").read_text(encoding="utf-8")
    start = source.index("async def get_customer_paper_positions(")
    end = source.index("\n\nasync def ", start)
    block = source[start:end]
    assert "JOIN customer_paper.fills AS f" in block
    assert "JOIN customer_paper.orders AS o" in block
    assert "o.metadata_json ->> 'timeframe'" in block
    assert "f.bar_key AS last_event_bar_key" in block
    assert "entry_event.quantity_before = 0" in block
    assert "entry_order.metadata_json ->> 'stop_loss_price'" in block
    assert "entry_order.metadata_json ->> 'take_profit_price'" in block
    assert '"last_event_bar_key": str(row["last_event_bar_key"] or "")' in block
    assert '"stop_loss_price": str(row["stop_loss_price"] or "")' in block
    assert '"take_profit_price": str(row["take_profit_price"] or "")' in block
