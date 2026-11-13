from pathlib import Path

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


def test_lab_endpoint_archives_trial_and_uses_oos_gate_for_pass():
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    start = source.index('@app.post("/api/customer/strategy-lab/candidates/{candidate_id}/validate")')
    end = source.index('@app.get("/api/customer/strategy-lab/candidates/{candidate_id}/runs")', start)
    endpoint = source[start:end]

    assert "archive_research_trials(result, req)" in endpoint
    assert 'archive_recording_ok = trial_archive_recording.get("status") in {"APPENDED", "REPLAYED"}' in endpoint
    assert "strategy_lab_candidate_passes(" in endpoint
    assert 'gate["sharpe"]>=' not in endpoint
    assert 'gate["max_drawdown"]>=' not in endpoint
    assert 'gate["trades"]>=' not in endpoint
    assert 'gate["total_return"]>=' not in endpoint
    assert '"in_sample_note": "Selected-on data. Diagnostic only; never a pass criterion."' in endpoint
