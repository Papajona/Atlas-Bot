import asyncio
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app import main as main_module
from app import strategy_engine
from app.execution import customer_paper_execution_blocked
from app.risk_governor import evaluate_trade
from app.schemas import ExecuteRequest
from app.strategy_engine import StrategyConfig, strategy_backtest


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_customer_paper_execution_guard_is_behavioural():
    assert customer_paper_execution_blocked(123, True) is True
    assert customer_paper_execution_blocked(123, False) is False
    assert customer_paper_execution_blocked(None, True) is False


def test_customer_start_gates_customer_paper_before_ai_work():
    source = _source("app/main.py")
    start = source[source.index('@app.post("/api/customer/bot/start")'):]
    paper_mode = start.index("needs_customer_paper = bool(paper_mode and profile.id is not None)")
    unsupported_asset_gate = start.index('if req.asset != "crypto":', paper_mode)
    config_gate = start.index("if not _customer_paper_configuration_ready():", unsupported_asset_gate)
    candidate_setup = start.index("candidate = None", config_gate)
    preflight = start.index("paper_preflight = await _customer_paper_isolation_preflight()", candidate_setup)
    analysis = start.index("asyncio.to_thread(start_bot_decision")
    ai_review = start.index("dual_ai_trade_safety_review(")

    assert paper_mode < unsupported_asset_gate < config_gate < candidate_setup < preflight < analysis < ai_review
    assert "no analysis or AI review was run" in start[paper_mode:analysis]
    assert "CUSTOMER_PAPER_LEDGER_ENABLED" in source
    assert 'customer_paper_ledger_enabled: bool = False' in _source("app/config.py")
    cycle = source[source.index("async def _run_persisted_adaptive_bot_cycle"): ]
    assert "simulation_ledger_required" in cycle
    assert 'bot.status = "STOPPED"' in cycle


def test_risk_governor_blocks_non_finite_protective_levels():
    common = dict(side="sell", price=100.0, quantity=1.0, live=True,
                  signal={}, spread_bps=1.0)
    nan_stop = evaluate_trade(**common, stop_loss_price=float("nan"), take_profit_price=90.0)
    inf_stop = evaluate_trade(**common, stop_loss_price=float("inf"), take_profit_price=90.0)
    nan_target = evaluate_trade(**common, stop_loss_price=110.0, take_profit_price=float("nan"))
    assert nan_stop.action == "BLOCK" and "invalid_stop_loss" in nan_stop.reasons
    assert inf_stop.action == "BLOCK" and "invalid_stop_loss" in inf_stop.reasons
    assert nan_target.action == "BLOCK" and "invalid_take_profit" in nan_target.reasons


@pytest.mark.parametrize("field", ["quantity", "price", "score", "long_probability", "short_probability", "flat_probability", "stop_loss_price", "take_profit_price"])
def test_execute_request_rejects_non_finite_floats(field):
    value = {"symbol": "BTC/USDT:USDT", "side": "buy", "quantity": 1.0, "price": 1.0, field: float("inf")}
    with pytest.raises(ValidationError):
        ExecuteRequest(**value)


def test_strategy_backtest_ignores_optional_session_nans_and_counts_entries(monkeypatch):
    idx = pd.date_range("2025-01-01", periods=7, freq="h", tz="UTC")
    opens = np.asarray([100, 101, 102, 103, 104, 103, 102], dtype=float)
    frame = pd.DataFrame({
        "open": opens,
        "high": opens + 1.0,
        "low": opens - 1.0,
        "close": opens,
        "volume": np.full(len(opens), 100.0),
    }, index=idx)
    signals = pd.DataFrame({
        "ensemble": [1, 1, 1, 0, 0, -1, -1],
        "atr": [1.0] * 7,
        "realized_vol": [0.5, 0.25, 0.2, 0.3, 0.3, 0.5, 0.25],
        "london_range_high": [np.nan] * 7,
    }, index=idx)
    signals.attrs["annualization_factor"] = 24.0
    monkeypatch.setattr(strategy_engine, "strategy_signals", lambda *args, **kwargs: signals)
    result = strategy_backtest(
        frame,
        StrategyConfig(target_vol_annual=0.3, max_leverage=3.0, signal_threshold=0.1),
    )
    assert result["validated_bars"] == 7
    assert result["trades"] == 2


