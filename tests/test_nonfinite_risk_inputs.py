import pytest
from pydantic import ValidationError

from app.risk_governor import evaluate_trade
from app.schemas import ExecuteRequest


@pytest.mark.parametrize("field", ["stop_loss_price", "take_profit_price"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_risk_governor_blocks_nonfinite_protective_prices(field, value):
    values = {
        "stop_loss_price": 90.0,
        "take_profit_price": 110.0,
    }
    values[field] = value
    decision = evaluate_trade(
        side="buy",
        price=100.0,
        quantity=1.0,
        live=False,
        stop_loss_price=values["stop_loss_price"],
        take_profit_price=values["take_profit_price"],
        signal={},
    )
    reason = "invalid_stop_loss" if field == "stop_loss_price" else "invalid_take_profit"
    assert decision.action == "BLOCK"
    assert reason in decision.reasons


@pytest.mark.parametrize(
    "field",
    [
        "quantity",
        "price",
        "score",
        "long_probability",
        "short_probability",
        "flat_probability",
        "stop_loss_price",
        "take_profit_price",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_execute_request_rejects_nonfinite_float_fields(field, value):
    payload = {"side": "buy", "quantity": 1.0}
    payload[field] = value
    with pytest.raises(ValidationError):
        ExecuteRequest.model_validate(payload)
