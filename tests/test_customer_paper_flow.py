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
    timeframe_seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "1d": 86400}[timeframe]
    bar_at = timestamp or (now - timedelta(hours=2))
    index = pd.date_range(end=pd.Timestamp(bar_at), periods=21, freq=pd.to_timedelta(timeframe_seconds, unit="s"))
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
    quote_at = bar_at + timedelta(seconds=timeframe_seconds + 1)
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
    assert verified["bid"] == Decimal("99.9")

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
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.01)

    async def run():
        customer_id = 99231
        first_at = datetime.now(timezone.utc) - timedelta(hours=4)
        req, frame, context = _market_fixture(timestamp=first_at, low=98, high=102)
        first_bar = frame.index[-1].to_pydatetime()
        submitted = await execute_customer_paper_spot_signal(
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
        # Orders are durable first; no fill can use the candle that generated the signal.
        assert submitted["decision"] == "NO_TRADE"
        assert submitted["status"] == "WAITING_FOR_NEXT_COMPLETED_BAR"
        assert submitted["real_money_ledger_written"] is False
        assert submitted["paper_account"]["real_funds_used"] is False
        assert await get_customer_paper_positions(customer_id=customer_id) == []

        # The next completed bar supplies both the fill quote and shared liquidity budget.
        second_at = first_bar + timedelta(hours=1)
        req2, frame2, context2 = _market_fixture(timestamp=second_at, low=98, high=102)
        opened = await recover_customer_paper_spot_flow(
            customer_id=customer_id, req=req2, df=frame2, market_context=context2,
        )
        assert opened is not None
        assert opened["status"] == "RECOVERY_PROCESSED"
        assert opened["decision"] == "TRADE"
        assert opened["execution"]["fill"]["status"] == "APPENDED"
        positions = await get_customer_paper_positions(customer_id=customer_id)
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTC/USDT"
        assert positions[0]["quantity"] == "1.000000000000000000"
        assert positions[0]["stop_loss_price"] == "95"
        assert positions[0]["take_profit_price"] == "110"
        assert datetime.fromisoformat(positions[0]["last_event_bar_key"]) == second_at

        # A completed candle crossing the stop creates a durable exit intent, but the
        # exit is filled only on a later completed bar (never retrospectively in-bar).
        third_at = second_at + timedelta(hours=1)
        req3, frame3, context3 = _market_fixture(timestamp=third_at, low=90, high=101, bid=90.0, ask=90.2)
        pending_exit = await recover_customer_paper_spot_flow(
            customer_id=customer_id, req=req3, df=frame3, market_context=context3,
        )
        assert pending_exit is not None
        assert pending_exit["status"] == "PROTECTIVE_EXIT_PENDING"
        assert pending_exit["decision"] == "NO_TRADE"
        assert pending_exit["execution"]["status"] == "WAITING_FOR_NEXT_COMPLETED_BAR"
        assert len(await get_customer_paper_positions(customer_id=customer_id)) == 1

        fourth_at = third_at + timedelta(hours=1)
        req4, frame4, context4 = _market_fixture(timestamp=fourth_at, low=89, high=95, bid=90.0, ask=90.2)
        closed = await recover_customer_paper_spot_flow(
            customer_id=customer_id, req=req4, df=frame4, market_context=context4,
        )
        assert closed is not None
        assert closed["status"] == "RECOVERY_PROCESSED"
        assert closed["decision"] == "TRADE"
        assert closed["execution"]["fill"]["status"] == "APPENDED"
        assert await get_customer_paper_positions(customer_id=customer_id) == []
        balances = await get_customer_paper_balances(customer_id=customer_id)
        by_code = {x["account_code"]: Decimal(x["balance"]) for x in balances}
        assert by_code["PAPER_FEES"] > 0
        assert by_code["PAPER_CASH"] > 0

        # Two tenants cannot spend more than the shared capacity for the same bar.
        monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.0005)
        capacity_customer_a, capacity_customer_b = 99233, 99234
        capacity_at = fourth_at + timedelta(minutes=5)
        cap_req_a, cap_frame_a, cap_context_a = _market_fixture(timestamp=capacity_at, timeframe="1m")
        cap_req_b, cap_frame_b, cap_context_b = _market_fixture(timestamp=capacity_at, timeframe="1m")
        for cid, reqx, framex, contextx in (
            (capacity_customer_a, cap_req_a, cap_frame_a, cap_context_a),
            (capacity_customer_b, cap_req_b, cap_frame_b, cap_context_b),
        ):
            intent = await execute_customer_paper_spot_signal(
                customer_id=cid, req=reqx, df=framex, market_context=contextx,
                side="BUY", quantity="0.4", stop_loss_price="95", take_profit_price="110",
                strategy="shared-capacity-regression",
            )
            assert intent["status"] == "WAITING_FOR_NEXT_COMPLETED_BAR"
        capacity_fill_at = capacity_at + timedelta(minutes=1)
        cap_req_a2, cap_frame_a2, cap_context_a2 = _market_fixture(timestamp=capacity_fill_at, timeframe="1m")
        cap_req_b2, cap_frame_b2, cap_context_b2 = _market_fixture(timestamp=capacity_fill_at, timeframe="1m")
        a_fill = await recover_customer_paper_spot_flow(
            customer_id=capacity_customer_a, req=cap_req_a2, df=cap_frame_a2, market_context=cap_context_a2,
        )
        b_fill = await recover_customer_paper_spot_flow(
            customer_id=capacity_customer_b, req=cap_req_b2, df=cap_frame_b2, market_context=cap_context_b2,
        )
        assert a_fill["execution"]["fill"]["status"] == "APPENDED"
        assert b_fill["execution"]["fill"]["status"] == "APPENDED"
        quantity_a = Decimal((await get_customer_paper_positions(customer_id=capacity_customer_a))[0]["quantity"])
        quantity_b = Decimal((await get_customer_paper_positions(customer_id=capacity_customer_b))[0]["quantity"])
        # 1000 units * 0.0005 participation = 0.5 shared units across both tenants.
        assert quantity_a + quantity_b <= Decimal("0.500000000000000000")
        assert quantity_a == Decimal("0.400000000000000000")
        assert quantity_b == Decimal("0.100000000000000000")

        # Simulate a crash after order intent commit but before fill commit.
        recovery_customer = 99232
        recovery_at = capacity_fill_at + timedelta(minutes=5)
        recovery_req, recovery_frame, recovery_context = _market_fixture(timestamp=recovery_at, timeframe="1m")
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
                "exchange": str(recovery_req.exchange).lower(),
                "market_type": "spot",
                "order_role": "ENTRY",
                "timeframe": str(recovery_req.timeframe),
                "signal_bar_timestamp": signal_bar,
                "submission_bar_at": signal_bar,
                "data_sha256": recovery_frame.attrs["data_provenance"]["sha256"],
                "stop_loss_price": "95",
                "take_profit_price": "110",
            },
        )
        assert order["status"] == "APPENDED"
        waiting = await recover_customer_paper_spot_flow(
            customer_id=recovery_customer, req=recovery_req,
            df=recovery_frame, market_context=recovery_context,
        )
        assert waiting["status"] == "WAITING_FOR_NEXT_COMPLETED_BAR"
        assert await get_customer_paper_positions(customer_id=recovery_customer) == []
        recovery_next_at = recovery_at + timedelta(minutes=1)
        recovery_req2, recovery_frame2, recovery_context2 = _market_fixture(timestamp=recovery_next_at, timeframe="1m")
        recovered = await recover_customer_paper_spot_flow(
            customer_id=recovery_customer, req=recovery_req2,
            df=recovery_frame2, market_context=recovery_context2,
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
    assert "COALESCE(mb.bar_at::text, o.metadata_json ->> 'signal_bar_timestamp', '') AS last_event_bar_key" in block
    assert "LEFT JOIN customer_paper.market_bar_budgets AS mb" in block
    assert "entry_event.quantity_before = 0" in block
    assert "entry_order.side = 'BUY'" in block
    assert "entry_order.metadata_json ->> 'stop_loss_price'" in block
    assert "entry_order.metadata_json ->> 'take_profit_price'" in block
    assert '"last_event_bar_key": _canonical_timestamp_text(row["last_event_bar_key"])' in block
    assert '"stop_loss_price": str(row["stop_loss_price"] or "")' in block
    assert '"take_profit_price": str(row["take_profit_price"] or "")' in block


def test_server_paper_context_measures_closed_bar_age_from_bar_close():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    start = source.index("def _server_paper_market_context(")
    end = source.index("\n\ndef ", start)
    block = source[start:end]
    assert "last_bar_ms + timeframe_ms > now_ms" in block
    assert "completed_bar_age_seconds = (now_ms - (last_bar_ms + timeframe_ms)) / 1000.0" in block
    assert "if completed_bar_age_seconds > max_bar_age_seconds:" in block


def _async_value(value):
    async def result(**_kwargs):
        return value
    return result


def _paper_fill_test_context():
    import app.customer_paper_flow as flow

    first = pd.Timestamp.now(tz="UTC").floor("h")
    second = first + pd.Timedelta(hours=1)
    state = {
        "order_id": "order-id-1", "status": "SUBMITTED",
        "remaining_quantity": "2", "side": "BUY", "venue": "binance",
        "symbol": "BTC/USDT", "currency": "USDT",
    }
    order = {
        "client_order_id": "paper-order-1",
        "metadata": {
            "signal_bar_timestamp": first.isoformat(), "submission_bar_at": first.isoformat(),
            "timeframe": "1h", "exchange": "binance", "market_type": "spot",
        },
    }
    market = {
        "bar_timestamp": second.isoformat(),
        "quote_timestamp": (second + pd.Timedelta(seconds=2)).isoformat(),
        "bar_data_sha256": "b" * 64, "data_sha256": "d" * 64,
        "base_volume": "1000", "bid": Decimal("119.9"), "ask": Decimal("120"),
        "spread_bps": Decimal("8.3"), "currency": "USDT",
    }
    return flow, state, order, market


def test_paper_protective_exit_finds_a_breach_before_the_latest_candle(monkeypatch):
    import app.customer_paper_flow as flow

    now = pd.Timestamp.now(tz="UTC").floor("h")
    prior, breach, latest = now - pd.Timedelta(hours=3), now - pd.Timedelta(hours=2), now - pd.Timedelta(hours=1)
    frame = pd.DataFrame(
        {"open": [100.0, 96.0, 100.0], "high": [101.0, 101.0, 101.0],
         "low": [99.0, 94.0, 99.0], "close": [100.0, 96.0, 100.0],
         "volume": [1000.0, 1000.0, 1000.0]},
        index=pd.DatetimeIndex([prior, breach, latest]),
    )
    req = SimpleNamespace(exchange="binance", symbol="BTC/USDT", timeframe="1h")
    market = {"bar_timestamp": latest.isoformat(), "currency": "USDT", "data_sha256": "d" * 64}
    monkeypatch.setattr(flow, "get_customer_paper_positions", _async_value([{
        "venue": "binance", "symbol": "BTC/USDT", "quantity": "1",
        "timeframe": "1h", "last_event_bar_key": prior.isoformat(),
        "stop_loss_price": "95", "take_profit_price": "110",
    }]))
    captured = {}

    async def submit(**kwargs):
        captured.update(kwargs)
        return {"status": "APPENDED", "client_order_id": kwargs["client_order_id"]}

    async def pending(**kwargs):
        return [{
            "client_order_id": captured["client_order_id"], "venue": "binance",
            "symbol": "BTC/USDT", "side": "SELL", "status": "SUBMITTED",
            "metadata": captured["metadata"],
        }]

    async def wait_next(customer_id, order, market_context, df):
        return {"status": "WAITING_FOR_NEXT_COMPLETED_BAR", "client_order_id": order["client_order_id"]}

    monkeypatch.setattr(flow, "submit_customer_paper_order", submit)
    monkeypatch.setattr(flow, "list_open_customer_paper_orders", pending)
    monkeypatch.setattr(flow, "_fill_open_order", wait_next)

    result = asyncio.run(flow._protective_exit(7, req, frame, market))
    assert result["status"] == "WAITING_FOR_NEXT_COMPLETED_BAR"
    assert captured["side"] == "SELL"
    assert captured["metadata"]["exchange"] == "binance"
    assert captured["metadata"]["market_type"] == "spot"
    assert captured["metadata"]["exit_reason"] == "STOP_LOSS"
    assert captured["metadata"]["signal_bar_timestamp"] == breach.isoformat()
    assert captured["metadata"]["submission_bar_at"] == latest.isoformat()


def test_pending_paper_buy_is_resized_at_the_fill_quote_to_available_cash(monkeypatch):
    import app.customer_paper_flow as flow

    _, state, order, market = _paper_fill_test_context()
    captured = {}
    monkeypatch.setattr(flow, "get_customer_paper_order", _async_value(state))
    monkeypatch.setattr(flow, "get_customer_paper_positions", _async_value([]))
    monkeypatch.setattr(flow, "register_customer_paper_market_bar", _async_value({
        "bar_key": "bar-key-cash-cap", "bar_at": market["bar_timestamp"],
        "capacity": "10", "available": "10",
    }))
    monkeypatch.setattr(flow, "get_customer_paper_fill_for_bar", _async_value(None))
    monkeypatch.setattr(flow, "get_customer_paper_balances", _async_value([{
        "account_code": "PAPER_CASH", "currency": "USDT", "balance": "100",
    }]))
    monkeypatch.setattr(
        flow, "_fill_price_and_fee",
        lambda side, quantity, context: (
            "120", "0", {"fee_bps_assumption": "0", "slippage_bps_assumption": "0",
                         "cost_model_status": "TEST_FIXTURE"},
        ),
    )

    async def append_fill(**kwargs):
        captured.update(kwargs)
        return {"status": "APPENDED", "fill_id": "fill-1"}

    monkeypatch.setattr(flow, "record_customer_paper_fill", append_fill)
    result = asyncio.run(flow._fill_open_order(
        customer_id=1001, order=order, market=market, df=pd.DataFrame()
    ))
    assert result["fill"]["status"] == "APPENDED"
    assert captured["quantity"] == "0.833333333333333333"
    assert Decimal(captured["quantity"]) * Decimal(captured["price"]) <= Decimal("100")


def test_same_bar_paper_fill_retry_replays_immutable_fill_without_resubmitting(monkeypatch):
    import app.customer_paper_flow as flow

    _, state, order, market = _paper_fill_test_context()
    replay = {
        "status": "REPLAYED", "fill_id": "existing-fill", "order_id": state["order_id"],
        "idempotency_key": "existing-key", "bar_key": "bar-key-replay",
        "quantity": "0.5", "price": "120", "fee_amount": "0",
    }
    monkeypatch.setattr(flow, "get_customer_paper_order", _async_value(state))
    monkeypatch.setattr(flow, "get_customer_paper_positions", _async_value([]))
    monkeypatch.setattr(flow, "register_customer_paper_market_bar", _async_value({
        "bar_key": "bar-key-replay", "bar_at": market["bar_timestamp"],
        "capacity": "10", "available": "9.5",
    }))
    monkeypatch.setattr(flow, "get_customer_paper_fill_for_bar", _async_value(replay))

    async def must_not_append(**kwargs):
        raise AssertionError("same-bar retry must replay the immutable fill")

    monkeypatch.setattr(flow, "record_customer_paper_fill", must_not_append)
    result = asyncio.run(flow._fill_open_order(
        customer_id=1002, order=order, market=market, df=pd.DataFrame()
    ))
    assert result["status"] == "REPLAYED"
    assert result["fill"]["fill_id"] == "existing-fill"


def test_paper_buy_that_is_no_longer_affordable_expires_instead_of_stalling(monkeypatch):
    import app.customer_paper_flow as flow

    _, state, order, market = _paper_fill_test_context()
    monkeypatch.setattr(flow, "get_customer_paper_order", _async_value(state))
    monkeypatch.setattr(flow, "get_customer_paper_positions", _async_value([]))
    monkeypatch.setattr(flow, "register_customer_paper_market_bar", _async_value({
        "bar_key": "bar-key-no-cash", "bar_at": market["bar_timestamp"],
        "capacity": "10", "available": "10",
    }))
    monkeypatch.setattr(flow, "get_customer_paper_fill_for_bar", _async_value(None))
    monkeypatch.setattr(flow, "get_customer_paper_balances", _async_value([{
        "account_code": "PAPER_CASH", "currency": "USDT", "balance": "0",
    }]))
    monkeypatch.setattr(
        flow, "_fill_price_and_fee",
        lambda side, quantity, context: (
            "120", "0", {"fee_bps_assumption": "0", "slippage_bps_assumption": "0",
                         "cost_model_status": "TEST_FIXTURE"},
        ),
    )
    expired = {}

    async def expire(**kwargs):
        expired.update(kwargs)
        return {"status": "EXPIRED"}

    async def must_not_append(**kwargs):
        raise AssertionError("an unaffordable order must not create a fill")

    monkeypatch.setattr(flow, "expire_customer_paper_order", expire)
    monkeypatch.setattr(flow, "record_customer_paper_fill", must_not_append)
    result = asyncio.run(flow._fill_open_order(
        customer_id=1003, order=order, market=market, df=pd.DataFrame()
    ))
    assert result["status"] == "EXPIRED_INSUFFICIENT_PAPER_CASH"
    assert expired["client_order_id"] == order["client_order_id"]


def test_pending_order_reader_exposes_market_identity_and_immutable_metadata():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "customer_paper_lifecycle.py").read_text(encoding="utf-8")
    start = source.index("async def list_open_customer_paper_orders(")
    end = source.index("\n\nasync def ", start)
    block = source[start:end]
    assert "o.client_order_id, o.venue, o.market_type, o.symbol, o.side" in block
    assert "o.quantity, o.metadata_json" in block
    assert '"venue": str(row["venue"]).lower()' in block
    assert '"market_type": str(row["market_type"])' in block
    assert '"metadata": metadata' in block