def test_safe_audit_does_not_propagate_database_failure(monkeypatch):
    async def broken_audit(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(main_module, "_audit", broken_audit)

    async def run():
        await main_module._safe_audit("TEST_WORKER_FAILURE", {"error": "simulated"})

    asyncio.run(run())


def test_worker_supervisor_restarts_failed_loop(monkeypatch):
    attempts = {"count": 0}

    async def broken_loop():
        attempts["count"] += 1
        raise RuntimeError("simulated loop failure")

    async def cancel_on_backoff(_seconds):
        raise asyncio.CancelledError()

    monkeypatch.setattr(main_module.asyncio, "sleep", cancel_on_backoff)

    async def run():
        try:
            await main_module._supervise_worker_loop(broken_loop, "test-loop")
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert attempts["count"] == 1
    assert main_module._worker_loop_health["test-loop"]["restart_count"] == 1
    assert main_module._worker_loop_health["test-loop"]["status"] == "RESTARTING"


def test_trade_learning_replay_is_started_only_when_enabled():
    source = _source("app/main.py")
    startup = source[source.index("async def startup()"):source.index("async def shutdown()")]
    assert "if settings.trade_learning_enabled:" in startup
    assert "_trade_learning_replay_loop" in startup


@pytest.mark.parametrize(
    "side,take_profit_price",
    [
        ("buy", 0.0),
        ("buy", -1.0),
        ("sell", 0.0),
        ("sell", -1.0),
    ],
)
def test_risk_governor_blocks_non_positive_take_profit_prices(side, take_profit_price):
    decision = evaluate_trade(
        side=side,
        price=100.0,
        quantity=1.0,
        live=False,
        stop_loss_price=90.0 if side == "buy" else 110.0,
        take_profit_price=take_profit_price,
        signal={},
    )
    assert decision.action == "BLOCK"
    assert "invalid_take_profit" in decision.reasons


@pytest.mark.parametrize("timeframe", ["4h", "1d"])
def test_customer_paper_quality_uses_closed_candle_age(timeframe, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "min_bars", 1)
    monkeypatch.setattr(settings, "adaptive_require_established_crypto", False)
    monkeypatch.setattr(settings, "stale_data_minutes", 180)
    now = pd.Timestamp.now(tz="UTC")
    last_open = now - pd.Timedelta(timeframe) - pd.Timedelta(seconds=30)
    frame = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [1000.0]},
        index=pd.DatetimeIndex([last_open]),
    )

    safe, reasons = main_module._customer_paper_market_data_quality(frame, timeframe, "crypto")

    assert safe, reasons
    assert "stale_market_data" not in reasons


def test_customer_paper_quality_rejects_a_future_incomplete_close(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "min_bars", 1)
    monkeypatch.setattr(settings, "adaptive_require_established_crypto", False)
    monkeypatch.setattr(settings, "stale_data_minutes", 180)
    now = pd.Timestamp.now(tz="UTC")
    last_open = now - pd.Timedelta("1h") + pd.Timedelta(seconds=30)
    frame = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [1000.0]},
        index=pd.DatetimeIndex([last_open]),
    )

    safe, reasons = main_module._customer_paper_market_data_quality(frame, "1h", "crypto")

    assert not safe
    assert "incomplete_latest_bar" in reasons


