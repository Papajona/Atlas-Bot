import numpy as np
import pandas as pd

from app.research_validation import (
    validate_ohlcv_frame, purged_walk_forward_splits, bootstrap_mean_ci,
    multiple_testing_sharpe_adjustment, cost_sensitivity, ohlc_trade_ambiguity,
    fixed_signal_walk_forward_oos, regime_conditioned_performance,
)


def frame(n=100):
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    c = pd.Series(np.linspace(100, 110, n), index=idx)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": 100}, index=idx)


def test_validate_ohlcv_and_splits():
    df = frame()
    assert validate_ohlcv_frame(df)["valid"]
    folds = purged_walk_forward_splits(200, folds=4, min_train=80, purge=2, embargo=3)
    assert folds and all(f.test_start > f.train_end for f in folds)
    assert all(f.purge == 2 and f.embargo == 3 for f in folds)


def test_validation_statistics_are_deterministic():
    ci = bootstrap_mean_ci(np.full(50, 0.001), samples=200, seed=9, block_size=5)
    assert ci["status"] == "OK" and ci["ci_low"] > 0
    adj = multiple_testing_sharpe_adjustment([0.2, 0.4, 0.8], 3)
    assert adj["status"] == "OK"
    costs = cost_sensitivity(np.full(20, 0.001), np.full(20, 0.1), [5, 50])
    assert costs[0]["total_return"] > costs[1]["total_return"]


def test_ohlc_ambiguity_is_detected():
    df = frame(2)
    stop = pd.Series([99, 109], index=df.index)
    target = pd.Series([101, 111], index=df.index)
    side = pd.Series(["buy", "buy"], index=df.index)
    out = ohlc_trade_ambiguity(df, stop, target, side)
    assert out["ambiguous"] == 2
    assert out["policy"] == "WORST_CASE"


def test_oos_and_regime_metrics_are_forward_only_and_finite():
    df = frame(1000)
    signal = pd.Series(0.5, index=df.index)
    out = fixed_signal_walk_forward_oos(df, signal, costs_bps=7.5, folds=3, min_train=500)
    assert out["status"] == "OK" and out["oos_bars"] > 0
    assert out["sharpe"] is not None and np.isfinite(out["sharpe"])
    assert out["max_drawdown"] is not None and np.isfinite(out["max_drawdown"])
    assert isinstance(out["trades"], int) and out["trades"] >= 0
    assert out["annualization_factor"] == 252.0
    assert out["cost_analysis"]["observations"] == out["oos_bars"]
    # Without a supplied carry schedule source and evidence ID, an otherwise green
    # numerical cost report remains non-promotion-grade financing evidence.
    assert out["cost_stress_ok"] is False
    assert out["financing_cost_model_complete"] is False
    regimes = regime_conditioned_performance(df, pd.Series(0.001, index=df.index))
    assert regimes and all(np.isfinite(v["total_return"]) for v in regimes.values())


def test_oos_gate_reports_missing_metrics_and_never_promotes_them():
    from app.research_validation import oos_promotion_gate

    strong = {
        "status": "OK",
        "folds": [{"total_return": value} for value in (0.03, 0.02, 0.01, 0.04, 0.02, 0.01)],
        "oos_total_return": 0.12,
        "sharpe": 0.8,
        "max_drawdown": -0.10,
        "trades": 150,
        "deflated_sharpe": 0.97,
        "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True,
        "trial_registry_complete": True,
        "slippage_model_complete": True,
    }
    missing_sharpe = dict(strong)
    missing_sharpe.pop("sharpe")
    report = oos_promotion_gate(missing_sharpe)
    assert report["passed"] is False
    assert "missing_or_below_minimum_oos_sharpe" in report["rejection_reasons"]

    malformed = {**strong, "sharpe": "nan"}
    report = oos_promotion_gate(malformed)
    assert report["passed"] is False
    assert "missing_or_below_minimum_oos_sharpe" in report["rejection_reasons"]


