import types

import numpy as np
import pandas as pd


def _run_component(monkeypatch, returns, positions):
    from app import research_engine

    idx = pd.date_range("2026-01-01", periods=len(returns), freq="h", tz="UTC")
    signals = pd.DataFrame(
        {
            "trend": positions,
            "realized_vol": np.full(len(returns), 0.20),
        },
        index=idx,
    )
    signals.attrs["annualization_factor"] = 252.0
    monkeypatch.setattr(research_engine, "strategy_signals", lambda df, cfg, asset: signals)
    monkeypatch.setattr(
        research_engine,
        "next_open_returns",
        lambda df: pd.Series(returns, index=idx),
    )

    df = pd.DataFrame(
        {
            "open": np.full(len(returns), 100.0),
            "high": np.full(len(returns), 101.0),
            "low": np.full(len(returns), 99.0),
            "close": np.full(len(returns), 100.0),
            "volume": np.full(len(returns), 100.0),
        },
        index=idx,
    )
    cfg = types.SimpleNamespace(target_vol_annual=0.20, max_leverage=1.0)
    return research_engine._component_backtest(
        df,
        "trend",
        cfg,
        taker_bps=0.0,
        slippage_bps=0.0,
        asset="commodity",
    )


def test_loss_guard_counts_closed_losing_trades_not_losing_bars(monkeypatch):
    from app import research_engine

    monkeypatch.setattr(research_engine.settings, "strategy_stoploss_guard_trades", 3)
    monkeypatch.setattr(research_engine.settings, "strategy_stoploss_guard_bars", 24)
    monkeypatch.setattr(research_engine.settings, "strategy_drawdown_halt", 1.0)
    monkeypatch.setattr(research_engine.settings, "strategy_daily_loss_halt", 1.0)

    # One continuous position has three losing bars but only one open trade.
    result = _run_component(
        monkeypatch,
        returns=[1.0, 0.999, 0.999, 0.999, 1.0],
        positions=[1.0, 1.0, 1.0, 1.0, 1.0],
    )

    # The guard must not interpret the three losing bars as three losing trades.
    assert result["risk_protections"]["loss_event_guard"] == 3
    assert result["trades"] == 1


def test_drawdown_halt_does_not_retrigger_forever_after_cooldown(monkeypatch):
    from app import research_engine

    monkeypatch.setattr(research_engine.settings, "strategy_stoploss_guard_trades", 99)
    monkeypatch.setattr(research_engine.settings, "strategy_stoploss_guard_bars", 2)
    monkeypatch.setattr(research_engine.settings, "strategy_drawdown_halt", 0.10)
    monkeypatch.setattr(research_engine.settings, "strategy_daily_loss_halt", 1.0)

    # A 10% drawdown triggers the halt. After cooldown, the test expects the
    # strategy to be able to resume instead of being halted forever by the same
    # stale peak reference.
    result = _run_component(
        monkeypatch,
        returns=[1.0, 0.90, 1.0, 1.0, 1.0, 1.0],
        positions=[1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    )

    assert result["average_leverage"] > 0.0
    assert result["trades"] > 1
