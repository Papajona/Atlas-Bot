import json

import pytest
from app.execution_optimizer import (
    BookLevel, build_execution_plan, executable_vwap, execution_plan_payload,
)


def test_vwap_walks_multiple_levels():
    vwap, remaining = executable_vwap([BookLevel(100, 2), BookLevel(101, 3)], 4)
    assert remaining == 0
    assert round(vwap, 6) == 100.5


def test_insufficient_depth_blocks_trade():
    plan = build_execution_plan(
        side="buy", quantity=10, best_price=100,
        levels=[BookLevel(100, 2)], maker_fee_bps=1, taker_fee_bps=5
    )
    assert plan.order_type == "NO_TRADE"
    assert plan.slices == 0


def test_high_urgency_prefers_market_when_cost_is_bounded():
    plan = build_execution_plan(
        side="buy", quantity=1, best_price=100,
        levels=[BookLevel(100, 1)], maker_fee_bps=1, taker_fee_bps=5,
        urgency=1.0, max_adverse_bps=20
    )
    assert plan.order_type == "MARKET"


def test_low_urgency_can_choose_maker_when_modelled_cost_is_lower():
    plan = build_execution_plan(
        side="buy", quantity=1, best_price=100,
        levels=[BookLevel(100, 1)], maker_fee_bps=0.5, taker_fee_bps=10,
        urgency=0.1, max_adverse_bps=50
    )
    assert plan.order_type == "LIMIT_MAKER"


def test_negative_fee_is_rejected():
    with pytest.raises(ValueError):
        build_execution_plan(side="buy", quantity=1, best_price=100,
                             levels=[BookLevel(100, 1)], maker_fee_bps=-1, taker_fee_bps=5)



@pytest.mark.parametrize(
    "levels,quantity",
    [
        ([BookLevel(float("nan"), 1.0)], 1.0),
        ([BookLevel(100.0, float("inf"))], 1.0),
        ([BookLevel(100.0, 1.0)], float("nan")),
        ([BookLevel(100.0, 1.0)], float("inf")),
    ],
)
def test_non_finite_book_values_and_quantities_are_rejected(levels, quantity):
    with pytest.raises(ValueError, match="finite|invalid"):
        executable_vwap(levels, quantity)


def test_insufficient_depth_plan_serializes_as_strict_json():
    plan = build_execution_plan(
        side="buy", quantity=10, best_price=100,
        levels=[BookLevel(100, 2)], maker_fee_bps=1, taker_fee_bps=5
    )
    payload = execution_plan_payload(plan)
    assert payload["total_cost_bps"] is None
    json.dumps(payload, allow_nan=False)


def test_non_finite_execution_cost_parameters_are_rejected():
    with pytest.raises(ValueError, match="finite"):
        build_execution_plan(
            side="buy", quantity=1, best_price=100,
            levels=[BookLevel(100, 1)], maker_fee_bps=1, taker_fee_bps=5,
            max_adverse_bps=float("nan"),
        )
