from decimal import Decimal

import pytest

from app.tron_reconciliation import parse_tron_usdt_balance


CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def record(balance="1234567", decimals=6, address=CONTRACT):
    return {"balance": balance, "token_info": {"address": address, "decimals": decimals}}


def test_parses_raw_tron_balance_without_binary_float_arithmetic():
    assert parse_tron_usdt_balance([record("1234567")], CONTRACT) == Decimal("1.234567")


def test_zero_balance_requires_explicit_matching_contract_metadata():
    assert parse_tron_usdt_balance([record("0")], CONTRACT) == Decimal("0")


def test_large_raw_balance_is_scaled_without_decimal_context_rounding():
    raw = "12345678901234567890123456789012345678901234567890"
    expected = Decimal("12345678901234567890123456789012345678901234.567890")
    assert parse_tron_usdt_balance([record(raw)], CONTRACT) == expected


def test_absent_configured_contract_is_missing_evidence_not_zero():
    with pytest.raises(ValueError, match="missing the configured USDT contract"):
        parse_tron_usdt_balance([record(address="different-contract")], CONTRACT)

    with pytest.raises(ValueError, match="missing the configured USDT contract"):
        parse_tron_usdt_balance([], CONTRACT)


@pytest.mark.parametrize("payload", [
    [{"balance": "12", "token_info": {"address": CONTRACT}}],
    [{"balance": "12", "token_info": {"address": CONTRACT, "decimals": 18}}],
    [{"balance": "12", "token_info": {"address": CONTRACT, "decimals": 6.9}}],
    [{"balance": "12", "token_info": {"address": CONTRACT, "decimals": "6"}}],
    [{"balance": "12", "token_info": {"address": CONTRACT, "decimals": True}}],
    [{"balance": "NaN", "token_info": {"address": CONTRACT, "decimals": 6}}],
    [{"balance": "Infinity", "token_info": {"address": CONTRACT, "decimals": 6}}],
    [{"balance": "-1", "token_info": {"address": CONTRACT, "decimals": 6}}],
    [{"balance": "1.5", "token_info": {"address": CONTRACT, "decimals": 6}}],
    [{"balance": "abc", "token_info": {"address": CONTRACT, "decimals": 6}}],
    [{"balance": 1234567.0, "token_info": {"address": CONTRACT, "decimals": 6}}],
])
def test_malformed_or_unexpected_contract_metadata_fails_closed(payload):
    with pytest.raises(ValueError):
        parse_tron_usdt_balance(payload, CONTRACT)


def test_contract_address_comparison_respects_base58_case_sensitivity():
    with pytest.raises(ValueError, match="missing the configured USDT contract"):
        parse_tron_usdt_balance([record(address=CONTRACT.lower())], CONTRACT)


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

    missing = Client({"data": []})
    with pytest.raises(ValueError, match="missing the configured USDT contract"):
        asyncio.run(custody_reconciliation._address_balance(missing, "TTestAddress"))
