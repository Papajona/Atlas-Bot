"""Regression tests for Atlas multi-market safety boundaries."""
import pytest
from pydantic import ValidationError

from app.forex_oanda import OandaConfig, OandaError
from app.schemas import CustomerBotStartRequest


@pytest.mark.parametrize("asset,symbol", [
    ("forex", "EUR_USD"),
    ("commodity", "XAU_USD"),
    ("crypto", "BTC/USDT"),
])
def test_customer_bot_request_accepts_supported_market_families(asset, symbol):
    request = CustomerBotStartRequest(asset=asset, symbol=symbol)
    assert request.asset == asset


@pytest.mark.parametrize("asset", ["stocks", "indices", "options", ""])
def test_customer_bot_request_rejects_unimplemented_asset_families(asset):
    with pytest.raises(ValidationError):
        CustomerBotStartRequest(asset=asset, symbol="EXAMPLE")


def test_oanda_adapter_is_practice_only():
    with pytest.raises(OandaError, match="permanently practice/demo-only"):
        OandaConfig(account_id="practice-account", token="test-token", practice=False)


def test_oanda_adapter_defaults_to_practice():
    config = OandaConfig(account_id="practice-account", token="test-token")
    assert config.practice is True
    assert config.base_url == "https://api-fxpractice.oanda.com"
