import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import settings
from app.payout import CCXTPayoutProvider, PayoutUnknown
from app.schemas import CustomerWithdrawalCreate, DerivTradeRequest, FundingWebhook
import app.distributed as distributed
import app.withdrawal_security as withdrawal_security


ROOT = Path(__file__).resolve().parents[1]


def test_staging_distributed_controls_fail_closed_without_redis(monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging", raising=False)
    monkeypatch.setattr(settings, "distributed_rate_limit_required", True, raising=False)
    monkeypatch.setattr(settings, "redis_url", "", raising=False)
    assert asyncio.run(distributed.check_redis()) is False
    allowed, _ = asyncio.run(distributed.allow_rate_limit("x", 10))
    assert allowed is False
    assert asyncio.run(distributed.acquire_lock("x")) is False


def test_staging_withdrawal_destination_fails_closed_without_whitelist(monkeypatch):
    monkeypatch.setattr(settings, "environment", "staging", raising=False)
    monkeypatch.setattr(settings, "withdrawal_whitelist_enabled", False, raising=False)
    assert withdrawal_security.destination_allowed("TExampleDestination", "USDT", "TRON") is False


@pytest.mark.parametrize(
    "model,payload",
    [
        (DerivTradeRequest, {"contract_type": "CALL", "underlying_symbol": "R_100", "amount": float("inf")}),
        (CustomerWithdrawalCreate, {"amount": float("inf"), "destination": "T" + "A" * 33}),
        (FundingWebhook, {"customer_auth_user_id": "customer-123", "provider": "x", "provider_reference": "ref", "amount": 10**30, "currency": "USDT"}),
    ],
)
def test_external_amounts_reject_nonfinite_or_unbounded_values(model, payload):
    with pytest.raises(ValidationError):
        model(**payload)


def test_ccxt_full_history_page_is_unknown(monkeypatch):
    provider = CCXTPayoutProvider.__new__(CCXTPayoutProvider)

    class Exchange:
        id = "fake"
        has = {"fetchWithdrawals": True}

    provider.exchange = Exchange()

    async def fake_call(fn, *args, **kwargs):
        return [{"id": f"other-{i}", "info": {}} for i in range(100)]

    monkeypatch.setattr(provider, "_call", fake_call)
    with pytest.raises(PayoutUnknown):
        asyncio.run(provider.recover("missing-id", currency="USDT"))


def test_stale_operator_configuration_is_corrected():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "RISK_PER_TRADE=" in env_example
    assert "MAX_RISK_PER_TRADE=" not in env_example


def test_jwt_decoder_requires_exp_sub_aud_iss():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert 'options={"require": ["exp", "sub", "aud", "iss"]}' in source


def test_android_sources_are_excluded_from_runtime_image():
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for entry in ("app/src/", "app/build.gradle.kts", "app/proguard-rules.pro"):
        assert entry in dockerignore
