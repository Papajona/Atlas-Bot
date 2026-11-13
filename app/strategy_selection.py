"""Autocorrelation-aware paired comparisons for strategy switches.

A selector compares strategies on the same market bars. Its uncertainty should
therefore be estimated from the paired difference in their net-return streams,
not from a z-score on two annualized Sharpe point estimates.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Iterable

import numpy as np

from .research_validation import newey_west_mean_inference


def multiple_testing_hurdle(candidate_count: int, alpha: float = 0.05) -> float:
    """One-sided Bonferroni normal hurdle across candidate challengers."""
    if isinstance(candidate_count, bool) or int(candidate_count) != candidate_count or candidate_count < 1:
        raise ValueError("candidate_count must be a positive integer")
    try:
        level = float(alpha)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("alpha must be finite and between 0 and 1") from exc
    if not math.isfinite(level) or not 0.0 < level < 1.0:
        raise ValueError("alpha must be finite and between 0 and 1")
    tail_probability = level / int(candidate_count)
    return float(NormalDist().inv_cdf(1.0 - tail_probability))


def paired_return_lead(
    challenger_returns: Iterable[float],
    incumbent_returns: Iterable[float],
    *,
    candidate_count: int = 5,
    alpha: float = 0.05,
    min_observations: int = 60,
    min_effective_observations: int = 30,
) -> dict:
    """Test whether a challenger beats the incumbent on paired net returns.

    The input series must refer to the same ordered market bars. The test uses a
    Newey-West HAC standard error on challenger-minus-incumbent returns, and
    requires both raw and effective observation floors before authorizing a
    switch. Missing, mismatched, or underpowered data always means HOLD.
    """
    try:
        challenger = np.asarray(list(challenger_returns), dtype=float)
        incumbent = np.asarray(list(incumbent_returns), dtype=float)
    except (TypeError, ValueError, OverflowError):
        return {"status": "INVALID_DATA", "significant_lead": False, "reason": "non_numeric_return_stream"}
    if challenger.ndim != 1 or incumbent.ndim != 1 or challenger.shape != incumbent.shape:
        return {
            "status": "INVALID_DATA",
            "significant_lead": False,
            "reason": "paired_return_streams_not_aligned",
        }
    valid = np.isfinite(challenger) & np.isfinite(incumbent)
    difference = challenger[valid] - incumbent[valid]
    if len(difference) < max(30, int(min_observations)):
        return {
            "status": "INSUFFICIENT_DATA",
            "significant_lead": False,
            "reason": "insufficient_paired_observations",
            "observations": int(len(difference)),
            "minimum_observations": int(min_observations),
        }
    inference = newey_west_mean_inference(difference)
    if inference.get("status") != "OK":
        return {
            "status": inference.get("status", "INVALID_INFERENCE"),
            "significant_lead": False,
            "reason": "paired_newey_west_inference_unavailable",
            "observations": int(len(difference)),
            "inference": inference,
        }
    observations = int(inference["observations"])
    effective = int(inference["effective_observations"])
    hurdle = multiple_testing_hurdle(candidate_count, alpha)
    t_stat = float(inference["newey_west_t_statistic"])
    mean_difference = float(inference["mean_return_per_period"])
    effective_ok = effective >= int(min_effective_observations)
    significant = bool(
        observations >= int(min_observations)
        and effective_ok
        and mean_difference > 0.0
        and t_stat >= hurdle
    )
    reason = (
        "significant_paired_net_return_lead"
        if significant
        else "insufficient_effective_observations"
        if not effective_ok
        else "paired_lead_below_multiplicity_adjusted_hurdle"
    )
    return {
        "status": "OK",
        "significant_lead": significant,
        "reason": reason,
        "observations": observations,
        "effective_observations": effective,
        "mean_return_difference_per_period": mean_difference,
        "newey_west_t_statistic": t_stat,
        "significance_hurdle": hurdle,
        "candidate_count": int(candidate_count),
        "alpha": float(alpha),
        "inference": inference,
        "comparison": "PAIRED_CHALLENGER_MINUS_INCUMBENT_NET_RETURNS",
    }
