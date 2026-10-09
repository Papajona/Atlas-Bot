from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.strategy_engine import StrategyConfig
from app.trade_learning import (
    _configured_replay_costs_bps,
    _fetch_learning_data,
    _last_completed_bar_index,
    replay_trade_episode,
)


def _df(n=700):
    idx = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    t = np.arange(n)
    close = pd.Series(100 + 0.03 * t + 2.0 * np.sin(t / 14.0), index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = np.maximum(open_, close) + 0.6
    low = np.minimum(open_, close) - 0.6
    volume = pd.Series(1_000_000.0, index=idx)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _episode(df, *, entry_at=None, exit_at=None):
    # Candle timestamps are candle-open times. The entry at index 451 uses the
    # completed close of candle 450; the exit at index 471 uses completed candle 470.
    return SimpleNamespace(
        id=1,
        entry_trade_id=1,
        asset="crypto",
        exchange="bybit",
        symbol="BTC/USDT:USDT",
        timeframe="1h",
        side="buy",
        strategy="trend",
        regime="TREND_UP",
        entry_at=entry_at or (df.index[451]).to_pydatetime(),
        exit_at=exit_at or (df.index[471]).to_pydatetime(),
        entry_quantity=1.0,
        entry_price=float(df.close.iloc[450]),
        exit_price=float(df.close.iloc[470]),
        net_pnl=100.0,
    )


def test_last_completed_bar_never_selects_open_candle():
    df = _df()
    assert _last_completed_bar_index(
        df, df.index[450] + pd.Timedelta(minutes=30), "1h"
    ) == 449
    assert _last_completed_bar_index(
        df, df.index[450] + pd.Timedelta(hours=1), "1h"
    ) == 450


def test_trade_replay_uses_completed_post_entry_candles_and_records_provenance():
    df = _df()
    df.attrs["data_provenance"] = {
        "source": "fixture",
        "sha256": "fixture-sha256",
        "row_count": len(df),
    }
    ep = _episode(df)
    result = replay_trade_episode(df, ep, cfg=StrategyConfig(), costs_bps=7.5)

    assert result["future_data_used_only_for_post_trade_learning"] is True
    assert result["bars_held"] == 20
    assert result["bar_alignment"]["completed_candles_only"] is True
    assert result["bar_alignment"]["partial_entry_exit_candles_excluded"] is True
    assert pd.Timestamp(result["bar_alignment"]["decision_bar_open"]) == df.index[450]
    assert pd.Timestamp(result["bar_alignment"]["outcome_first_bar_open"]) == df.index[451]
    assert pd.Timestamp(result["bar_alignment"]["outcome_last_bar_open"]) == df.index[470]
    assert result["data_provenance"] == df.attrs["data_provenance"]
    assert result["costs_bps_per_side"] == 7.5
    assert len(result["counterfactuals"]) >= 5
    assert result["best_counterfactual_strategy"] in {
        "trend", "momentum", "breakout", "mean_reversion", "ensemble"
    }
    assert "market_flow" in result
    assert "mfe_bps" in result and "mae_bps" in result
    assert all(cf["confidence"] != "HIGH" for cf in result["counterfactuals"])


def test_partial_entry_candle_extremes_do_not_change_mfe_or_mae():
    df = _df()
    baseline = replay_trade_episode(df, _episode(df), cfg=StrategyConfig())
    altered = df.copy()
    # Candle 450 is complete before entry and must not contribute to outcome extrema.
    altered.iloc[450, altered.columns.get_loc("high")] = 1_000_000.0
    altered.iloc[450, altered.columns.get_loc("low")] = 0.01
    changed = replay_trade_episode(altered, _episode(altered), cfg=StrategyConfig())

    assert changed["mfe_bps"] == pytest.approx(baseline["mfe_bps"])
    assert changed["mae_bps"] == pytest.approx(baseline["mae_bps"])


def test_entry_signal_is_independent_of_prices_after_decision_bar():
    df = _df()
    ep = _episode(df)
    baseline = replay_trade_episode(df, ep, cfg=StrategyConfig())

    changed_df = df.copy()
    changed_df.loc[df.index[451] :, "close"] *= 1.15
    changed_df.loc[df.index[451] :, "high"] *= 1.15
    changed_df.loc[df.index[451] :, "low"] *= 1.15
    changed = replay_trade_episode(changed_df, ep, cfg=StrategyConfig())

    baseline_signals = {x["strategy"]: x["entry_signal"] for x in baseline["counterfactuals"]}
    changed_signals = {x["strategy"]: x["entry_signal"] for x in changed["counterfactuals"]}
    assert changed_signals == baseline_signals


def test_short_episode_without_completed_post_entry_candles_is_rejected():
    df = _df()
    ep = _episode(
        df,
        entry_at=(df.index[450] + pd.Timedelta(minutes=5)).to_pydatetime(),
        exit_at=(df.index[451] + pd.Timedelta(minutes=5)).to_pydatetime(),
    )
    with pytest.raises(ValueError, match="no fully completed post-entry candles"):
        replay_trade_episode(df, ep, cfg=StrategyConfig())


def test_commodity_replay_refuses_to_substitute_forex_data():
    ep = SimpleNamespace(
        asset="commodity", symbol="XAU_USD", exchange="", timeframe="1h"
    )
    with pytest.raises(ValueError, match="no verified commodity data adapter"):
        _fetch_learning_data(ep)


def test_replay_costs_use_verified_asset_profile_values():
    assert _configured_replay_costs_bps("crypto") == pytest.approx(7.5)
    assert _configured_replay_costs_bps("forex") == pytest.approx(1.0)
    with pytest.raises(ValueError, match="No configured replay cost profile"):
        _configured_replay_costs_bps("commodity")
