"""Costs, not the market, decide most bot outcomes: promotion must survive stressed costs; decayed edge must be detectable."""
import numpy as np

from app.research_validation import cost_breakeven_report, edge_decay_check, oos_promotion_gate


def _series(edge_bps_per_turn, turn_per_bar=0.05, n=4000, seed=7, noise=0.0002):
    rng = np.random.default_rng(seed)
    turnover = np.full(n, turn_per_bar)
    gross = turnover * edge_bps_per_turn / 10_000.0 + rng.normal(0, noise, n)
    return gross, turnover


def test_no_edge_per_turnover_fails_even_before_stress():
    g, t = _series(edge_bps_per_turn=3.0)                      # earns 3 bps per unit traded, pays 6.5 bps
    r = cost_breakeven_report(g, t, taker_bps=5.5, slippage_bps=1.0, bars_per_year=8760)
    assert r["status"] == "OK" and r["annual_net_return"] < 0 and r["stress_ok"] is False


def test_edge_must_survive_double_costs():
    g, t = _series(edge_bps_per_turn=10.0)                     # beats 6.5 bps but not 13 bps
    r = cost_breakeven_report(g, t, taker_bps=5.5, slippage_bps=1.0, bars_per_year=8760, stress_multiplier=2.0)
    assert r["annual_net_return"] > 0 and r["annual_net_return_stressed"] < 0 and r["stress_ok"] is False
    g2, t2 = _series(edge_bps_per_turn=30.0)
    r2 = cost_breakeven_report(g2, t2, taker_bps=5.5, slippage_bps=1.0, bars_per_year=8760, stress_multiplier=2.0)
    assert r2["stress_ok"] is True and r2["cost_headroom"] > 2.0


def test_turnover_is_the_cost_lever():
    g, t = _series(edge_bps_per_turn=12.0, turn_per_bar=0.05)
    hi = cost_breakeven_report(g, t, taker_bps=5.5, slippage_bps=1.0, bars_per_year=8760)
    lo = cost_breakeven_report(g, t / 4, taker_bps=5.5, slippage_bps=1.0, bars_per_year=8760)   # same gross, quarter the trading
    assert lo["annual_cost_drag"] == hi["annual_cost_drag"] / 4 or abs(lo["annual_cost_drag"] - hi["annual_cost_drag"] / 4) < 1e-12


def test_insufficient_data_is_not_a_pass():
    assert cost_breakeven_report([0.01] * 5, [0.1] * 5, taker_bps=5, slippage_bps=1, bars_per_year=8760)["stress_ok"] is False


def _oos(**extra):
    return {
        "status": "OK",
        "folds": [{"total_return": 0.02}] * 6,
        "oos_total_return": 0.12,
        "sharpe": 0.8,
        "max_drawdown": -0.10,
        "trades": 150,
        "deflated_sharpe": 0.97,
        "cost_stress_ok": True,
        "financing_cost_model_complete": True,
        "intrabar_stop_target_model_complete": True,
        "execution_cost_model_complete": True,
        "execution_cost_calibration_status": "VERIFIED",
        "execution_cost_calibration_evidence_id": "fixture:venue-cost-calibration",
        "trial_registry_complete": True,
        **extra,
    }


def test_gate_is_strict_by_default_and_fails_closed_without_evidence():
    assert oos_promotion_gate(_oos())["passed"] is True
    assert oos_promotion_gate(_oos(cost_stress_ok=None))["passed"] is False
    assert oos_promotion_gate(_oos(cost_stress_ok=False))["passed"] is False
    assert oos_promotion_gate(_oos(trial_registry_complete=False))["passed"] is False
    assert oos_promotion_gate(_oos(deflated_sharpe=0.94))["passed"] is False
    assert oos_promotion_gate(_oos(trades=99))["passed"] is False
    assert oos_promotion_gate(_oos(max_drawdown=-0.21))["passed"] is False


def test_edge_decay_detects_a_vanished_edge_but_not_noise():
    rng = np.random.default_rng(11)
    healthy = rng.normal(0.0004, 0.004, 400)
    assert edge_decay_check(healthy, expected_mean=0.0004)["status"] == "OK"
    decayed = rng.normal(-0.0006, 0.004, 400)                    # edge gone and slightly negative
    assert edge_decay_check(decayed, expected_mean=0.0004)["status"] == "HALT_NEW_ENTRIES"
    assert edge_decay_check(decayed[:20], expected_mean=0.0004)["status"] == "INSUFFICIENT_DATA"


def test_customer_ui_carries_risk_disclosure():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "customer.html").read_text()
    assert 'id="riskDisclosure"' in html and "lose some or all" in html and "hypothetical" in html


def test_fixed_carry_drag_is_included_in_cost_stress():
    gross = np.full(400, 0.001, dtype=float)
    turnover = np.full(400, 0.05, dtype=float)
    carry = np.full(400, 0.001, dtype=float)
    report = cost_breakeven_report(
        gross,
        turnover,
        taker_bps=5.5,
        slippage_bps=1.0,
        bars_per_year=252,
        stress_multiplier=2.0,
        fixed_cost_drag=carry,
    )
    assert report["status"] == "OK"
    assert np.isclose(report["annual_fixed_cost_drag"], 0.252, rtol=0, atol=1e-12)
    assert report["annual_net_return"] < 0
    assert report["stress_ok"] is False


def test_invalid_fixed_cost_drag_fails_closed():
    report = cost_breakeven_report(
        [0.001] * 40,
        [0.05] * 40,
        taker_bps=5.5,
        slippage_bps=1.0,
        bars_per_year=252,
        fixed_cost_drag=[float("nan")] * 40,
    )
    assert report["status"] == "INVALID_COST_INPUT"
    assert report["stress_ok"] is False


def test_gate_requires_financing_and_intrabar_execution_evidence():
    passing = _oos()
    assert oos_promotion_gate(passing)["passed"] is True
    required = (
        "financing_cost_model_complete",
        "intrabar_stop_target_model_complete",
        "execution_cost_model_complete",
    )
    for key in required:
        without_evidence = {name: value for name, value in passing.items() if name != key}
        report = oos_promotion_gate(without_evidence)
        assert report["passed"] is False
        assert key.removesuffix("_model_complete") in " ".join(report["rejection_reasons"])
