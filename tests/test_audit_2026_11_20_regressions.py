"""Regression tests for the 2026-11-20 audit fixes (stdlib-only modules, no database needed)."""
import math
from datetime import datetime, timedelta, timezone

from app.risk_governor import evaluate_trade
from app.risk_metrics import performance_ratios
from app.trading_requirements import spot_requirement

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
RETURNS = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015, 0.005, -0.002, 0.01, 0.012]


def test_governor_accepts_order_sized_to_advertised_minimum_balance():
    price, stop, amount_min = 50000.0, 49000.0, 0.001
    market = {"limits": {"amount": {"min": amount_min}, "cost": {"min": 5.0}}}
    risk_per_trade, tolerance = 0.005, 0.10
    req = spot_requirement(venue="binance", symbol="BTC/USDT", price=price, market=market,
                           stop_loss_price=stop, risk_per_trade=risk_per_trade, risk_tolerance=tolerance)
    kwargs = dict(side="buy", price=price, quantity=amount_min, live=True, stop_loss_price=stop,
                  take_profit_price=None, signal={"data_quality": {"valid": True}}, spread_bps=1.0,
                  equity=req.risk_required_balance, risk_per_trade=risk_per_trade)
    # Without the tolerance the governor contradicts the requirement it advertised.
    assert evaluate_trade(**kwargs).action == "BLOCK"
    assert evaluate_trade(**kwargs, risk_tolerance=tolerance).action == "ALLOW"


def test_governor_still_blocks_risk_beyond_tolerance():
    kwargs = dict(side="buy", price=100.0, quantity=10.0, live=True, stop_loss_price=90.0,
                  take_profit_price=None, signal={"data_quality": {"valid": True}}, spread_bps=1.0,
                  equity=1000.0, risk_per_trade=0.01, risk_tolerance=0.10)
    # risk cash = 100 > 1000 * 0.01 * 1.10 = 11
    assert "risk_per_trade_limit" in evaluate_trade(**kwargs).reasons


def test_sortino_uses_all_observations_in_downside_deviation():
    ts = [BASE + timedelta(days=10 * i) for i in range(10)]
    out = performance_ratios(RETURNS, ts, 5.0)
    n = len(RETURNS)
    mean = sum(RETURNS) / n
    downside = math.sqrt(sum(min(0.0, r) ** 2 for r in RETURNS) / n)
    root = math.sqrt(out["trades_per_year"])
    assert math.isclose(out["sortino"], mean / downside * root, rel_tol=1e-9)


def test_sharpe_and_edge_decay_use_sample_standard_deviation():
    ts = [BASE + timedelta(days=10 * i) for i in range(10)]
    out = performance_ratios(RETURNS, ts, 5.0)
    n = len(RETURNS)
    mean = sum(RETURNS) / n
    sample_vol = math.sqrt(sum((r - mean) ** 2 for r in RETURNS) / (n - 1))
    assert math.isclose(out["sharpe"], mean / sample_vol * math.sqrt(out["trades_per_year"]), rel_tol=1e-9)
    assert math.isclose(out["edge_decay_z"], mean / (sample_vol / math.sqrt(n)), rel_tol=1e-9)



def test_risk_tolerance_is_finite_nonnegative_and_capped_at_ten_percent():
    from app.risk_governor import MAX_RISK_TOLERANCE, bounded_risk_tolerance

    assert MAX_RISK_TOLERANCE == 0.10
    assert bounded_risk_tolerance(0.05) == 0.05
    assert bounded_risk_tolerance(100.0) == 0.10
    assert bounded_risk_tolerance(-1.0) == 0.0
    assert bounded_risk_tolerance(float("nan")) == 0.0
    assert bounded_risk_tolerance(float("inf")) == 0.0


def test_governor_does_not_accept_more_than_ten_percent_risk_headroom():
    common = dict(
        side="buy", price=100.0, live=True, stop_loss_price=90.0,
        take_profit_price=120.0, signal={"data_quality": {"valid": True}},
        equity=1000.0, risk_per_trade=0.01, risk_tolerance=1000.0,
    )
    # The 10% ceiling permits $11 of stop risk, but not $11.01, regardless
    # of how large an environment-supplied tolerance attempts to become.
    assert evaluate_trade(quantity=1.10, **common).action == "ALLOW"
    blocked = evaluate_trade(quantity=1.101, **common)
    assert blocked.action == "BLOCK"
    assert "risk_per_trade_limit" in blocked.reasons
