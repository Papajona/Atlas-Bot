from app.strategy_lab_validation import strategy_lab_candidate_passes


def _gate(*, passed=True, sharpe=1.2, drawdown=-0.10, trades=250, total_return=0.25):
    return {
        "passed": passed,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "trades": trades,
        "oos_total_return": total_return,
    }


def test_lab_pass_requires_oos_thresholds_even_when_in_sample_is_strong():
    # In-sample numbers are deliberately absent from the helper contract.
    passed, metrics = strategy_lab_candidate_passes(
        _gate(sharpe=0.4, drawdown=-0.50, trades=12, total_return=-0.2),
        validation_available=True,
        min_sharpe=1.0,
        max_drawdown=0.2,
        min_trades=100,
        min_total_return=0.0,
    )
    assert passed is False
    assert metrics["sharpe"] == 0.4


def test_lab_passes_when_oos_gate_and_all_oos_thresholds_pass():
    passed, metrics = strategy_lab_candidate_passes(
        _gate(),
        validation_available=True,
        min_sharpe=1.0,
        max_drawdown=0.2,
        min_trades=100,
        min_total_return=0.0,
    )
    assert passed is True
    assert metrics == {
        "sharpe": 1.2,
        "max_drawdown": -0.10,
        "trades": 250,
        "total_return": 0.25,
    }


def test_lab_rejects_when_oos_gate_is_unavailable_or_fails():
    for gate, available in ((_gate(), False), (_gate(passed=False), True)):
        passed, _ = strategy_lab_candidate_passes(
            gate,
            validation_available=available,
            min_sharpe=0.0,
            max_drawdown=1.0,
            min_trades=0,
            min_total_return=-1.0,
        )
        assert passed is False


def test_lab_rejects_missing_or_nonfinite_oos_metrics():
    for gate in (
        {"passed": True, "sharpe": float("nan"), "max_drawdown": -0.1, "trades": 200, "oos_total_return": 0.2},
        {"passed": True, "sharpe": 1.2, "max_drawdown": None, "trades": 200, "oos_total_return": 0.2},
        {"passed": True, "sharpe": 1.2, "max_drawdown": -0.1, "trades": 200.5, "oos_total_return": 0.2},
        {"passed": True, "sharpe": 1.2, "max_drawdown": -0.1, "trades": 200, "oos_total_return": None},
    ):
        passed, _ = strategy_lab_candidate_passes(
            gate,
            validation_available=True,
            min_sharpe=0.0,
            max_drawdown=1.0,
            min_trades=0,
            min_total_return=-1.0,
        )
        assert passed is False
