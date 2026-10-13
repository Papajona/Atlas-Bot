"""Behavioral regressions for review fixes on billing refunds and CCXT amount precision."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.main as main
from app.config import settings
from app.payout import CCXTPayoutProvider, PayoutError


def test_admin_revenue_rejects_refunds_exceeding_gross_before_database_access(monkeypatch):
    async def allow_auth(*args, **kwargs):
        return {"sub": "finance"}

    async def allow_role(*args, **kwargs):
        return None

    def forbidden_session():
        raise AssertionError("Database must not be touched for an invalid refund payload")

    monkeypatch.setattr(main, "auth", allow_auth)
    monkeypatch.setattr(main, "require_role", allow_role)
    monkeypatch.setattr(main, "SessionLocal", forbidden_session)
    request = main.RevenueEventRequest(
        provider="test", provider_reference="refund-overflow",
        gross_amount=10, refunds=10.01, currency="USD",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.admin_billing_revenue(request, authorization=None, x_admin_token="test"))
    assert exc.value.status_code == 422
    assert exc.value.detail == "Refunds cannot exceed gross amount"


class _PrecisionExchange:
    id = "fake"
    has = {"withdraw": True}

    def __init__(self, precision_value):
        self.precision_value = precision_value
        self.withdraw_amount = None
        self.loaded = False

    def load_markets(self):
        self.loaded = True
        return {}

    def currency_to_precision(self, currency, amount):
        assert self.loaded, "market metadata must load before precision conversion"
        assert currency == "USDT"
        assert Decimal(str(amount)) == Decimal("1.234567")
        return self.precision_value

    def withdraw(self, currency, amount, destination, tag, params):
        self.withdraw_amount = amount
        return {"id": "provider-withdrawal-1", "status": "pending"}


def _provider(exchange):
    provider = object.__new__(CCXTPayoutProvider)
    provider.exchange = exchange
    return provider


def test_ccxt_withdraw_uses_exchange_normalized_amount(monkeypatch):
    monkeypatch.setattr(settings, "payout_client_id_param", "")
    exchange = _PrecisionExchange("1.2345")
    result = asyncio.run(_provider(exchange).send(
        currency="USDT", amount=Decimal("1.234567"), destination="T" + "A" * 33,
        tag=None, network="TRON", idempotency_key="precision-test", metadata={},
    ))
    assert exchange.loaded
    assert exchange.withdraw_amount == "1.2345"
    assert result.provider_id == "provider-withdrawal-1"


def test_ccxt_withdraw_rejects_amount_that_rounds_to_zero_before_provider_call(monkeypatch):
    monkeypatch.setattr(settings, "payout_client_id_param", "")
    exchange = _PrecisionExchange("0")
    with pytest.raises(PayoutError, match="rounds to zero"):
        asyncio.run(_provider(exchange).send(
            currency="USDT", amount=Decimal("0.000001"), destination="T" + "A" * 33,
            tag=None, network="TRON", idempotency_key="precision-zero", metadata={},
        ))
    assert exchange.withdraw_amount is None