def test_paper_protective_exit_finds_a_breach_before_the_latest_candle(monkeypatch):
    import app.customer_paper_flow as flow

    now = pd.Timestamp.now(tz="UTC").floor("h")
    prior = now - pd.Timedelta(hours=3)
    breach = now - pd.Timedelta(hours=2)
    latest = now - pd.Timedelta(hours=1)
    frame = pd.DataFrame(
        {
            "open": [100.0, 96.0, 100.0],
            "high": [101.0, 101.0, 101.0],
            "low": [99.0, 94.0, 99.0],
            "close": [100.0, 96.0, 100.0],
            "volume": [1000.0, 1000.0, 1000.0],
        },
        index=pd.DatetimeIndex([prior, breach, latest]),
    )
    req = SimpleNamespace(exchange="binance", symbol="BTC/USDT", timeframe="1h")
    market = {
        "bar_timestamp": latest.isoformat(),
        "currency": "USDT",
        "data_sha256": "d" * 64,
    }
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
            "client_order_id": captured["client_order_id"],
            "venue": "binance", "symbol": "BTC/USDT", "side": "SELL",
            "status": "SUBMITTED", "metadata": captured["metadata"],
        }]

    async def wait_next(**kwargs):
        return {"status": "WAITING_FOR_NEXT_COMPLETED_BAR", "client_order_id": kwargs["order"]["client_order_id"]}

    monkeypatch.setattr(flow, "submit_customer_paper_order", submit)
    monkeypatch.setattr(flow, "list_open_customer_paper_orders", pending)
    monkeypatch.setattr(flow, "_fill_open_order", wait_next)

    async def run():
        return await flow._protective_exit(7, req, frame, market)

    result = asyncio.run(run())
    assert result["status"] == "WAITING_FOR_NEXT_COMPLETED_BAR"
    assert captured["side"] == "SELL"
    assert captured["metadata"]["exchange"] == "binance"
    assert captured["metadata"]["market_type"] == "spot"
    assert captured["metadata"]["exit_reason"] == "STOP_LOSS"
    assert captured["metadata"]["signal_bar_timestamp"] == breach.isoformat()
    assert captured["metadata"]["submission_bar_at"] == latest.isoformat()


def _async_value(value):
    async def result(**_kwargs):
        return value
    return result


def test_paper_fill_metadata_includes_exchange_for_budget_validation():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "customer_paper_flow.py").read_text(encoding="utf-8")
    signal_start = source.index("async def execute_customer_paper_spot_signal(")
    exit_start = source.index("async def _protective_exit(")
    signal = source[signal_start:exit_start]
    protective = source[exit_start:source.index("async def recover_customer_paper_spot_flow(", exit_start)]
    assert '"exchange": str(req.exchange).lower()' in signal
    assert '"exchange": str(req.exchange).lower()' in protective
    assert '"market_type": "spot"' in signal
    assert '"market_type": "spot"' in protective


def _paper_fill_test_context():
    import app.customer_paper_flow as flow

    first = pd.Timestamp.now(tz="UTC").floor("h")
    second = first + pd.Timedelta(hours=1)
    req = SimpleNamespace(exchange="binance", symbol="BTC/USDT", timeframe="1h")
    state = {
        "order_id": "order-id-1",
        "status": "SUBMITTED",
        "remaining_quantity": "2",
        "side": "BUY",
        "venue": "binance",
        "symbol": "BTC/USDT",
        "currency": "USDT",
    }
    order = {
        "client_order_id": "paper-order-1",
        "metadata": {
            "signal_bar_timestamp": first.isoformat(),
            "submission_bar_at": first.isoformat(),
            "timeframe": "1h",
            "exchange": "binance",
            "market_type": "spot",
        },
    }
    market = {
        "bar_timestamp": second.isoformat(),
        "quote_timestamp": (second + pd.Timedelta(seconds=2)).isoformat(),
        "bar_data_sha256": "b" * 64,
        "data_sha256": "d" * 64,
        "base_volume": "1000",
        "bid": Decimal("119.9"),
        "ask": Decimal("120"),
        "spread_bps": Decimal("8.3"),
        "currency": "USDT",
    }
    return flow, req, state, order, market


def test_pending_paper_buy_is_resized_at_the_fill_quote_to_available_cash(monkeypatch):
    import app.customer_paper_flow as flow

    _, _req, state, order, market = _paper_fill_test_context()
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

    _, _req, state, order, market = _paper_fill_test_context()
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

    _, _req, state, order, market = _paper_fill_test_context()
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
