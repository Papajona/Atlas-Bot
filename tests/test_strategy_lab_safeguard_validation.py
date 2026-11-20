from app.strategy_lab_validation import (
    UNCOMPILED_CANDIDATE_SAFEGUARDS,
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
