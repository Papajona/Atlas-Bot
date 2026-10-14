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


def test_custody_worker_uses_the_same_strict_balance_parser(monkeypatch):
    import asyncio
    from app import custody_reconciliation
    from app.config import settings

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, payload):
            self.payload = payload

        async def get(self, *args, **kwargs):
            return Response(self.payload)

    monkeypatch.setattr(settings, "usdt_trongrid_base_url", "https://provider.example")
    monkeypatch.setattr(settings, "usdt_tron_usdt_contract", CONTRACT)
    monkeypatch.setattr(settings, "usdt_trongrid_api_key", "unit-test-not-a-real-key")

    valid = Client({"data": [record("1234567", decimals=6)]})
    assert asyncio.run(custody_reconciliation._address_balance(valid, "TTestAddress")) == Decimal("1.234567")

    invalid = Client({"data": [record("1234567", decimals=18)]})
    with pytest.raises(ValueError, match="exactly 6"):
        asyncio.run(custody_reconciliation._address_balance(invalid, "TTestAddress"))
