from decimal import Decimal

import pytest

from app.execution import _exact_reserved_cash


def test_customer_cash_reserve_is_decimal_before_ledger_posting():
    assert _exact_reserved_cash("0.1", "0.2", "250") == Decimal("0.0205")
    assert _exact_reserved_cash("123.45", "0.67", "0") == Decimal("82.7115")


@pytest.mark.parametrize(
    "price,quantity,slippage",
    [
        ("NaN", "1", "10"),
        ("10", "Infinity", "10"),
        ("10", "1", "-1"),
        ("0", "1", "10"),
        ("10", "0", "10"),
    ],
)
def test_customer_cash_reserve_rejects_invalid_values(price, quantity, slippage):
    with pytest.raises(ValueError):
        _exact_reserved_cash(price, quantity, slippage)
