import types

import numpy as np
import pandas as pd
import pytest


def _run_component(monkeypatch, returns, positions):
    from app import research_engine

    idx = pd.date_range("2026-01-01", periods=len(returns), freq="h", tz="UTC")
    signals = pd.DataFrame(
        {
            "trend": positions,
            "atr": np.full(len(returns), 1.0),
            "realized_vol": np.full(len(returns), 0.20),
            "atr": np.full(len(returns), 1.0),
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


def test_loss_guard_counts_a_continuous_losing_position_once(monkeypatch):
    from app import research_engine

    monkeypatch.setattr(research_engine.settings, "strategy_stoploss_guard_trades", 3)
    monkeypatch.setattr(research_engine.settings, "strategy_stoploss_guard_bars", 24)
    monkeypatch.setattr(research_engine.settings, "strategy_drawdown_halt", 1.0)
    monkeypatch.setattr(research_engine.settings, "strategy_daily_loss_halt", 1.0)

    # One continuous position loses on four consecutive bars, but it is only
    # one trade. The loss guard must not halt on the third losing bar.
    result = _run_component(
        monkeypatch,
        returns=[1.0, 0.999, 0.999, 0.999, 1.0, 1.0],
        positions=[1.0, 1.0, 1.0, 1.0, 0.0, 0.0],
    )

    # Positions are shifted one bar to avoid look-ahead, so four active
    # position bars are present and the three losing returns are applied.
    expected = (0.999 ** 3) - 1.0
    assert result["total_return"] == expected
    assert result["average_leverage"] == 4 / 6


def test_drawdown_halt_remains_active_while_drawdown_threshold_is_still_breached(monkeypatch):
    from app import research_engine

    monkeypatch.setattr(research_engine.settings, "strategy_stoploss_guard_trades", 99)
    monkeypatch.setattr(research_engine.settings, "strategy_stoploss_guard_bars", 2)
    monkeypatch.setattr(research_engine.settings, "strategy_drawdown_halt", 0.10)
    monkeypatch.setattr(research_engine.settings, "strategy_daily_loss_halt", 1.0)

    # Once the cooldown expires, the drawdown reference is reset to the
    # post-halt equity so the same stale peak cannot retrigger forever.
    result = _run_component(
        monkeypatch,
        returns=[1.0, 0.90, 1.0, 1.0, 1.0, 1.0],
        positions=[1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    )

    # The halt is triggered after the drawdown bar. With a two-bar cooldown,
    # the next two bars are suppressed and execution resumes on the fourth
    # subsequent bar, leaving five of six bars active under the one-bar signal
    # shift used by the backtest.
    assert result["average_leverage"] == 5 / 6
    assert result["total_return"] == pytest.approx(-0.10)
