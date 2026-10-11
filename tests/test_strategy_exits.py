import json
import types

import numpy as np
import pandas as pd
import pytest

from app.strategy_engine import StrategyConfig, strategy_backtest
from app.strategy_exits import simulate_exit_rules, stop_policy_study, target_positions


def _df(o, h, l, c=None):
    idx = pd.date_range("2025-01-01", periods=len(o), freq="1h", tz="UTC")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c if c is not None else o, "volume": 1.0}, index=idx)


def _sim(df, pos, **kw):
    atr = pd.Series(2.0, index=df.index)
    kw.setdefault("taker_bps", 0.0)
    kw.setdefault("slippage_bps", 0.0)
    return simulate_exit_rules(df, pd.Series(pos, index=df.index, dtype=float), atr, **kw)


def test_no_rules_matches_strategy_backtest_exactly():
    rng = np.random.default_rng(11)
    n = 2500
    close = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.001, n))
    df = _df(open_, np.maximum(open_, close) * 1.004, np.minimum(open_, close) * 0.996, close)
    cfg = StrategyConfig()
    pos, atr = target_positions(df, cfg)
    sim = simulate_exit_rules(df, pos, atr)
    eq = (1 + sim["net"]).cumprod()
    ref = strategy_backtest(df, cfg)
    assert eq.iloc[-1] - 1 == pytest.approx(ref["total_return"], rel=1e-9, abs=1e-12)
    assert float((eq / eq.cummax() - 1).min()) == pytest.approx(ref["max_drawdown"], rel=1e-9, abs=1e-12)


def test_long_stop_fills_at_stop_price():
    df = _df([100, 100, 100, 100], [101, 101, 101, 101], [99, 95, 99, 99])
    out = _sim(df, [0, 1, 1, 0], stop_atr=2.0)
    assert out["net"].iloc[1] == pytest.approx(-0.04)
    assert out["counts"]["stop_exits"] == 1


def test_gap_through_stop_fills_at_open_not_stop():
    df = _df([100, 100, 94, 94], [101, 101, 95, 95], [99, 99, 93, 93])
    out = _sim(df, [0, 1, 1, 1], stop_atr=2.0)
    assert out["counts"]["stop_exits"] == 1
    assert out["net"].iloc[1] == pytest.approx(-0.06)
    assert out["net"].iloc[2] == pytest.approx(0.0)


def test_stop_wins_when_bar_touches_stop_and_target():
    df = _df([100, 100, 100], [101, 110, 101], [99, 90, 99])
    out = _sim(df, [0, 1, 0], stop_atr=2.0, take_profit_atr=3.0)
    assert out["counts"]["stop_exits"] == 1 and out["counts"]["take_profit_exits"] == 0
    assert out["net"].iloc[1] == pytest.approx(-0.04)


def test_take_profit_fills_at_target():
    df = _df([100, 100, 100], [101, 107, 101], [99, 99, 99])
    out = _sim(df, [0, 1, 0], stop_atr=2.0, take_profit_atr=3.0)
    assert out["net"].iloc[1] == pytest.approx(0.06)
    assert out["counts"]["take_profit_exits"] == 1


def test_short_stop_mirrors_long():
    df = _df([100, 100, 100], [101, 105, 101], [99, 99, 99])
    out = _sim(df, [0, -1, 0], stop_atr=2.0)
    assert out["net"].iloc[1] == pytest.approx(-0.04)


def test_no_instant_reentry_after_stop_until_signal_resets():
    df = _df([100] * 7, [101] * 7, [99, 95, 99, 99, 99, 99, 99])
    out = _sim(df, [0, 1, 1, 1, 0, 1, 1], stop_atr=2.0)
    assert out["counts"]["entries"] == 2
    assert out["counts"]["stop_exits"] == 1
    assert out["net"].iloc[2] == 0.0 and out["net"].iloc[3] == 0.0


