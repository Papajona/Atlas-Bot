import hashlib

import numpy as np
import pytest

from app.research_validation import (
    deflated_sharpe_report,
    deflated_sharpe_report_from_summary,
    newey_west_mean_inference,
)


def test_compact_full_sample_summary_matches_raw_sample_dsr():
    rng = np.random.default_rng(87)
    returns = rng.normal(0.00015, 0.0012, 4000)
    trials = rng.normal(0.4, 0.6, 25).tolist()
    annualization = 8760

    inference = newey_west_mean_inference(returns)
    std = float(np.std(returns, ddof=1))
    centred = (returns - float(np.mean(returns))) / std
    summary = {
        "status": "OK",
        "observations": len(returns),
        "effective_observations": inference["effective_observations"],
        "per_period_sharpe": float(np.mean(returns) / std),
        "annualized_sharpe": float(np.mean(returns) / std * np.sqrt(annualization)),
        "skew": float(np.mean(centred ** 3)),
        "kurtosis": float(np.mean(centred ** 4)),
        "sample_sha256": hashlib.sha256(np.asarray(returns, dtype="<f8").tobytes()).hexdigest(),
    }

    raw = deflated_sharpe_report(returns, trials, len(trials), annualization)
    compact = deflated_sharpe_report_from_summary(summary, trials, len(trials), annualization)

    assert compact["status"] == "OK"
    assert compact["observations"] == len(returns)
    assert compact["effective_observations"] == inference["effective_observations"]
    assert compact["deflated_sharpe_ratio"] == pytest.approx(raw["deflated_sharpe_ratio"], abs=1e-12)


def test_compact_full_sample_dsr_rejects_incomplete_statistics():
    result = deflated_sharpe_report_from_summary(
        {"status": "INSUFFICIENT_DATA", "observations": 20},
        [0.2, 0.4],
        2,
        252,
    )
    assert result["status"] == "INSUFFICIENT_DATA"
