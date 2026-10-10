import pytest
from pydantic import ValidationError

from app.risk_governor import evaluate_trade
from app.schemas import ExecuteRequest

def test_live_requires_stop():
    d=evaluate_trade(side="buy",price=100,quantity=1,live=True,stop_loss_price=None,take_profit_price=None,signal={})
    assert d.action=="BLOCK" and "live_protective_stop_missing" in d.reasons

def test_paper_valid_trade_allowed():
    d=evaluate_trade(side="buy",price=100,quantity=1,live=False,stop_loss_price=95,take_profit_price=110,signal={})
    assert d.action=="ALLOW"

def test_spread_guard():
    d=evaluate_trade(side="buy",price=100,quantity=1,live=True,stop_loss_price=95,take_profit_price=110,signal={},spread_bps=150,max_spread_bps=100)
    assert d.action=="BLOCK"


def test_risk_per_trade_limit_blocks_oversized_stop_risk():
    d=evaluate_trade(side="buy",price=100,quantity=10,live=True,stop_loss_price=90,take_profit_price=120,signal={},equity=1000,risk_per_trade=0.005)
    assert d.action=="BLOCK" and "risk_per_trade_limit" in d.reasons

def test_risk_per_trade_limit_allows_sized_position():
    d=evaluate_trade(side="buy",price=100,quantity=0.5,live=True,stop_loss_price=90,take_profit_price=120,signal={},equity=1000,risk_per_trade=0.005,risk_tolerance=0.10)
    assert d.action=="ALLOW"


def test_live_execution_spread_is_blocked_above_configured_ceiling():
    d = evaluate_trade(side="buy", price=101, quantity=1, live=True, stop_loss_price=95,
                       take_profit_price=110, signal={}, spread_bps=101, max_spread_bps=100)
    assert d.action == "BLOCK"
    assert "spread_guard" in d.reasons


@pytest.mark.parametrize("field,reason", [
    ("stop_loss_price", "invalid_stop_loss_price"),
    ("take_profit_price", "invalid_take_profit_price"),
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_risk_governor_blocks_non_finite_protective_prices(field, reason, value):
    values = {"stop_loss_price": 95.0, "take_profit_price": 110.0}
    values[field] = value
    decision = evaluate_trade(
        side="buy", price=100.0, quantity=1.0, live=False,
        stop_loss_price=values["stop_loss_price"],
        take_profit_price=values["take_profit_price"], signal={},
    )
    assert decision.action == "BLOCK"
    assert reason in decision.reasons


@pytest.mark.parametrize("field", [
    "quantity", "price", "score", "long_probability", "short_probability",
    "flat_probability", "stop_loss_price", "take_profit_price",
])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_execute_request_rejects_non_finite_float_fields(field, value):
    payload = {"side": "buy", "quantity": 1.0, field: value}
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate(payload)