def test_manual_paper_initialization_reuses_the_configured_seed_helper():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "customer_paper_api.py").read_text(encoding="utf-8")
    start = source.index('@router.post("/account/initialize")')
    end = source.index('\n    @router.get("/balances")', start)
    block = source[start:end]
    assert "ensure_customer_paper_account" in block
    assert "settings.customer_paper_initial_currency" in block
    assert "customer-paper-virtual-seed-v1" not in block
    assert "real_funds_used" in block and "virtual_only" in block


def test_concurrent_paper_cash_spend_expires_unaffordable_pending_buy(monkeypatch):
    import app.customer_paper_flow as flow
    from app.customer_paper_lifecycle import CustomerPaperOrderConflict

    _, state, order, market = _paper_fill_test_context()
    monkeypatch.setattr(flow, "get_customer_paper_order", _async_value(state))
    monkeypatch.setattr(flow, "get_customer_paper_positions", _async_value([]))
    monkeypatch.setattr(flow, "register_customer_paper_market_bar", _async_value({
        "bar_key": "bar-key-cash-race", "bar_at": market["bar_timestamp"],
        "capacity": "10", "available": "10",
    }))
    monkeypatch.setattr(flow, "get_customer_paper_fill_for_bar", _async_value(None))
    monkeypatch.setattr(flow, "get_customer_paper_balances", _async_value([{
        "account_code": "PAPER_CASH", "currency": "USDT", "balance": "100",
    }]))
    monkeypatch.setattr(
        flow, "_fill_price_and_fee",
        lambda side, quantity, context: (
            "120", "0", {"fee_bps_assumption": "0", "slippage_bps_assumption": "0",
                         "cost_model_status": "TEST_FIXTURE"},
        ),
    )

    async def cash_was_spent_by_another_fill(**kwargs):
        raise CustomerPaperOrderConflict("paper fill would overdraw the customer's paper cash")

    expired = {}

    async def expire(**kwargs):
        expired.update(kwargs)
        return {"status": "EXPIRED"}

    monkeypatch.setattr(flow, "record_customer_paper_fill", cash_was_spent_by_another_fill)
    monkeypatch.setattr(flow, "expire_customer_paper_order", expire)
    result = asyncio.run(flow._fill_open_order(
        customer_id=1004, order=order, market=market, df=pd.DataFrame()
    ))

    assert result["status"] == "EXPIRED_INSUFFICIENT_PAPER_CASH"
    assert expired["client_order_id"] == order["client_order_id"]
    assert expired["idempotency_key"].startswith("paper-cash-race-")


