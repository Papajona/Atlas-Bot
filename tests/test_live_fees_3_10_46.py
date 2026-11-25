"""Pure unit tests for app.live_fees (no database needed)."""
import pytest
from decimal import Decimal
from app.live_fees import (
    extract_order_fee_quote, extract_order_fee_quote_decimal, split_symbol,
    fee_quality_is_verified, EXACT, CONVERTED, ESTIMATED, UNVERIFIED,
)

BPS = 5.5


def est(price, qty):
    return price * qty * BPS / 10_000


@pytest.mark.parametrize("order,symbol,price,qty,expected,quality", [
    ({"fee": {"currency": "USDT", "cost": 6.0}}, "BTC/USDT", 60000, 0.1, 6.0, EXACT),
    ({"fee": {"currency": "BTC", "cost": 0.0001}}, "BTC/USDT", 60000, 0.1, 6.0, CONVERTED),
    ({"fees": [{"currency": "USDT", "cost": 2}, {"currency": "USDT", "cost": 3}]}, "ETH/USDT", 3000, 1, 5.0, EXACT),
    ({"fee": {"currency": "BNB", "cost": 0.01}}, "BTC/USDT", 60000, 0.1, 0.0, UNVERIFIED),
    ({}, "BTC/USDT", 60000, 0.1, est(60000, 0.1), ESTIMATED),
    ({"fee": {"currency": "USDT", "cost": 1}}, "BTC/USDT:USDT", 60000, 0.1, 1.0, EXACT),
    ({"fee": {"currency": "USDT", "cost": 1}}, "BTC/USDT", 60000, 0, 0.0, EXACT),
    # A negative cost is a maker rebate: not credited, and NOT replaced by an estimate (that would overcharge).
    ({"fee": {"currency": "USDT", "cost": -1}}, "BTC/USDT", 60000, 0.1, 0.0, EXACT),
])
def test_extract_order_fee_quote(order, symbol, price, qty, expected, quality):
    fee, q, _ = extract_order_fee_quote(order, symbol, price, qty, fallback_taker_bps=BPS)
    assert fee == pytest.approx(expected)
    assert q == quality


def test_split_symbol():
    assert split_symbol("BTC/USDT:USDT") == ("BTC", "USDT")
    assert split_symbol("eth/usdt") == ("ETH", "USDT")


def test_incomplete_fee_record_forces_conservative_estimate():
    fee, quality, note = extract_order_fee_quote(
        {
            "fees": [
                {"currency": "USDT", "cost": "0.10"},
                {"currency": "BNB", "cost": None},
            ]
        },
        "BTC/USDT",
        100.0,
        1.0,
        fallback_taker_bps=10.0,
    )
    assert quality == ESTIMATED
    assert fee >= 0.10
    assert "no usable cost" in note

def test_fee_conversion_keeps_sub_micro_quote_amount_as_decimal():
    fee, quality, note = extract_order_fee_quote_decimal(
        {"fee": {"currency": "USDT", "cost": "0.0000006"}},
        "BTC/USDT",
        "60000.00",
        "0.001",
        fallback_taker_bps="5.5",
    )
    assert fee == __import__("decimal").Decimal("0.0000006")
    assert quality == EXACT
    assert note == ""



def test_fee_cost_without_currency_is_unverified_and_not_estimated():
    fee, quality, note = extract_order_fee_quote_decimal(
        {"fee": {"cost": "0.25"}},
        "BTC/USDT",
        "60000",
        "0.1",
        fallback_taker_bps="5.5",
    )
    assert fee == __import__("decimal").Decimal("0")
    assert quality == UNVERIFIED
    assert not fee_quality_is_verified(quality)
    assert "customer charge withheld" in note



@pytest.mark.parametrize(
    "fees",
    [
        [{"cost": "0"}, {"currency": "USDT", "cost": "NaN"}],
        [{"currency": "USDT", "cost": "NaN"}, {"cost": "0"}],
        [{"cost": "0"}, {"currency": "USDT", "cost": "not-a-number"}],
        [{"currency": "USDT", "cost": "not-a-number"}, {"cost": "0"}],
    ],
)
def test_unverified_missing_currency_fee_stays_withheld_with_later_invalid_record(fees):
    fee, quality, note = extract_order_fee_quote_decimal(
        {"fees": fees},
        "BTC/USDT",
        "60000",
        "0.1",
        fallback_taker_bps="100",
    )

    assert fee == Decimal("0")
    assert quality == UNVERIFIED
    assert not fee_quality_is_verified(quality)
    assert "customer charge withheld" in note
