from decimal import Decimal

import pytest

from app.tron_reconciliation import parse_tron_usdt_balance


CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def record(balance="1234567", decimals=6, address=CONTRACT):
    return {"balance": balance, "token_info": {"address": address, "decimals": decimals}}


def test_parses_raw_tron_balance_without_binary_float_arithmetic():
    assert parse_tron_usdt_balance([record("1234567")], CONTRACT) == Decimal("1.234567")


def test_missing_contract_record_is_zero_but_not_fabricated_metadata():
    assert parse_tron_usdt_balance([record(address="different-contract")], CONTRACT) == Decimal("0")


@pytest.mark.parametrize("payload", [
    [{"balance": "12", "token_info": {"address": CONTRACT}}],
    [{"balance": "12", "token_info": {"address": CONTRACT, "decimals": 18}}],
    [{"balance": "NaN", "token_info": {"address": CONTRACT, "decimals": 6}}],
    [{"balance": "Infinity", "token_info": {"address": CONTRACT, "decimals": 6}}],
    [{"balance": "-1", "token_info": {"address": CONTRACT, "decimals": 6}}],
    [{"balance": "abc", "token_info": {"address": CONTRACT, "decimals": 6}}],
])
def test_malformed_or_unexpected_contract_metadata_fails_closed(payload):
    with pytest.raises(ValueError):
        parse_tron_usdt_balance(payload, CONTRACT)


def test_duplicate_contract_records_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        parse_tron_usdt_balance([record("1"), record("2")], CONTRACT)


@pytest.mark.parametrize("payload", [None, {}, "invalid"])
def test_malformed_provider_data_shape_fails_closed(payload):
    with pytest.raises(ValueError):
        parse_tron_usdt_balance(payload, CONTRACT)
