import pytest
from pydantic import ValidationError

from app.schemas import CustomerWithdrawalCreate

DESTINATION = "T" + "A" * 33


def _make(amount):
    return CustomerWithdrawalCreate(amount=amount, destination=DESTINATION)


def test_normal_amount_accepted():
    assert _make(12.5).amount == 12.5


@pytest.mark.parametrize("bad", [0, -1, 1e30, 1_000_000_001, 0.0000001, float("inf"), float("nan")])
def test_bad_amounts_rejected(bad):
    with pytest.raises(ValidationError):
        _make(bad)
