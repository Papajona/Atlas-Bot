from decimal import Decimal

import pytest

from app.execution import _exact_realized_pnl


def test_realized_pnl_uses_decimal_arithmetic_for_decimal_input_strings():
    assert _exact_realized_pnl("0.3", "0.3", "0.1", 1) == Decimal("0.06")
    assert _exact_realized_pnl("0.3", "0.1", "0.3", -1) == Decimal("0.06")


@pytest.mark.parametrize(
    "quantity,fill,entry,direction",
    [
        ("NaN", "10", "9", 1),
        ("1", "Infinity", "9", 1),
        ("1", "10", "0", 1),
        ("-1", "10", "9", 1),
        ("1", "10", "9", 0),
    ],
)
def test_realized_pnl_rejects_invalid_values(quantity, fill, entry, direction):
    with pytest.raises(ValueError):
        _exact_realized_pnl(quantity, fill, entry, direction)
