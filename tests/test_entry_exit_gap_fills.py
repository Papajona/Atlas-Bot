"""Stops that are gapped through must fill at the bar open, not at the (better) stop price."""
import numpy as np
import pandas as pd
import pytest

import app.entry_exit_engine as ee

SLIP = 2.0 / 10_000.0  # default slippage_bps=2.0


def _run(monkeypatch, *, side, gap_open=None, stop_touch_low=None, stop_touch_high=None):
    """Controlled one-trade scenario: signal on bar K, entry fills on K+1, protective bar is K+2."""
    n, k = 400, 300
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    flat = np.full(n, 100.0)
    df = pd.DataFrame({"open": flat, "high": flat + 0.2, "low": flat - 0.2, "close": flat, "volume": 1000.0}, index=idx)
    a = pd.DataFrame(index=idx)
    a["side"], a["entry_valid"], a["atr"] = "flat", False, 0.5
    for col in ("entry_price", "stop_loss_price", "take_profit_price", "risk_per_unit", "reward_risk"):
        a[col] = np.nan
    if side == "buy":
        df.loc[idx[k + 1], "high"] = 100.6                      # reaches the 100.5 buy-stop entry
        plan = ["buy", True, 100.5, 99.0, 103.5, 1.5, 2.0]
        if gap_open is not None:
            df.loc[idx[k + 2], ["open", "high", "low", "close"]] = [gap_open, gap_open + 0.5, gap_open - 0.5, gap_open]
        elif stop_touch_low is not None:
            df.loc[idx[k + 2], "low"] = stop_touch_low           # pierces the stop without gapping
    else:
        df.loc[idx[k + 1], "low"] = 99.4                         # reaches the 99.5 sell-stop entry
        plan = ["sell", True, 99.5, 101.0, 96.5, 1.5, 2.0]
        if gap_open is not None:
            df.loc[idx[k + 2], ["open", "high", "low", "close"]] = [gap_open, gap_open + 0.5, gap_open - 0.5, gap_open]
        elif stop_touch_high is not None:
            df.loc[idx[k + 2], "high"] = stop_touch_high
    a.loc[idx[k], ["side", "entry_valid", "entry_price", "stop_loss_price", "take_profit_price", "risk_per_unit", "reward_risk"]] = plan
    monkeypatch.setattr(ee, "gated_entry_exit_analysis", lambda _df, _cfg=None: a)
    result = ee.gated_entry_exit_backtest(df, ee.EntryExitConfig())
    assert result["trades"] == 1
    trade = result["trades_detail"][0]
    trade["_gap_stop_exit_count"] = result["gap_stop_exit_count"]
    return trade


def test_long_stop_gapped_down_fills_at_the_open_not_the_stop(monkeypatch):
    trade = _run(monkeypatch, side="buy", gap_open=90.0)
    assert trade["reason"] == "stop_loss"
    assert trade["exit"] == pytest.approx(90.0 * (1 - SLIP))
    assert trade["exit"] < trade["stop"] - 5          # nowhere near the 99.0 stop
    assert trade["net_return"] < -0.02                 # loss exceeds the 0.5% risk budget, as it would live
    assert trade["_gap_stop_exit_count"] == 1


def test_short_stop_gapped_up_fills_at_the_open_not_the_stop(monkeypatch):
    trade = _run(monkeypatch, side="sell", gap_open=110.0)
    assert trade["reason"] == "stop_loss"
    assert trade["exit"] == pytest.approx(110.0 * (1 + SLIP))
    assert trade["exit"] > trade["stop"] + 5
    assert trade["net_return"] < -0.02


def test_stop_without_a_gap_still_fills_at_the_stop_price(monkeypatch):
    long_trade = _run(monkeypatch, side="buy", stop_touch_low=98.8)
    assert long_trade["exit"] == pytest.approx(99.0 * (1 - SLIP))
    short_trade = _run(monkeypatch, side="sell", stop_touch_high=101.2)
    assert short_trade["exit"] == pytest.approx(101.0 * (1 + SLIP))
    assert long_trade["_gap_stop_exit_count"] == 0 and short_trade["_gap_stop_exit_count"] == 0


def test_gap_through_the_stop_is_never_better_than_a_no_gap_stop(monkeypatch):
    gapped = _run(monkeypatch, side="buy", gap_open=97.0)
    normal = _run(monkeypatch, side="buy", stop_touch_low=98.8)
    assert gapped["net_return"] < normal["net_return"]
