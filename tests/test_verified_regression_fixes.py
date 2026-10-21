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
    assert "_customer_paper_configuration_ready()" in start
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


def test_recovered_paper_trade_audit_only_emits_for_new_fill():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    guard = 'and str(((recovered.get("execution") or {}).get("fill") or {}).get("status") or "").upper() == "APPENDED"'
    assert source.count(guard) == 2


@pytest.mark.parametrize(
    ("timeframe", "time_after_close"),
    [("4h", pd.Timedelta(hours=4)), ("1d", pd.Timedelta(hours=12))],
)
def test_customer_paper_quality_allows_closed_slow_candles_with_fresh_quote_window(
    timeframe, time_after_close, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "min_bars", 1)
    monkeypatch.setattr(settings, "adaptive_require_established_crypto", False)
    monkeypatch.setattr(settings, "stale_data_minutes", 180)
    now = pd.Timestamp.now(tz="UTC")
    bar_open = now - pd.Timedelta(timeframe) - time_after_close
    frame = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [1000.0]},
        index=pd.DatetimeIndex([bar_open]),
    )

    safe, reasons = main_module._customer_paper_market_data_quality(frame, timeframe, "crypto")

    assert safe, reasons
    assert "stale_market_data" not in reasons
