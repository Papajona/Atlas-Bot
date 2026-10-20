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
