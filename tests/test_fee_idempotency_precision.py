from decimal import Decimal

from app.execution import _money_ref


def test_cumulative_fee_reference_does_not_round_to_quantity_precision():
    assert _money_ref(Decimal("0.0000006")) != _money_ref(Decimal("0.0000012"))
    assert _money_ref(Decimal("1.23000000")) == _money_ref(Decimal("1.23"))
    assert _money_ref(Decimal("0")) == "0"
