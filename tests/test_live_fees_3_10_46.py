"""Pure unit tests for app.live_fees (no database needed)."""
import pytest
from app.live_fees import extract_order_fee_quote, split_symbol, EXACT, CONVERTED, ESTIMATED

BPS = 5.5


def est(price, qty):
    return price * qty * BPS / 10_000


@pytest.mark.parametrize("order,symbol,price,qty,expected,quality", [
    ({"fee": {"currency": "USDT", "cost": 6.0}}, "BTC/USDT", 60000, 0.1, 6.0, EXACT),
    ({"fee": {"currency": "BTC", "cost": 0.0001}}, "BTC/USDT", 60000, 0.1, 6.0, CONVERTED),
    ({"fees": [{"currency": "USDT", "cost": 2}, {"currency": "USDT", "cost": 3}]}, "ETH/USDT", 3000, 1, 5.0, EXACT),
    ({"fee": {"currency": "BNB", "cost": 0.01}}, "BTC/USDT", 60000, 0.1, est(60000, 0.1), ESTIMATED),
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
