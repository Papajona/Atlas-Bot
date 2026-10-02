import pytest
from app.research_stats import deflated_sharpe_ratio, expected_max_sharpe, probabilistic_sharpe_ratio


def test_psr_is_half_when_sharpe_equals_benchmark():
    assert probabilistic_sharpe_ratio(0.1, 0.1, 500) == pytest.approx(0.5)


def test_more_trials_means_higher_hurdle_and_lower_dsr():
    var = 0.01 ** 2 * 50
    assert expected_max_sharpe(1, var) == 0.0
    assert expected_max_sharpe(100, var) > expected_max_sharpe(10, var) > 0
    d10 = deflated_sharpe_ratio(0.15, 1000, 10, var)
    d1000 = deflated_sharpe_ratio(0.15, 1000, 1000, var)
    assert d1000 < d10


def test_negative_skew_and_fat_tails_reduce_confidence():
    base = probabilistic_sharpe_ratio(0.1, 0.0, 500)
    worse = probabilistic_sharpe_ratio(0.1, 0.0, 500, skew=-1.5, kurt=8.0)
    assert worse < base
