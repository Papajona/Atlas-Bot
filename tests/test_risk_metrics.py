from datetime import datetime, timedelta, timezone

from app.risk_metrics import performance_ratios

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
RETURNS = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015, 0.005, -0.002, 0.01, 0.012]


def _days(n):
    return [BASE + timedelta(days=i) for i in range(n)]


def test_too_few_samples_returns_zeros_not_constants():
    out = performance_ratios(RETURNS[:5], _days(5), 3.0)
    assert out["sharpe"] == out["calmar"] == out["sortino"] == out["edge_decay_z"] == 0.0


def test_zero_volatility_and_zero_drawdown_are_not_fabricated():
    out = performance_ratios([0.01] * 12, _days(12), 0.0)
    assert (out["sharpe"], out["calmar"], out["sortino"], out["edge_decay_z"]) == (0.0, 0.0, 0.0, 0.0)


def test_annualisation_uses_observed_trade_frequency():
    out = performance_ratios(RETURNS, _days(10), 5.0)
    assert out["annualized"] is True and 400 < out["trades_per_year"] < 412
    assert out["sharpe"] > 0 and out["calmar"] > 0


def test_missing_or_identical_timestamps_are_reported_unannualised():
    assert performance_ratios(RETURNS, None, 5.0)["annualized"] is False
    assert performance_ratios(RETURNS, [BASE] * 10, 5.0)["annualized"] is False


def test_none_nan_and_naive_datetimes_are_handled():
    out = performance_ratios(RETURNS + [None, float("nan")], [d.replace(tzinfo=None) for d in _days(10)] + [None, None], 2.0)
    assert out["sample_size"] == 10 and out["annualized"] is True
