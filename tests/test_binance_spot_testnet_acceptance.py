from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.binance_spot_testnet_acceptance import (
    active_spot_api_urls,
    cleanup_new_spot_base,
    require_testnet_spot_endpoints,
)

ROOT = Path(__file__).resolve().parents[1]


def test_active_spot_endpoints_require_testnet_not_inactive_url_strings():
    client = SimpleNamespace(
        urls={
            "api": {
                "public": "https://api.binance.com/api/v3",
                "private": "https://api.binance.com/api/v3",
                "inactiveTestnetExample": "https://testnet.binance.vision/api/v3",
            }
        }
    )
    assert active_spot_api_urls(client) == {
        "public": "https://api.binance.com/api/v3",
        "private": "https://api.binance.com/api/v3",
    }
    with pytest.raises(RuntimeError, match="active CCXT public and private API endpoints"):
        require_testnet_spot_endpoints(client)


def test_active_spot_public_and_private_endpoints_must_both_be_testnet():
    good = SimpleNamespace(
        urls={
            "api": {
                "public": "https://testnet.binance.vision/api/v3",
                "private": "https://testnet.binance.vision/api/v3",
                "sapi": "https://api.binance.com/sapi/v1",
            }
        }
    )
    assert set(require_testnet_spot_endpoints(good)) == {"public", "private"}

    mixed = SimpleNamespace(
        urls={
            "api": {
                "public": "https://testnet.binance.vision/api/v3",
                "private": "https://api.binance.com/api/v3",
            }
        }
    )
    with pytest.raises(RuntimeError):
        require_testnet_spot_endpoints(mixed)


def test_testnet_endpoint_must_be_https_and_exact_host():
    for url in (
        "http://testnet.binance.vision/api/v3",
        "https://testnet.binance.vision.attacker.example/api/v3",
        "https://user@testnet.binance.vision/api/v3",
        "https://api.binance.com/api/v3",
    ):
        client = SimpleNamespace(urls={"api": {"public": url, "private": url}})
        with pytest.raises(RuntimeError):
            require_testnet_spot_endpoints(client)


def test_cleanup_flattens_only_newly_received_base_and_tolerates_non_sellable_dust():
    class FakeClient:
        def __init__(self):
            self.base_free = 2.0
            self.cancelled = []

        def cancel_order(self, order_id, symbol):
            self.cancelled.append(order_id)

        def fetch_balance(self):
            return {"free": {"BTC": str(self.base_free)}}

    class FakeBroker:
        def __init__(self, client):
            self.client = client
            self.sells = []

        def normalize_amount(self, symbol, amount):
            return str(int(amount * 1_000_000) / 1_000_000)

        def market_order(self, symbol, side, amount, client_order_id):
            self.sells.append((symbol, side, amount, client_order_id))
            self.client.base_free -= amount
            return {"id": "sell-1"}

        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": "closed", "filled": self.sells[-1][2]}

    client = FakeClient()
    broker = FakeBroker(client)
    result = cleanup_new_spot_base(
        broker,
        client,
        symbol="BTC/USDT",
        base_asset="BTC",
        initial_free_base=1.0,
        min_amount=0.001,
        stop_id="stop-1",
        stop_client_id="stop-client-1",
    )

    assert client.cancelled == ["stop-1"]
    assert len(broker.sells) == 1
    assert broker.sells[0][2] == pytest.approx(1.0)
    assert result["ok"] is True
    assert result["remaining_base"] == "0"
    assert result["residual_dust"] == "0"


def test_cleanup_reconciles_stop_when_creation_response_lost():
    class FakeClient:
        def __init__(self):
            self.base_free = 1.0
            self.cancelled = []
            self.open_orders = [
                {"id": "stop-2", "clientOrderId": "stop-client-2", "info": {}}
            ]

        def fetch_open_orders(self, symbol):
            return self.open_orders

        def cancel_order(self, order_id, symbol):
            self.cancelled.append(order_id)
            self.open_orders = []

        def fetch_balance(self):
            return {"free": {"BTC": str(self.base_free)}}

    class FakeBroker:
        def normalize_amount(self, symbol, amount):
            return str(int(amount * 1_000_000) / 1_000_000)

        def market_order(self, symbol, side, amount, client_order_id):
            return {"id": "sell-2"}

        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": "closed", "filled": "1.0"}

    client = FakeClient()
    result = cleanup_new_spot_base(
        FakeBroker(),
        client,
        symbol="BTC/USDT",
        base_asset="BTC",
        initial_free_base=1.0,
        min_amount=0.001,
        stop_id=None,
        stop_client_id="stop-client-2",
    )
    assert client.cancelled == ["stop-2"]
    assert result["ok"] is True
    assert result["remaining_base"] == "0"


def test_binance_spot_harness_requires_opt_in_sandbox_and_verified_cleanup_boundary():
    source = (ROOT / "scripts/binance_spot_testnet_acceptance.py").read_text(encoding="utf-8")
    assert 'os.getenv("ATLAS_BINANCE_TESTNET", "").strip().lower() != "true"' in source
    assert "require_testnet_spot_endpoints(client)" in source
    assert "if not broker.config.sandbox:" in source
    assert "enable_withdrawals=False" in source
    assert "universal_transfer=False" in source
    assert "finally:" in source
    assert "cleanup_new_spot_base(" in source


def test_binance_spot_harness_uses_spot_stop_loss_not_derivatives_reduce_only():
    source = (ROOT / "scripts/binance_spot_testnet_acceptance.py").read_text(encoding="utf-8")
    assert '"STOP_LOSS"' in source
    assert '"stopPrice"' in source
    assert "reduceOnly" not in source


def test_binance_spot_harness_reconciles_after_flatten():
    source = (ROOT / "scripts/binance_spot_testnet_acceptance.py").read_text(encoding="utf-8")
    assert "fetch_balance" in source
    assert "remaining_base" in source
    assert "sellable_residual" in source
