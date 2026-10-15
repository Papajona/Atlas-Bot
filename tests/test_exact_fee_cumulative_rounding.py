from decimal import Decimal
from types import SimpleNamespace

from app.execution import _prepare_live_fee, _money_ref


def test_cumulative_fee_uses_ledger_quantization_not_per_delta_rounding():
    trade = SimpleNamespace(fee=Decimal("0"), symbol="BTC/USDT")
    first_delta, first_cumulative, quality, _ = _prepare_live_fee(
        trade, {"fee": {"currency": "USDT", "cost": "0.0000006"}}, 1.0, 1.0, 5.5
    )
    assert first_delta == Decimal("0.000001")
    assert first_cumulative == Decimal("0.000001")

    second_delta, second_cumulative, _, _ = _prepare_live_fee(
        trade, {"fee": {"currency": "USDT", "cost": "0.0000012"}}, 1.0, 1.0, 5.5
    )
    assert second_delta == Decimal("0")
    assert second_cumulative == Decimal("0.000001")

    third_delta, third_cumulative, _, _ = _prepare_live_fee(
        trade, {"fee": {"currency": "USDT", "cost": "0.0000016"}}, 1.0, 1.0, 5.5
    )
    assert third_delta == Decimal("0.000001")
    assert third_cumulative == Decimal("0.000002")
    assert quality == "exact"
    assert _money_ref(first_cumulative) != _money_ref(third_cumulative)
