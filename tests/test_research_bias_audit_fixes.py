"""Regression tests for the research-bias audit remediation.

These tests validate reported evidence and fail-closed behavior; they do not assert
that a strategy has predictive skill or substitute for venue-calibration data.
"""
import numpy as np
import pandas as pd

from app.research_validation import (
    validate_ohlcv_frame,
    newey_west_mean_inference,
    oos_benchmark_comparison,
    multiple_testing_sharpe_adjustment,
    fixed_signal_walk_forward_oos,
    oos_promotion_gate,
)


def _ohlcv(n=1200, freq="h"):
    index = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    rng = np.random.default_rng(123)
    ret = rng.normal(0.0001, 0.002, n)
    close = 100.0 * np.cumprod(1.0 + ret)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {"open": open_, "high": np.maximum(open_, close) * 1.002,
         "low": np.minimum(open_, close) * 0.998, "close": close,
         "volume": np.full(n, 1000.0)},
        index=index,
    )


def test_gap_report_exposes_irregular_timestamp_coverage():
    frame = _ohlcv(30)
    idx = frame.index.to_series()
    idx.iloc[15:] = idx.iloc[15:] + pd.Timedelta(hours=4)
    frame.index = pd.DatetimeIndex(idx)
    report = validate_ohlcv_frame(frame, timeframe="1h", asset="crypto")
    assert report["gap_report"]["intervals_above_1_5x_expected"] >= 1
    assert report["gap_report"]["unexpected_gap_intervals"] >= 1
    assert report["gap_report"]["estimated_missing_bars"] >= 1
    assert report["gap_report"]["covered_interval_ratio"] < 1.0



def test_fx_weekend_like_closure_is_reported_separately_from_missing_bars():
    index = pd.to_datetime([
        "2025-01-03T16:00:00Z",
        "2025-01-03T17:00:00Z",
        "2025-01-05T22:00:00Z",
        "2025-01-05T23:00:00Z",
    ])
    close = np.array([100.0, 100.1, 100.2, 100.3])
    frame = pd.DataFrame({
        "open": close, "high": close + 0.2, "low": close - 0.2,
        "close": close, "volume": np.full(4, 100.0),
    }, index=index)
    report = validate_ohlcv_frame(frame, timeframe="1h", asset="forex")
    assert report["gap_report"]["market_closure_suspected_intervals"] == 1
    assert report["gap_report"]["unexpected_gap_intervals"] == 0
    assert report["gap_report"]["estimated_missing_bars"] == 0

def test_newey_west_report_separates_period_statistic_from_annualized_sharpe():
    rng = np.random.default_rng(55)
    innovations = rng.normal(0.0005, 0.01, 4000)
    returns = np.zeros_like(innovations)
    for i in range(1, len(returns)):
        returns[i] = 0.65 * returns[i - 1] + innovations[i]
    result = newey_west_mean_inference(returns)
    assert result["status"] == "OK"
    assert result["observations"] == len(returns)
    assert 1 <= result["effective_observations"] <= len(returns)
    assert result["newey_west_standard_error"] > 0
    assert np.isfinite(result["newey_west_t_statistic"])
    assert result["per_period_sharpe"] is not None


def test_multiple_testing_proxy_scales_with_observed_trial_dispersion():
    low = multiple_testing_sharpe_adjustment([0.1, 0.2, 0.3], trials=3)
    high = multiple_testing_sharpe_adjustment([1.0, 2.0, 3.0], trials=3)
    assert low["status"] == high["status"] == "OK"
    assert high["observed_trial_variance"] > low["observed_trial_variance"]
    assert high["expected_null_max_proxy"] > low["expected_null_max_proxy"]


def test_benchmark_comparison_uses_same_fold_blocks_and_reports_single_symbol_limit():
    market = [np.full(100, 0.001), np.full(100, -0.0002)]
    positions = [np.ones(100), np.ones(100)]
    candidate = [np.zeros(100), np.zeros(100)]
    report = oos_benchmark_comparison(
        market, positions, candidate,
        transaction_cost_bps=2.0, carry_bps_per_bar=0.0, random_trials=25, seed=8,
    )
    assert report["status"] == "OK"
    assert report["candidate_net_return"] == 0.0
    assert report["buy_and_hold_net_return"] > report["candidate_net_return"]
    assert report["gate_passed"] is False
    assert report["equal_weight_basket"]["status"] == "UNAVAILABLE_SINGLE_SYMBOL_INPUT"
    assert report["random_trials"] == 25


def test_fixed_signal_oos_reports_inference_benchmark_and_hold_horizon():
    frame = _ohlcv(1400)
    signal = pd.Series(np.where(np.arange(len(frame)) % 48 < 24, 0.5, -0.5), index=frame.index)
    result = fixed_signal_walk_forward_oos(
        frame, signal, costs_bps=2.0, folds=3, min_train=500,
        max_hold_bars=12, embargo_bars=12,
    )
    assert result["status"] == "OK"
    assert result["purge_bars"] == 12
    assert result["embargo_bars"] == 12
    assert result["inference"]["status"] == "OK"
    assert result["benchmark_comparison"]["status"] == "OK"
    assert "excess_return_vs_buy_and_hold" in result["benchmark_comparison"]


def test_promotion_gate_requires_inference_and_benchmarks_when_configured():
    folds = [{"total_return": 0.02} for _ in range(5)]
    candidate = {
        "status": "OK", "folds": folds, "oos_total_return": 0.10,
        "sharpe": 0.8, "max_drawdown": -0.10, "trades": 150,
        "deflated_sharpe": 0.97, "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True, "slippage_model_complete": True,
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_evidence_id": "fixture:venue-cost-calibration",
        "trial_registry_complete": True,
    }
    missing = oos_promotion_gate(
        candidate, min_mean_t_statistic=1.645,
        min_effective_observations=100, require_benchmark_excess_return=True,
    )
    assert missing["passed"] is False
    assert "missing_or_insignificant_newey_west_mean_return" in missing["rejection_reasons"]
    assert "benchmark_excess_return_not_positive" in missing["rejection_reasons"]

    complete = {
        **candidate,
        "inference": {
            "status": "OK", "newey_west_t_statistic": 2.0,
            "effective_observations": 250,
        },
        "benchmark_comparison": {"status": "OK", "gate_passed": True},
    }
    passed = oos_promotion_gate(
        complete, min_mean_t_statistic=1.645,
        min_effective_observations=100, require_benchmark_excess_return=True,
    )
    assert passed["passed"] is True
