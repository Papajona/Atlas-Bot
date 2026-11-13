"""Regression tests for the effective-sample-size Deflated Sharpe Ratio.

The DSR used to be computed with the raw bar count even though the promotion gate's Newey-West
diagnostic already reports an autocorrelation-aware effective sample. These tests pin the corrected
behaviour. They validate statistics plumbing on synthetic returns; they do not claim any strategy
has predictive skill.
"""
import numpy as np
import pandas as pd

from app.research_validation import deflated_sharpe_report, newey_west_mean_inference

BARS_PER_YEAR = 8760
N_TRIALS = 25


def _trial_sharpes(seed: int = 3) -> list[float]:
    rng = np.random.default_rng(seed)
    return [float(v) for v in rng.normal(0.4, 0.6, N_TRIALS)]


def _ar1(phi: float, n: int = 4000, mu: float = 0.00008, sigma: float = 0.002, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, sigma, n)
    x = np.zeros(n)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + noise[t]
    return x + mu


def test_report_exposes_raw_and_effective_observations():
    r = _ar1(0.3)
    out = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR)
    assert out["status"] == "OK"
    assert out["observations"] == len(r)
    assert 2 <= out["effective_observations"] <= out["observations"]


def test_independent_returns_keep_nearly_full_sample():
    r = _ar1(0.0)
    raw = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR, use_effective_n=False)
    eff = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR)
    assert eff["effective_observations"] >= 0.85 * eff["observations"]
    assert abs(eff["deflated_sharpe_ratio"] - raw["deflated_sharpe_ratio"]) < 0.05


def test_autocorrelated_returns_shrink_effective_n():
    r = _ar1(0.5)
    out = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR)
    # AR(1) phi=0.5 implies an effective sample of roughly (1-phi)/(1+phi) = one third of the bars.
    assert out["effective_observations"] < 0.6 * out["observations"]


def test_candidate_above_hurdle_gets_a_lower_dsr_with_effective_n():
    r = _ar1(0.5, mu=0.00016)
    raw = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR, use_effective_n=False)
    eff = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR)
    assert raw["per_period_sharpe"] > raw["sharpe_hurdle_per_period"]  # precondition: candidate clears the hurdle
    assert eff["deflated_sharpe_ratio"] < raw["deflated_sharpe_ratio"] - 0.05


def test_a_raw_pass_can_become_a_fail_once_autocorrelation_is_respected():
    r = _ar1(0.5, mu=0.00018)
    raw = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR, use_effective_n=False)
    eff = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR)
    assert raw["deflated_sharpe_ratio"] >= 0.95      # passes with the raw bar count
    assert eff["deflated_sharpe_ratio"] < 0.95       # fails with the effective sample


def test_effective_n_shrinks_dsr_toward_one_half_and_never_across_it():
    # Below the deflation hurdle a smaller sample moves the DSR UP toward 0.5 (less certain it is bad),
    # but it can never cross 0.5, so it cannot manufacture a pass at the 0.95 bar.
    r = _ar1(0.5, mu=0.00008)
    raw = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR, use_effective_n=False)
    eff = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR)
    assert raw["per_period_sharpe"] < raw["sharpe_hurdle_per_period"]
    assert raw["deflated_sharpe_ratio"] < eff["deflated_sharpe_ratio"] < 0.5


def test_legacy_behaviour_available_and_reports_full_sample():
    r = _ar1(0.5)
    legacy = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR, use_effective_n=False)
    assert legacy["effective_observations"] == legacy["observations"] == len(r)


def test_effective_n_matches_the_gate_diagnostic():
    r = _ar1(0.4)
    gate = newey_west_mean_inference(r)
    out = deflated_sharpe_report(r, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR)
    assert gate["status"] == "OK"
    assert out["effective_observations"] == min(len(r), max(2, gate["effective_observations"]))


def test_insufficient_data_path_is_unchanged():
    out = deflated_sharpe_report(np.arange(10, dtype=float) * 1e-4, _trial_sharpes(), N_TRIALS, BARS_PER_YEAR)
    assert out["status"] == "INSUFFICIENT_DATA"


def _ohlcv(n: int = 3000) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(5)
    close = 100.0 * np.cumprod(1.0 + rng.normal(0.0001, 0.004, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.001, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.001, n)))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": rng.uniform(100, 200, n)}, index=idx
    )


def test_walk_forward_reports_per_fold_training_size_not_full_frame_length():
    """`train_bars` was silently overwritten by an ATR series and reported len(frame) for every fold."""
    from app.trading_core import ai_walk_forward_backtest

    df = _ohlcv()
    out = ai_walk_forward_backtest(df, folds=3, min_train=600)
    folds = out["folds"]
    assert len(folds) >= 2
    sizes = [f["train_bars"] for f in folds]
    assert all(s < len(df) for s in sizes), sizes
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes), sizes  # expanding window grows each fold
