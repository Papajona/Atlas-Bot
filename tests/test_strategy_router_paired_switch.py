import numpy as np
import pandas as pd

from app.strategy_engine import StrategyConfig
from app import strategy_router as router


def _frame(n=700):
    index = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = np.linspace(100.0, 120.0, n)
    return pd.DataFrame({
        "open": close,
        "high": close * 1.002,
        "low": close * 0.998,
        "close": close,
        "volume": np.full(n, 1000.0),
    }, index=index)


def _select_with_paired_streams(
    monkeypatch, *, regime_bars=300, challenger_edge=0.00002, seed=11,
    blend_enabled=False, cooldown_active=False, switch_min_advantage=0.0,
):
    df = _frame()
    history_index = df.index[:-1]
    regime_values = np.array(["TREND_UP"] * (len(df) - regime_bars) + ["RANGE"] * regime_bars)
    monkeypatch.setattr(
        router, "classify_regime",
        lambda frame, asset="crypto": pd.Series(regime_values[:len(frame)], index=frame.index),
    )
    metrics = {
        name: {"score": (2.0 if name == "trend" else 0.25), "bars": regime_bars, "trades": 100}
        for name in router.STRATEGIES
    }
    monkeypatch.setattr(
        router, "regime_conditioned_strategy_performance",
        lambda *args, **kwargs: {name: {"RANGE": value} for name, value in metrics.items()},
    )
    monkeypatch.setattr(router, "_score_metrics", lambda row, min_trades=20: row["score"])

    rng = np.random.default_rng(seed)
    incumbent = rng.normal(0.0, 0.001, len(history_index))
    challenger = incumbent + rng.normal(challenger_edge, 0.0007, len(history_index))
    streams = {"trend": challenger, "momentum": incumbent}
    monkeypatch.setattr(
        router, "_strategy_returns",
        lambda frame, cfg, strategy, costs_bps, asset: pd.Series(streams[strategy][:len(frame)], index=frame.index),
    )
    monkeypatch.setattr(
        router, "strategy_signals",
        lambda frame, cfg, asset="crypto": pd.DataFrame(
            {
                "trend": np.ones(len(frame)),
                "momentum": np.full(len(frame), 0.25),
                "breakout": np.zeros(len(frame)),
                "mean_reversion": np.zeros(len(frame)),
                "ensemble": np.zeros(len(frame)),
            },
            index=frame.index,
        ),
    )
    return router.select_strategy(
        df,
        StrategyConfig(),
        current_strategy="momentum",
        cooldown_active=cooldown_active,
        min_regime_bars=3,
        min_trades=0,
        switch_min_advantage=switch_min_advantage,
        blend_enabled=blend_enabled,
        online_weight=0.0,
    )


def test_router_holds_when_ranked_challenger_has_no_significant_paired_lead(monkeypatch):
    result = _select_with_paired_streams(monkeypatch, challenger_edge=0.00002)
    assert result["status"] == "OK"
    assert result["strategy"] == "momentum"
    assert result["action"] == "HOLD"
    assert result["switch_inference"]["significant_lead"] is False


def test_router_switches_only_after_significant_paired_net_return_lead(monkeypatch):
    result = _select_with_paired_streams(monkeypatch, challenger_edge=0.0009, seed=31)
    assert result["status"] == "OK"
    assert result["strategy"] == "trend"
    assert result["action"] == "SWITCH"
    assert result["switch_inference"]["significant_lead"] is True


def test_router_holds_when_the_current_regime_slice_is_underpowered(monkeypatch):
    result = _select_with_paired_streams(monkeypatch, regime_bars=45, challenger_edge=0.01)
    assert result["status"] == "OK"
    assert result["strategy"] == "momentum"
    assert result["action"] == "HOLD"
    assert result["switch_inference"]["reason"] == "insufficient_paired_observations"



def test_failed_switch_inference_cannot_leak_rejected_challenger_into_blend(monkeypatch):
    result = _select_with_paired_streams(
        monkeypatch,
        challenger_edge=0.00002,
        blend_enabled=True,
    )

    assert result["status"] == "OK"
    assert result["strategy"] == "momentum"
    assert result["action"] == "HOLD"
    assert result["switch_inference"]["significant_lead"] is False
    assert result["blend_scope"] == "INCUMBENT_ONLY"
    assert result["blend"] == [
        {"strategy": "momentum", "weight": 1.0, "score": 0.25}
    ]
    # The rejected trend challenger has a stronger raw score and signal; it must
    # not contribute to the actual HOLD composite.
    assert result["signal"] == 0.25


def test_cooldown_skips_expensive_paired_return_calculation(monkeypatch):
    def unexpected_calculation(*args, **kwargs):
        raise AssertionError("paired stream calculations must be skipped during cooldown")

    monkeypatch.setattr(router, "_strategy_returns", unexpected_calculation)
    result = _select_with_paired_streams(
        monkeypatch,
        challenger_edge=0.0009,
        cooldown_active=True,
    )

    assert result["status"] == "OK"
    assert result["strategy"] == "momentum"
    assert result["action"] == "HOLD"
    assert result["switch_inference"]["status"] == "SKIPPED"
    assert result["switch_inference"]["reason"] == "cooldown_active"
    assert result["switch_hold_reason"] == "cooldown_active"


def test_small_score_advantage_skips_paired_return_calculation(monkeypatch):
    def unexpected_calculation(*args, **kwargs):
        raise AssertionError("paired stream calculations must be skipped below switch threshold")

    monkeypatch.setattr(router, "_strategy_returns", unexpected_calculation)
    result = _select_with_paired_streams(
        monkeypatch,
        challenger_edge=0.0009,
        switch_min_advantage=5.0,
    )

    assert result["status"] == "OK"
    assert result["strategy"] == "momentum"
    assert result["action"] == "HOLD"
    assert result["switch_inference"]["status"] == "SKIPPED"
    assert result["switch_inference"]["reason"] == "score_advantage_below_minimum"
