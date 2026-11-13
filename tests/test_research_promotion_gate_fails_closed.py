from app.research_validation import oos_promotion_gate


def _oos(**overrides):
    row = {
        "status": "OK",
        "folds": [{"total_return": 0.03} for _ in range(5)],
        "oos_total_return": 0.20,
        "sharpe": 1.4,
        "max_drawdown": -0.10,
        "trades": 300,
        "deflated_sharpe": 0.98,
        "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_evidence_id": "signed-cost-evidence-id",
        "execution_cost_model_complete": True,
        "slippage_model_complete": True,
        "trial_registry_complete": False,
        "inference": {
            "status": "OK",
            "newey_west_t_statistic": 3.0,
            "effective_observations": 500,
        },
    }
    row.update(overrides)
    return row


def _evaluate(row):
    return oos_promotion_gate(
        row,
        min_dsr=0.95,
        require_cost_stress=True,
        min_folds=5,
        min_total_return=0.0,
        min_positive_fold_ratio=0.0,
        max_negative_fold_return=-1.0,
        require_last_fold_positive=False,
        min_sharpe=1.0,
        max_drawdown=-0.20,
        min_trades=100,
        min_mean_t_statistic=1.645,
        min_effective_observations=100,
    )


def test_gate_stays_blocked_without_verified_comparable_trial_registry():
    result = _evaluate(_oos())
    assert result["passed"] is False
    assert "comparable_trial_registry_incomplete" in result["rejection_reasons"]


def test_signed_cost_calibration_does_not_prove_cost_model_was_applied():
    result = _evaluate(_oos(execution_cost_model_complete=False))
    assert result["passed"] is False
    assert result["execution_cost_calibration_ok"] is True
    assert "execution_cost_model_incomplete" in result["rejection_reasons"]


def test_calibration_does_not_prove_volume_and_volatility_slippage_was_applied():
    result = _evaluate(_oos(slippage_model_complete=False))
    assert result["passed"] is False
    assert "volume_volatility_aware_slippage_model_incomplete" in result["rejection_reasons"]


def test_report_fields_cannot_replace_a_failed_cost_calibration():
    result = _evaluate(_oos(
        execution_cost_calibration_status="MISSING_VENUE_CALIBRATION_EVIDENCE",
        execution_cost_calibration_evidence_id=None,
        execution_cost_model_complete=True,
        slippage_model_complete=True,
    ))
    assert result["passed"] is False
    assert "venue_execution_cost_calibration_missing" in result["rejection_reasons"]
