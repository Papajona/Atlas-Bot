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
    return {"status": "OK", "folds": [{"total_return": 0.02}] * 6, "oos_total_return": 0.12, **extra}


def test_gate_cost_stress_off_by_default_and_fails_closed_when_required():
    assert oos_promotion_gate(_oos())["passed"] is True
    assert oos_promotion_gate(_oos(), require_cost_stress=True)["passed"] is False                 # no evidence -> no promotion
    assert oos_promotion_gate(_oos(cost_stress_ok=False), require_cost_stress=True)["passed"] is False
    assert oos_promotion_gate(_oos(cost_stress_ok=True), require_cost_stress=True)["passed"] is True


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
