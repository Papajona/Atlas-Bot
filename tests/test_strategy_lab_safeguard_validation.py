from app.strategy_lab_validation import (
    UNCOMPILED_CANDIDATE_SAFEGUARDS,
    record_validation_unavailable_reason,
    uncompiled_candidate_safeguards,
    strategy_lab_candidate_passes,
)


def test_native_ai_profile_safeguards_are_reported_as_uncompiled():
    spec = {
        "entry_conditions": [
            {"type": "HIGHER_TIMEFRAME_AGREEMENT", "rule": "require higher timeframe agreement"},
            {"type": "CONSERVATIVE_VOLATILITY_CAP", "rule": "limit volatility"},
            {"type": "REGIME_ADAPTATION", "rule": "switch by regime"},
            {"type": "VOLATILITY_SCALED_RISK", "rule": "scale risk"},
        ],
        "exit_conditions": [
            {"type": "TIME_STOP", "rule": "exit after configured bars"},
        ],
    }
    assert uncompiled_candidate_safeguards(spec) == sorted(UNCOMPILED_CANDIDATE_SAFEGUARDS)


def test_base_strategy_conditions_do_not_trigger_the_profile_safeguard_block():
    spec = {
        "entry_conditions": [
            {"type": "TREND_CONFIRMATION", "rule": "trend filters agree"},
            {"type": "MOMENTUM_CONFIRMATION", "rule": "momentum agrees"},
        ],
        "exit_conditions": [{"type": "TREND_EXIT", "rule": "exit when trend reverses"}],
    }
    assert uncompiled_candidate_safeguards(spec) == []


def test_candidate_cannot_pass_when_safeguard_validation_is_unavailable():
    passed, metrics = strategy_lab_candidate_passes(
        {
            "passed": True,
            "sharpe": 2.0,
            "max_drawdown": -0.05,
            "trades": 500,
            "oos_total_return": 0.30,
        },
        validation_available=False,
        min_sharpe=0.5,
        max_drawdown=-0.2,
        min_trades=100,
        min_total_return=0.0,
    )
    assert passed is False
    assert metrics["sharpe"] == 2.0



def test_uncompiled_safeguards_preserve_trial_archive_failure_reason():
    gate = {
        "validation_available": False,
        "validation_unavailable_reason": "TRIAL_HISTORY_RECORDING_FAILED",
    }

    record_validation_unavailable_reason(gate, "UNCOMPILED_STRATEGY_SAFEGUARDS")

    assert gate["validation_unavailable_reason"] == "TRIAL_HISTORY_RECORDING_FAILED"
    assert gate["validation_unavailable_reasons"] == [
        "TRIAL_HISTORY_RECORDING_FAILED",
        "UNCOMPILED_STRATEGY_SAFEGUARDS",
    ]


def test_uncompiled_safeguard_is_primary_reason_when_no_other_failure_exists():
    gate = {"validation_available": False}

    record_validation_unavailable_reason(gate, "UNCOMPILED_STRATEGY_SAFEGUARDS")

    assert gate["validation_unavailable_reason"] == "UNCOMPILED_STRATEGY_SAFEGUARDS"
    assert gate["validation_unavailable_reasons"] == ["UNCOMPILED_STRATEGY_SAFEGUARDS"]

def test_blank_primary_reason_is_replaced_by_first_concrete_reason():
    gate = {
        "validation_available": False,
        "validation_unavailable_reason": "   ",
        "validation_unavailable_reasons": ["TRIAL_HISTORY_RECORDING_FAILED"],
    }

    record_validation_unavailable_reason(gate, "UNCOMPILED_STRATEGY_SAFEGUARDS")

    assert gate["validation_unavailable_reason"] == "TRIAL_HISTORY_RECORDING_FAILED"
    assert gate["validation_unavailable_reasons"] == [
        "TRIAL_HISTORY_RECORDING_FAILED",
        "UNCOMPILED_STRATEGY_SAFEGUARDS",
    ]

