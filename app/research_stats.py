"""Formal multiple-testing statistics for strategy promotion (stdlib only).

Implements the Probabilistic and Deflated Sharpe Ratio from Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality". All Sharpe inputs are PER-PERIOD (not annualized).
NOT yet wired into the promotion gate: call deflated_sharpe_ratio() with the number of strategy variants actually tried.
"""
from __future__ import annotations

import math
from statistics import NormalDist

_N = NormalDist()
EULER_MASCHERONI = 0.5772156649015329


def probabilistic_sharpe_ratio(sr: float, sr_benchmark: float, n_obs: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """P(true Sharpe > sr_benchmark) given an observed per-period Sharpe over n_obs returns. kurt is RAW kurtosis (normal = 3)."""
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if denom <= 0:
        return 0.0
    return _N.cdf((sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, sharpe_variance: float) -> float:
    """Expected maximum per-period Sharpe among n_trials skill-less strategies (the selection-bias hurdle SR0)."""
    if n_trials <= 1 or sharpe_variance <= 0:
        return 0.0
    g = EULER_MASCHERONI
    return math.sqrt(sharpe_variance) * ((1 - g) * _N.inv_cdf(1 - 1.0 / n_trials) + g * _N.inv_cdf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe_ratio(sr: float, n_obs: int, n_trials: int, sharpe_variance: float, skew: float = 0.0, kurt: float = 3.0) -> float:
    """Probability the selected strategy's Sharpe is real after correcting for n_trials of selection. Require e.g. >= 0.95."""
    return probabilistic_sharpe_ratio(sr, expected_max_sharpe(n_trials, sharpe_variance), n_obs, skew, kurt)
