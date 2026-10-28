from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.binance_spot_testnet_acceptance import (
    _net_filled_base,
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
        drill_base_amount=Decimal("1.0"),
        min_cost=Decimal("0"),
        stop_id="stop-1",
        stop_client_id=None,
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
        drill_base_amount=Decimal("1.0"),
        min_cost=Decimal("0"),
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



def test_stop_absence_is_not_assumed_after_ambiguous_create_and_cleanup_does_not_sell():
    class Client:
        def __init__(self):
            self.base_free = "1.0"
            self.queries = 0
            self.sells_forbidden = False

        def fetch_open_orders(self, symbol):
            self.queries += 1
            return []

        def fetch_balance(self):
            return {"free": {"BTC": self.base_free}}

    class Broker:
        def __init__(self):
            self.sells = []

        def normalize_amount(self, symbol, amount):
            return str(int(amount * 1_000_000) / 1_000_000)

        def market_order(self, *args):
            self.sells.append(args)
            return {"id": "must-not-sell"}

    client, broker = Client(), Broker()
    result = cleanup_new_spot_base(
        broker, client, symbol="BTC/USDT", base_asset="BTC",
        initial_free_base=Decimal("0"), drill_base_amount=Decimal("1.0"),
        min_amount=Decimal("0.001"), min_cost=Decimal("0"),
        stop_id=None, stop_client_id="ambiguous-stop",
    )
    assert client.queries == 3
    assert broker.sells == []
    assert result["ok"] is False
    assert any("refusing to flatten" in error for error in result["errors"])


def test_cleanup_respects_minimum_notional_and_reports_non_sellable_residual():
    class Client:
        def __init__(self):
            self.base_free = "0.0005"

        def fetch_balance(self):
            return {"free": {"BTC": self.base_free}}

    class Broker:
        def __init__(self):
            self.sells = []

        def normalize_amount(self, symbol, amount):
            return str(int(amount * 1_000_000) / 1_000_000)

        def ticker(self, symbol):
            return {"last": "100"}

        def market_order(self, *args):
            self.sells.append(args)
            return {"id": "unexpected"}

    client, broker = Client(), Broker()
    result = cleanup_new_spot_base(
        broker, client, symbol="BTC/USDT", base_asset="BTC",
        initial_free_base=Decimal("0"), drill_base_amount=Decimal("0.0005"),
        min_amount=Decimal("0.0001"), min_cost=Decimal("10"),
        stop_id=None, stop_client_id=None,
    )
    assert broker.sells == []
    assert result["ok"] is True
    assert result["remaining_base"] == "0.0005"
    assert result["residual_dust"] == "0.0005"


def test_cleanup_never_sells_unrelated_balance_credit_above_this_drills_fill():
    class Client:
        def __init__(self):
            self.base_free = Decimal("1.5")
        def fetch_balance(self):
            return {"free": {"BTC": str(self.base_free)}}

    class Broker:
        def __init__(self, client):
            self.client = client
            self.sells = []
        def normalize_amount(self, symbol, amount):
            return str(int(amount * 1_000_000) / 1_000_000)
        def market_order(self, symbol, side, amount, client_order_id):
            self.sells.append(Decimal(str(amount)))
            self.client.base_free -= Decimal(str(amount))
            return {"id": "sell-bounded"}
        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": "closed", "filled": str(self.sells[-1])}

    client = Client()
    broker = Broker(client)
    result = cleanup_new_spot_base(
        broker, client, symbol="BTC/USDT", base_asset="BTC",
        initial_free_base=Decimal("1.0"), drill_base_amount=Decimal("0.2"),
        min_amount=Decimal("0.001"), min_cost=Decimal("0"),
        stop_id=None, stop_client_id=None,
    )
    assert broker.sells == [Decimal("0.2")]
    assert client.base_free == Decimal("1.3")
    assert result["ok"] is True
    # The extra 0.3 BTC credit belongs to neither the initial balance nor this drill.
    assert result["remaining_base"] == "0"


def test_net_filled_base_subtracts_fees_charged_in_base_asset():
    order = {
        "filled": "0.25",
        "fees": [
            {"currency": "BTC", "cost": "0.0004"},
            {"currency": "USDT", "cost": "0.01"},
        ],
    }
    assert _net_filled_base(order, "BTC") == Decimal("0.2496")
