"""reduceOnly reaches derivatives venues only for non-flipping closes; DSR floor in the OOS gate fails closed."""
from pathlib import Path

from app.research_validation import deflated_sharpe_report, oos_promotion_gate

EXEC = Path(__file__).resolve().parents[1] / "app" / "execution.py"


def test_order_call_no_longer_hardcodes_reduce_only_false():
    src = EXEC.read_text()
    call = src[src.rindex("broker.market_order, symbol, side, amount, cid"):]  # crypto path is the last call site (the first is the OANDA demo path)
    call = call[:call.index(")\n")+1]
    assert "_is_derivatives_market(broker)" in call and "exchange_reduce_only" in call and ", False," not in call


def test_exchange_reduce_only_requires_non_flipping_size():
    src = EXEC.read_text()
    assert "amount <= opposing_qty + 1e-9" in src


def _oos(**extra):
    folds = [{"total_return": 0.02}] * 6
    return {"status": "OK", "folds": folds, "oos_total_return": 0.12, **extra}


def test_dsr_floor_off_by_default_and_fails_closed_when_set():
    assert oos_promotion_gate(_oos())["passed"] is True
    assert oos_promotion_gate(_oos(), min_dsr=0.95)["passed"] is False                       # missing DSR -> not a pass
    assert oos_promotion_gate(_oos(deflated_sharpe=0.97), min_dsr=0.95)["passed"] is True
    assert oos_promotion_gate(_oos(deflated_sharpe=0.40), min_dsr=0.95)["passed"] is False


def test_dsr_report_penalises_many_trials():
    import numpy as np
    rng = np.random.default_rng(1)
    net = rng.normal(0.0004, 0.01, 2000)
    sharpes = list(rng.normal(0.5, 0.8, 12))
    few = deflated_sharpe_report(net, sharpes, 3, 8760)
    many = deflated_sharpe_report(net, sharpes, 500, 8760)
    assert few["status"] == many["status"] == "OK"
    assert many["deflated_sharpe_ratio"] < few["deflated_sharpe_ratio"]
    assert deflated_sharpe_report([0.01] * 5, sharpes, 3, 8760)["status"] == "INSUFFICIENT_DATA"
