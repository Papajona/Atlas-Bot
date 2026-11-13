import numpy as np
import pytest

from app.strategy_selection import multiple_testing_hurdle, paired_return_lead


def test_multiple_testing_hurdle_is_one_sided_bonferroni():
    assert multiple_testing_hurdle(5, alpha=0.05) == pytest.approx(2.326347874, abs=1e-8)
    assert multiple_testing_hurdle(1, alpha=0.05) == pytest.approx(1.644853627, abs=1e-8)


def test_paired_hac_comparison_recognizes_a_clear_net_return_lead():
    rng = np.random.default_rng(41)
    incumbent = rng.normal(0.0, 0.001, 600)
    challenger = rng.normal(0.0008, 0.001, 600)
    result = paired_return_lead(challenger, incumbent, candidate_count=5)
    assert result["status"] == "OK"
    assert result["comparison"] == "PAIRED_CHALLENGER_MINUS_INCUMBENT_NET_RETURNS"
    assert result["significant_lead"] is True
    assert result["effective_observations"] >= 30


def test_paired_hac_comparison_holds_for_a_small_noisy_lead():
    rng = np.random.default_rng(7)
    incumbent = rng.normal(0.0, 0.002, 800)
    challenger = rng.normal(0.00003, 0.002, 800)
    result = paired_return_lead(challenger, incumbent, candidate_count=5)
    assert result["status"] == "OK"
    assert result["significant_lead"] is False
    assert result["reason"] == "paired_lead_below_multiplicity_adjusted_hurdle"


def test_underpowered_regime_slice_never_authorizes_a_switch():
    rng = np.random.default_rng(3)
    incumbent = rng.normal(0.0, 0.001, 45)
    challenger = incumbent + 0.01
    result = paired_return_lead(challenger, incumbent, min_observations=60)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["significant_lead"] is False


def test_misaligned_return_streams_are_rejected():
    result = paired_return_lead([0.1, 0.2, 0.3], [0.1, 0.2])
    assert result["status"] == "INVALID_DATA"
    assert result["significant_lead"] is False