def test_pending_paper_sell_expires_when_position_was_closed_elsewhere(monkeypatch):
    import app.customer_paper_flow as flow

    _, state, order, market = _paper_fill_test_context()
    state["side"] = "SELL"
    monkeypatch.setattr(flow, "get_customer_paper_order", _async_value(state))
    monkeypatch.setattr(flow, "get_customer_paper_positions", _async_value([]))
    expired = {}

    async def expire(**kwargs):
        expired.update(kwargs)
        return {"status": "EXPIRED"}

    async def must_not_register(**kwargs):
        raise AssertionError("no market budget is needed for a SELL with no open position")

    monkeypatch.setattr(flow, "expire_customer_paper_order", expire)
    monkeypatch.setattr(flow, "register_customer_paper_market_bar", must_not_register)
    result = asyncio.run(flow._fill_open_order(
        customer_id=1005, order=order, market=market, df=pd.DataFrame()
    ))

    assert result["status"] == "EXPIRED_NO_OPEN_POSITION"
    assert result["fill"]["status"] == "EXPIRED"
    assert expired["client_order_id"] == order["client_order_id"]


def test_manual_paper_sell_recovery_caps_against_the_order_venue():
    """Manual sell recovery must never consume a position from a different venue."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "customer_paper_api.py").read_text(encoding="utf-8")
    assert 'str(position.get("venue") or "").lower() == str(order.get("venue") or "").lower()' in source
    assert 'str(position.get("venue") or "").lower() == "binance"' not in source

def test_paper_fill_money_fields_round_conservatively_to_numeric_38_18(monkeypatch):
    import app.customer_paper_flow as flow

    monkeypatch.setattr(settings, "research_taker_bps", 10)
    monkeypatch.setattr(settings, "research_slippage_bps", 0)
    market = {
        "ask": Decimal("100.0000000000000000009"),
        "bid": Decimal("99.9999999999999999999"),
        "spread_bps": Decimal("0.1"),
        "quote_timestamp": "2026-10-21T00:00:00+00:00",
        "data_sha256": "a" * 64,
    }
    quantity = Decimal("0.1234567890123456789")

    buy_price, buy_fee, _ = flow._fill_price_and_fee("BUY", quantity, market)
    sell_price, sell_fee, _ = flow._fill_price_and_fee("SELL", quantity, market)

    # Buy-side rounding never improves the simulated price; sell-side rounding never does.
    assert Decimal(buy_price) == Decimal("100.000000000000000001")
    assert Decimal(sell_price) == Decimal("99.999999999999999999")
    assert Decimal(buy_fee) == Decimal("0.012345678901234568")
    assert Decimal(sell_fee) == Decimal("0.012345678901234568")
    assert Decimal(buy_fee).as_tuple().exponent == -18
    assert Decimal(sell_fee).as_tuple().exponent == -18



def test_fractional_paper_fill_at_supported_quantity_uses_rounded_money_values(monkeypatch):
    """Regression for a realistic 18-place quantity paired with a non-round quote."""
    import app.customer_paper_flow as flow

    monkeypatch.setattr(settings, "research_taker_bps", 10)
    monkeypatch.setattr(settings, "research_slippage_bps", 0)
    market = {
        "ask": Decimal("123.4567891234567891234"),
        "bid": Decimal("123.4567891234567891229"),
        "spread_bps": Decimal("0.0000000000000001"),
        "quote_timestamp": "2026-10-23T00:00:00+00:00",
        "data_sha256": "b" * 64,
    }
    quantity = Decimal("0.016666666666666666")

    buy_price, buy_fee, _ = flow._fill_price_and_fee("BUY", quantity, market)
    sell_price, sell_fee, _ = flow._fill_price_and_fee("SELL", quantity, market)

    assert Decimal(buy_price) == Decimal("123.456789123456789124")
    assert Decimal(sell_price) == Decimal("123.456789123456789122")
    for submitted_value in (buy_price, buy_fee, sell_price, sell_fee):
        parsed = Decimal(submitted_value)
        assert parsed.is_finite()
        assert parsed.as_tuple().exponent == -18

def test_autonomous_paper_order_waits_for_configured_fill_latency(monkeypatch):
    import app.customer_paper_flow as flow

    _, state, order, market = _paper_fill_test_context()
    monkeypatch.setattr(flow, "get_customer_paper_order", _async_value(state))
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 2)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 5)

    result = asyncio.run(flow._fill_open_order(
        customer_id=12001, order=order, market=market, df=pd.DataFrame()
    ))
    assert result["status"] == "WAITING_FOR_FILL_LATENCY"
    assert result["elapsed_bars"] == 1


def test_autonomous_paper_order_expires_at_ttl_boundary(monkeypatch):
    import app.customer_paper_flow as flow

    _, state, order, market = _paper_fill_test_context()
    market["bar_timestamp"] = (
        pd.Timestamp(order["metadata"]["submission_bar_at"]) + pd.Timedelta(hours=3)
    ).isoformat()
    market["quote_timestamp"] = (
        pd.Timestamp(market["bar_timestamp"]) + pd.Timedelta(seconds=2)
    ).isoformat()
    monkeypatch.setattr(flow, "get_customer_paper_order", _async_value(state))
    monkeypatch.setattr(settings, "paper_fill_latency_bars", 1)
    monkeypatch.setattr(settings, "paper_order_ttl_bars", 3)
    expired = {}

    async def expire(**kwargs):
        expired.update(kwargs)
        return {"status": "EXPIRED"}

    monkeypatch.setattr(flow, "expire_customer_paper_order", expire)
    result = asyncio.run(flow._fill_open_order(
        customer_id=12002, order=order, market=market, df=pd.DataFrame()
    ))
    assert result["status"] == "EXPIRED_TTL"
    assert result["elapsed_bars"] == 3
    assert expired["client_order_id"] == order["client_order_id"]


def test_paper_protective_exit_fails_closed_when_intervening_candle_is_missing(monkeypatch):
    import app.customer_paper_flow as flow

    now = pd.Timestamp.now(tz="UTC").floor("h")
    prior, latest = now - pd.Timedelta(hours=3), now - pd.Timedelta(hours=1)
    frame = pd.DataFrame(
        {"open": [100.0, 100.0], "high": [101.0, 101.0],
         "low": [99.0, 99.0], "close": [100.0, 100.0],
         "volume": [1000.0, 1000.0]},
        index=pd.DatetimeIndex([prior, latest]),
    )
    req = SimpleNamespace(exchange="binance", symbol="BTC/USDT", timeframe="1h")
    market = {"bar_timestamp": latest.isoformat(), "currency": "USDT", "data_sha256": "d" * 64}
    monkeypatch.setattr(flow, "get_customer_paper_positions", _async_value([{
        "venue": "binance", "symbol": "BTC/USDT", "quantity": "1",
        "timeframe": "1h", "last_event_bar_key": prior.isoformat(),
        "stop_loss_price": "95", "take_profit_price": "110",
    }]))
    with pytest.raises(CustomerPaperFlowBlocked, match="history is incomplete"):
        asyncio.run(flow._protective_exit(12003, req, frame, market))


def test_tiny_positive_paper_sell_quote_never_writes_zero_price_fill(monkeypatch):
    import app.customer_paper_flow as flow

    _, state, order, market = _paper_fill_test_context()
    state["side"] = "SELL"
    market["bid"] = Decimal("0.0000000000000000009")
    monkeypatch.setattr(flow, "get_customer_paper_order", _async_value(state))
    monkeypatch.setattr(flow, "get_customer_paper_positions", _async_value([{
        "venue": "binance", "symbol": "BTC/USDT", "quantity": "1",
    }]))
    monkeypatch.setattr(flow, "register_customer_paper_market_bar", _async_value({
        "bar_key": "tiny-sell-bar", "bar_at": market["bar_timestamp"],
        "capacity": "10", "available": "10",
    }))
    monkeypatch.setattr(flow, "get_customer_paper_fill_for_bar", _async_value(None))

    async def must_not_write(**kwargs):
        raise AssertionError("zero-price SELL must never be written")

    monkeypatch.setattr(flow, "record_customer_paper_fill", must_not_write)
    result = asyncio.run(flow._fill_open_order(
        customer_id=12004, order=order, market=market, df=pd.DataFrame()
    ))
    assert result["status"] == "WAITING_FOR_PRICE_PRECISION"
    assert result["fill"]["status"] == "NOT_FILLED"



def test_conservative_buy_sizing_price_matches_actual_paper_fill_price(monkeypatch):
    import app.customer_paper_flow as flow

    monkeypatch.setattr(settings, "research_slippage_bps", 37)
    monkeypatch.setattr(settings, "research_taker_bps", 10)
    context = {
        "ask": "120.123456789012345678",
        "bid": "120.0",
        "spread_bps": "10.2",
        "quote_timestamp_utc": "2026-10-23T09:00:00+00:00",
        "ohlcv_sha256": "c" * 64,
    }
    price = flow.conservative_customer_paper_buy_price(context)
    expected, _fee, _costs = flow._fill_price_and_fee("BUY", Decimal("1"), {
        "ask": Decimal(context["ask"]),
        "bid": Decimal(context["bid"]),
        "spread_bps": Decimal(context["spread_bps"]),
        "quote_timestamp": context["quote_timestamp_utc"],
        "data_sha256": context["ohlcv_sha256"],
    })
    assert price == Decimal(expected)
    assert price > Decimal(context["ask"])


@pytest.mark.postgres
@pytest.mark.skipif(
    not (os.getenv("ATLAS_PAPER_TEST_DATABASE_URL") and os.getenv("ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL")),
    reason="requires CI-provisioned isolated customer paper and primary PostgreSQL databases",
)
def test_customer_paper_buy_fill_enforces_shared_group_cap_at_actual_price(monkeypatch):
    """Concurrent same-group fills cannot both consume the same pre-fill headroom."""
    from app.correlation_risk import risk_group

    monkeypatch.setattr(settings, "database_url", os.environ["ATLAS_PAPER_PRIMARY_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_database_url", os.environ["ATLAS_PAPER_TEST_DATABASE_URL"])
    monkeypatch.setattr(settings, "customer_paper_ledger_enabled", True)
    monkeypatch.setattr(settings, "customer_paper_shared_database_enabled", False)
    monkeypatch.setattr(settings, "customer_paper_initial_balance", 10000.0)
    monkeypatch.setattr(settings, "customer_paper_initial_currency", "USDT")
    monkeypatch.setattr(settings, "paper_max_bar_volume_participation", 0.01)
    monkeypatch.setattr(settings, "correlation_group_cap_usd", 50.0)
    monkeypatch.setattr(settings, "research_slippage_bps", 0)
    monkeypatch.setattr(settings, "research_taker_bps", 0)
    # This test isolates the fill-time exposure gate; same-day entry discipline is
    # exercised by separate tests and should not block the two pending intents here.
    monkeypatch.setattr(settings, "discipline_enabled", False)

    async def run():
        first_at = datetime.now(timezone.utc) - timedelta(hours=8)
        # Use a fresh customer ID per run so rerunning the test cannot inherit old positions.
        customer_id = 990000000 + int(first_at.timestamp()) % 90000000

        eth_req, eth_frame_1, eth_context_1 = _market_fixture(
            timestamp=first_at, symbol="ETH/USDT", bid=100.0, ask=100.1,
        )
        sol_req, sol_frame_1, sol_context_1 = _market_fixture(
            timestamp=first_at, symbol="SOL/USDT", bid=100.0, ask=100.1,
        )
        submitted_eth = await execute_customer_paper_spot_signal(
            customer_id=customer_id, req=eth_req, df=eth_frame_1,
            market_context=eth_context_1, side="BUY", quantity="0.3",
            stop_loss_price="95", take_profit_price="110",
            strategy="group-cap-concurrency-regression",
        )
        submitted_sol = await execute_customer_paper_spot_signal(
            customer_id=customer_id, req=sol_req, df=sol_frame_1,
            market_context=sol_context_1, side="BUY", quantity="0.3",
            stop_loss_price="95", take_profit_price="110",
            strategy="group-cap-concurrency-regression",
        )
        assert submitted_eth["status"] == "WAITING_FOR_NEXT_COMPLETED_BAR"
        assert submitted_sol["status"] == "WAITING_FOR_NEXT_COMPLETED_BAR"

        # Both intents are pending before either fill starts. They race into the same
        # L1_BETA budget on the next bar; the group advisory lock must serialize them.
        second_at = first_at + timedelta(hours=1)
        eth_req_2, eth_frame_2, eth_context_2 = _market_fixture(
            timestamp=second_at, symbol="ETH/USDT", bid=100.0, ask=100.1,
        )
        sol_req_2, sol_frame_2, sol_context_2 = _market_fixture(
            timestamp=second_at, symbol="SOL/USDT", bid=100.0, ask=100.1,
        )
        eth_result, sol_result = await asyncio.gather(
            recover_customer_paper_spot_flow(
                customer_id=customer_id, req=eth_req_2,
                df=eth_frame_2, market_context=eth_context_2,
            ),
            recover_customer_paper_spot_flow(
                customer_id=customer_id, req=sol_req_2,
                df=sol_frame_2, market_context=sol_context_2,
            ),
        )
        assert eth_result is not None and sol_result is not None
        eth_fill = eth_result["execution"]["fill"]
        sol_fill = sol_result["execution"]["fill"]
        assert eth_fill["status"] == sol_fill["status"] == "APPENDED"
        filled_quantities = [
            Decimal(eth_fill["filled_quantity"]),
            Decimal(sol_fill["filled_quantity"]),
        ]
        # At the $100.10 fill price, only one 0.3-unit order fits in full under $50.
        assert max(filled_quantities) == Decimal("0.300000000000000000")
        assert Decimal("0") < min(filled_quantities) < Decimal("0.3")

        positions = await get_customer_paper_positions(customer_id=customer_id)
        group_notional = sum(
            (
                abs(Decimal(str(p["quantity"]))) * Decimal(str(p["average_entry_price"]))
                for p in positions
                if risk_group(str(p["symbol"])) == "L1_BETA"
            ),
            Decimal("0"),
        )
        assert group_notional <= Decimal("50")
        assert {p["symbol"] for p in positions} == {"ETH/USDT", "SOL/USDT"}

    asyncio.run(run())


def test_customer_paper_buy_discipline_counts_any_filled_order_and_checks_cooldown_across_midnight():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "customer_paper_lifecycle.py").read_text(
        encoding="utf-8"
    )
    daily_start = source.index("todays_entries =")
    daily_end = source.index("if int(todays_entries", daily_start)
    daily_query = source[daily_start:daily_end]
    cooldown_start = source.index("last_entry =")
    cooldown_end = source.index("if last_entry is not None", cooldown_start)
    cooldown_query = source[cooldown_start:cooldown_end]

    for query in (daily_query, cooldown_query):
        assert "EXISTS (" in query
        assert "FROM customer_paper.fills f" in query
        assert "f.customer_id=o.customer_id AND f.order_id=o.id" in query
        assert "latest.event_type IN ('SUBMITTED','PARTIALLY_FILLED','FILLED')" in query
    assert "o.created_at >= :day_start" not in cooldown_query
    assert "cooldown_start = now - timedelta(" in source
    assert "o.created_at >= :cooldown_start" in cooldown_query
    # A 90-second cooldown starting at 00:00:30 includes a 23:59:30 entry.
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 24, 0, 0, 30, tzinfo=timezone.utc)
    cooldown_start_at = now - timedelta(seconds=90)
    prior_entry = datetime(2026, 10, 23, 23, 59, 30, tzinfo=timezone.utc)
    assert cooldown_start_at <= prior_entry < now