def test_oos_gate_names_incomplete_trial_registry_as_a_blocker():
    from app.research_validation import oos_promotion_gate

    evidence = {
        "status": "OK",
        "folds": [{"total_return": 0.03}] * 6,
        "oos_total_return": 0.18,
        "sharpe": 1.0,
        "max_drawdown": -0.05,
        "trades": 120,
        "deflated_sharpe": 0.98,
        "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True,
        "trial_registry_complete": False,
        "slippage_model_complete": True,
    }
    report = oos_promotion_gate(evidence)
    assert report["passed"] is False
    assert report["rejection_reasons"] == ["comparable_trial_registry_incomplete"]

def test_regime_volatility_uses_observed_intraday_bar_cadence():
    import pandas as pd
    from app.research_validation import regime_conditioned_performance

    hourly_index = pd.date_range("2024-01-01", periods=260, freq="h", tz="UTC")
    daily_index = pd.date_range("2024-01-01", periods=260, freq="D", tz="UTC")
    hourly_close = pd.Series(np.linspace(100.0, 110.0, len(hourly_index)), index=hourly_index)
    daily_close = pd.Series(np.linspace(100.0, 110.0, len(daily_index)), index=daily_index)
    hourly_df = pd.DataFrame({"close": hourly_close})
    daily_df = pd.DataFrame({"close": daily_close})

    hourly = regime_conditioned_performance(hourly_df, pd.Series(0.001, index=hourly_index), asset="crypto")
    daily = regime_conditioned_performance(daily_df, pd.Series(0.001, index=daily_index), asset="crypto")

    assert all(row["annualization_factor"] > 8000 for row in hourly.values())
    assert all(300 <= row["annualization_factor"] <= 400 for row in daily.values())


def test_oos_rejects_misaligned_signal_index_instead_of_silently_flattening():
    df = frame(1000)
    signal = pd.Series(0.5, index=df.index + pd.Timedelta(hours=1))
    out = fixed_signal_walk_forward_oos(df, signal, costs_bps=7.5, folds=3, min_train=500)
    assert out["status"] == "INVALID_DATA"
    assert out["reason"] == "signal_index_mismatch"
    assert out["folds"] == []


def test_oos_rejects_signal_length_mismatch():
    df = frame(1000)
    signal = np.full(len(df) - 1, 0.5)
    out = fixed_signal_walk_forward_oos(df, signal, costs_bps=7.5, folds=3, min_train=500)
    assert out["status"] == "INVALID_DATA"
    assert out["reason"] == "signal_length_mismatch"


def test_oos_promotion_gate_blocks_known_survivor_only_crypto_universe():
    from app.research_validation import oos_promotion_gate

    strong = {
        "status": "OK",
        "folds": [{"total_return": value} for value in (0.03, 0.02, 0.01, 0.04, 0.02, 0.01)],
        "oos_total_return": 0.12,
        "sharpe": 0.8,
        "max_drawdown": -0.10,
        "trades": 150,
        "deflated_sharpe": 0.97,
        "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True,
        "trial_registry_complete": True,
        "slippage_model_complete": True,
        "universe_bias": "CURRENT_SURVIVORS_ONLY_NO_POINT_IN_TIME_HISTORY",
    }
    report = oos_promotion_gate(strong)
    assert report["passed"] is False
    assert report["survivorship_bias_blocked"] is True
    assert "crypto_universe_survivorship_bias_not_resolved" in report["rejection_reasons"]


def test_oos_promotion_gate_blocks_explicitly_incomplete_slippage_model():
    from app.research_validation import oos_promotion_gate

    strong = {
        "status": "OK",
        "folds": [{"total_return": 0.03}] * 6,
        "oos_total_return": 0.18,
        "sharpe": 1.0,
        "max_drawdown": -0.05,
        "trades": 120,
        "deflated_sharpe": 0.98,
        "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True,
        "trial_registry_complete": True,
        "slippage_model_complete": False,
        "slippage_model_status": "STATIC_BPS_NOT_VOLUME_VOLATILITY_CALIBRATED",
    }
    report = oos_promotion_gate(strong)
    assert report["passed"] is False
    assert report["slippage_model_complete"] is False
    assert "volume_volatility_aware_slippage_model_incomplete" in report["rejection_reasons"]