def test_time_stop_exits_after_n_bars_and_blocks_reentry():
    o = [100, 100, 101, 102, 103, 104]
    df = _df(o, [x + 0.5 for x in o], [x - 0.5 for x in o])
    out = _sim(df, [0, 1, 1, 1, 1, 1], max_hold_bars=2)
    assert out["counts"]["time_exits"] == 1 and out["counts"]["entries"] == 1
    assert out["net"].iloc[3:].abs().sum() == 0.0


def test_stop_exit_cost_includes_exit_and_extra_slippage():
    df = _df([100, 100, 100], [101, 101, 101], [99, 95, 99])
    base = _sim(df, [0, 1, 0], stop_atr=2.0, taker_bps=10.0, slippage_bps=0.0)["net"].iloc[1]
    worse = _sim(df, [0, 1, 0], stop_atr=2.0, taker_bps=10.0, slippage_bps=0.0, stop_extra_slippage_bps=20.0)["net"].iloc[1]
    assert base == pytest.approx(-0.04 - 2 * 0.001)
    assert worse == pytest.approx(base - 0.002)


def test_study_reports_every_variant_counts_trials_and_never_auto_selects():
    rng = np.random.default_rng(5)
    n = 1800
    close = 100 * np.exp(np.cumsum(rng.normal(0.0001, 0.008, n)))
    open_ = np.r_[close[0], close[:-1]]
    df = _df(open_, np.maximum(open_, close) * 1.003, np.minimum(open_, close) * 0.997, close)
    res = stop_policy_study(df, stop_grid=(2.0, 3.0), take_profit_grid=(None, 4.0))
    assert len(res["variants"]) == 5 and res["cumulative_trials"] == 5
    assert res["baseline"]["stop_atr"] is None and "note" in res
    res2 = stop_policy_study(df, stop_grid=(2.0, 3.0), take_profit_grid=(None, 4.0), prior_trial_labels=["earlier-run-variant"])
    assert res2["cumulative_trials"] == 6
    if res["deflated_sharpe"].get("status") == "OK":
        assert res["deflated_sharpe"]["trials"] == 5


def test_cli_runs_on_csv(tmp_path, capsys):
    from scripts.stop_study import main
    rng = np.random.default_rng(2)
    n = 1500
    close = 100 * np.exp(np.cumsum(rng.normal(0.0, 0.008, n)))
    open_ = np.r_[close[0], close[:-1]]
    df = _df(open_, np.maximum(open_, close) * 1.003, np.minimum(open_, close) * 0.997, close)
    p = tmp_path / "d.csv"
    df.rename_axis("timestamp").reset_index().to_csv(p, index=False)
    assert main(["--csv", str(p), "--signal", "trend"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["signal"] == "trend" and out["variants"]


def test_correlated_group_cap_helpers(monkeypatch):
    import app.execution as ex
    from app.config import settings
    monkeypatch.setattr(settings, "correlated_symbol_groups", "BTC/USDT, ETH/USDT ,SOL/USDT; XAU/USD,XAG/USD")
    pos = [types.SimpleNamespace(symbol="BTC/USDT", quantity=0.1, average_entry_price=60000.0),
           types.SimpleNamespace(symbol="XAU/USD", quantity=1.0, average_entry_price=2000.0)]
    total, group = ex._correlated_group_exposure_usd(pos, "ETH/USDT", 1500.0)
    assert group == {"BTC/USDT", "ETH/USDT", "SOL/USDT"} and total == pytest.approx(7500.0)
    total2, group2 = ex._correlated_group_exposure_usd(pos, "DOGE/USDT", 500.0)
    assert group2 is None and total2 == 500.0
    monkeypatch.setattr(settings, "correlated_symbol_groups", "")
    assert ex._correlation_groups() == []


def test_correlated_cap_is_wired_into_risk_gate_after_position_count_check():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "execution.py").read_text(encoding="utf-8")
    assert src.index("Maximum open position count reached") < src.index("Correlated-asset group exposure limit exceeded")
    from app.config import Settings
    assert Settings.model_fields["max_correlated_group_exposure_usd"].default == 0.0
