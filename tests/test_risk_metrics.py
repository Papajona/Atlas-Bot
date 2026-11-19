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


def test_annualisation_uses_intervals_not_observation_count():
    # Ten outcomes over nine 10-day intervals means one observation per 10 days.
    timestamps = [BASE + timedelta(days=10 * i) for i in range(10)]
    out = performance_ratios(RETURNS, timestamps, 5.0)
    assert out["annualized"] is True
    assert abs(out["trades_per_year"] - 36.525) < 0.01
    assert out["sharpe"] > 0 and out["calmar"] > 0


def test_short_or_bursty_samples_are_not_annualized():
    timestamps = [BASE + timedelta(seconds=i) for i in range(10)]
    out = performance_ratios(RETURNS, timestamps, 5.0)
    assert out["annualized"] is False
    assert out["trades_per_year"] is None
    assert out["sharpe"] < 100 and out["sortino"] < 100


def test_missing_or_identical_timestamps_are_reported_unannualised():
    assert performance_ratios(RETURNS, None, 5.0)["annualized"] is False
    assert performance_ratios(RETURNS, [BASE] * 10, 5.0)["annualized"] is False


def test_none_nan_and_naive_datetimes_are_handled():
    timestamps = [BASE + timedelta(days=10 * i) for i in range(10)]
    out = performance_ratios(
        RETURNS + [None, float("nan")],
        [d.replace(tzinfo=None) for d in timestamps] + [None, None],
        2.0,
    )
    assert out["sample_size"] == 10 and out["annualized"] is True
