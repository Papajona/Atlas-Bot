import numpy as np
import pandas as pd

from app.strategy_engine import StrategyConfig
from app.strategy_router import (
    STRATEGIES,
    classify_regime,
    regime_conditioned_strategy_performance,
    select_strategy,
    online_strategy_scores,
    strategy_trade_plan,
    router_walk_forward_backtest,
)


def _df(n=1100):
    idx = pd.date_range("2022-01-01", periods=n, freq="h", tz="UTC")
    t = np.arange(n)
    close = 100 + 0.025 * t + 2.5 * np.sin(t / 18.0) + 0.9 * np.sin(t / 4.0)
    close[700:] += 3.0 * np.sin(np.arange(n - 700) / 35.0)
    close = pd.Series(close, index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    volume = pd.Series(1_000_000 + 50_000 * np.sin(t / 11.0), index=idx)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def test_regime_classifier_uses_completed_bars_only():
    df = _df()
    regimes = classify_regime(df)
    assert len(regimes) == len(df)
    assert set(regimes.dropna().unique()).issubset({"TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "TRANSITION"})


def test_regime_conditioned_performance_is_per_strategy():
    df = _df()
    perf = regime_conditioned_strategy_performance(df, StrategyConfig())
    assert set(perf) == set(STRATEGIES)
    assert any(perf[name] for name in STRATEGIES)
    for name in STRATEGIES:
        for regime, metrics in perf[name].items():
            assert "sharpe" in metrics
            assert "max_drawdown" in metrics
            assert "trades" in metrics


def test_online_scores_require_minimum_observations():
    from datetime import datetime, timezone
    rows = [{"strategy": "breakout", "return_bps": 20.0, "created_at": datetime.now(timezone.utc)} for _ in range(10)]
    scores = online_strategy_scores(rows, min_observations=10)
    assert scores["breakout"] > 0
    rows = rows[:9]
    scores = online_strategy_scores(rows, min_observations=10)
    assert scores["breakout"] == 0.0


def test_router_holds_during_cooldown():
    df = _df()
    result = select_strategy(df, StrategyConfig(), current_strategy="ensemble", cooldown_active=True)
    assert result["status"] in {"OK", "TRANSITION"}
    if result["status"] == "OK":
        assert result["strategy"] == "ensemble"
        assert result["action"] == "HOLD"


def test_router_can_return_blended_signal():
    df = _df()
    result = select_strategy(df, StrategyConfig(), blend_enabled=True, blend_top_n=3, min_trades=5)
    assert result["status"] in {"OK", "TRANSITION"}
    if result["status"] == "OK":
        assert isinstance(result["blend"], list)
        assert len(result["blend"]) >= 1
        assert abs(float(result["signal"])) <= 1.0


def test_strategy_trade_plan_is_risk_aware():
    df = _df()
    plan = strategy_trade_plan(df, "trend", signal=0.9, cfg=StrategyConfig())
    assert plan is not None
    assert plan["side"] == "buy"
    assert plan["stop_loss_price"] < plan["entry_price"] < plan["take_profit_price"]
    assert plan["reward_risk"] >= 2.0


def test_router_walk_forward_evaluates_selection_on_unseen_folds():
    df = _df(1300)
    result = router_walk_forward_backtest(df, StrategyConfig(), folds=3, min_train=700, min_trades=5)
    assert result["status"] in {"OK", "INSUFFICIENT_DATA"}
    if result["status"] == "OK":
        assert result["router"] == "REGIME_CONDITIONED_WALK_FORWARD"
        assert len(result["folds"]) >= 1


def test_online_scores_prefer_current_regime_and_net_return():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    rows = []
    for _ in range(10):
        rows.append({"strategy": "breakout", "regime": "TREND_UP", "timeframe": "1h", "net_return_bps": 30.0, "created_at": now - timedelta(hours=1)})
        rows.append({"strategy": "breakout", "regime": "RANGE", "timeframe": "1h", "net_return_bps": -20.0, "created_at": now - timedelta(hours=1)})
    scores = online_strategy_scores(rows, current_regime="TREND_UP", timeframe="1h", min_observations=10)
    assert scores["breakout"] > 0


def test_policy_walk_forward_includes_online_feedback_and_switch_controls():
    df = _df(1000)
    from app.strategy_router import policy_walk_forward_backtest
    result = policy_walk_forward_backtest(df, StrategyConfig(), folds=2, min_train=500, min_trades=5, decision_stride=8)
    assert result["status"] in {"OK", "INSUFFICIENT_DATA"}
    if result["status"] == "OK":
        assert result["router"] == "FULL_ADAPTIVE_POLICY_WALK_FORWARD"
        assert set(result["includes"]) >= {"regime", "online_outcome_feedback", "switch_hysteresis", "blending", "abstention"}
        # Overall timestamps must survive concatenation so hourly data is not
        # silently annualized with the 252-period fallback.
        assert result["annualization_factor"] > 8000

def test_full_policy_walk_forward_executes_after_signal_bar(monkeypatch):
    # Close moves down while executable opens rise. The old close[i] -> close[next_i]
    # simulation lost money; a properly lagged open[i+1] -> open[next_i+1] result is positive.
    from app import strategy_router as router

    n = 80
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    t = np.arange(n, dtype=float)
    close = 200.0 - t
    opens = 100.0 + t
    frame = pd.DataFrame({
        "open": opens,
        "high": np.maximum(opens, close) + 1.0,
        "low": np.minimum(opens, close) - 1.0,
        "close": close,
        "volume": np.full(n, 1_000_000.0),
    }, index=idx)
    monkeypatch.setattr(
        router, "strategy_signals",
        lambda df, cfg, asset="crypto": pd.DataFrame(
            {"realized_vol": np.ones(len(df))}, index=df.index
        ),
    )
    monkeypatch.setattr(
        router, "classify_regime",
        lambda df, asset="crypto": pd.Series("RANGE", index=df.index),
    )
    observed_counts = []

    def capture_observed(observed, **kwargs):
        observed_counts.append(len(observed))
        return {}

    monkeypatch.setattr(router, "online_strategy_scores", capture_observed)
    monkeypatch.setattr(
        router, "select_strategy",
        lambda *a, **k: {
            "status": "OK", "strategy": "trend", "signal": 1.0,
            "action": "HOLD",
        },
    )

    result = router.policy_walk_forward_backtest(
        frame, StrategyConfig(target_vol_annual=1.0, max_leverage=1.0, signal_threshold=0.0),
        folds=2, min_train=10, min_trades=0, decision_stride=1, costs_bps=0.0,
    )
    assert result["status"] == "OK"
    assert result["total_return"] > 0.0
    assert all(fold["total_return"] > 0.0 for fold in result["folds"])
    # First reward ends at open[i+2], so it must not be seen by the immediately
    # following close[i+1] decision. It becomes available at the endpoint bar close.
    assert observed_counts[0] == 0
    assert observed_counts[1] == 0
    assert observed_counts[2] > 0




def test_router_walk_forward_scores_final_test_bar_using_full_frame_next_open(monkeypatch):
    """The last test-bar return must use the next open outside the sliced fold."""
    from types import SimpleNamespace
    from app import strategy_router as router

    idx = pd.date_range("2026-01-01", periods=7, freq="h", tz="UTC")
    opens = np.array([100.0, 110.0, 121.0, 133.1, 146.41, 161.051, 177.1561])
    close = opens.copy()
    frame = pd.DataFrame({
        "open": opens,
        "high": np.maximum(opens, close) + 1.0,
        "low": np.minimum(opens, close) - 1.0,
        "close": close,
        "volume": np.full(len(opens), 1000.0),
    }, index=idx)

    # Use one deterministic held-out fold so the final-bar execution boundary is
    # the only behavior under test; training chooses the first strategy on ties.
    monkeypatch.setattr(
        router, "purged_walk_forward_splits",
        lambda *args, **kwargs: [SimpleNamespace(test_start=3, test_end=5)],
    )
    monkeypatch.setattr(
        router, "strategy_signals",
        lambda df, cfg, asset="crypto": pd.DataFrame(
            {name: np.ones(len(df)) for name in router.STRATEGIES}, index=df.index
        ),
    )
    monkeypatch.setattr(
        router, "classify_regime",
        lambda df, asset="crypto": pd.Series(router.REGIMES[0], index=df.index),
    )
    monkeypatch.setattr(router, "regime_conditioned_strategy_performance", lambda *a, **k: {})
    monkeypatch.setattr(router, "_score_metrics", lambda metrics, min_trades=20: 1.0)

    result = router.router_walk_forward_backtest(
        frame, StrategyConfig(signal_threshold=0.0, target_vol_annual=1.0, max_leverage=1.0),
        folds=1, min_train=1, min_trades=0, costs_bps=0.0,
    )
    assert result["status"] == "OK"
    # The position is active on fold bar 4 and earns open[5] / open[4] - 1.
    # Computing next_open_returns on the fold slice would incorrectly score this as 0.
    assert result["total_return"] > 0.09


def test_regime_classifier_annualization_tracks_hourly_and_daily_cadence():
    hourly = classify_regime(_df(260), asset="crypto")
    daily_df = _df(260).iloc[::24].copy()
    daily_df.index = pd.date_range("2022-01-01", periods=len(daily_df), freq="D", tz="UTC")
    daily = classify_regime(daily_df, asset="crypto")
    assert hourly.attrs["annualization_factor"] > 8000
    assert 300 <= daily.attrs["annualization_factor"] <= 400



def test_regime_filter_keeps_annualization_from_full_source_cadence(monkeypatch):
    from app import strategy_router as router

    frame = _df(700)
    alternating = pd.Series(
        np.where(np.arange(len(frame)) % 2 == 0, "RANGE", "TREND_UP"),
        index=frame.index,
    )
    monkeypatch.setattr(router, "classify_regime", lambda df, asset="crypto": alternating)

    result = router.regime_conditioned_strategy_performance(frame, StrategyConfig(), asset="crypto")
    expected = router._annualization_factor(frame.index, asset="crypto")
    assert expected > 8000
    assert result["ensemble"]["RANGE"]["annualization_factor"] == expected
    assert result["ensemble"]["TREND_UP"]["annualization_factor"] == expected
