from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.binance_spot_testnet_acceptance import (
    _market_order_min_notional_rule,
    _market_order_reference_price,
    _cancel_stop_if_present,
    _net_filled_base,
    _reconcile_buy_order,
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
            return {"id": order_id, "status": "canceled"}

        def fetch_open_orders(self, symbol):
            return []

        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": "canceled" if order_id in self.cancelled else "open"}

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
        initial_free_base=Decimal("1.0"),
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
            return {"id": order_id, "status": "canceled"}

        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": "canceled" if order_id in self.cancelled else "open"}

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
        initial_free_base=Decimal("1.0"),
        min_amount=0.001,
        drill_base_amount=Decimal("1.0"),
        min_cost=Decimal("0"),
        stop_id=None,
        stop_client_id="stop-client-2",
    )
    assert client.cancelled == ["stop-2"]
    assert result["ok"] is True
    assert result["remaining_base"] == "0"


def test_cleanup_resolves_terminal_stop_by_client_id_when_open_orders_are_stale():
    class Client:
        def __init__(self):
            self.base_free = Decimal("1.0")
            self.lookups = []
            self.open_order_queries = 0

        def fetch_order(self, order_id, symbol, params=None):
            self.lookups.append((order_id, params))
            if order_id is None and params == {"origClientOrderId": "stop-terminal-client"}:
                return {"id": "stop-terminal", "status": "canceled"}
            if order_id == "stop-terminal":
                return {"id": order_id, "status": "canceled"}
            raise AssertionError(f"unexpected order lookup: {order_id!r}, {params!r}")

        def fetch_open_orders(self, symbol):
            self.open_order_queries += 1
            return []

        def fetch_balance(self):
            return {"free": {"BTC": str(self.base_free)}}

    class Broker:
        def __init__(self, client):
            self.client = client
            self.sells = []

        def normalize_amount(self, symbol, amount):
            return str(amount)

        def market_order(self, symbol, side, amount, client_order_id):
            self.sells.append(Decimal(str(amount)))
            self.client.base_free -= Decimal(str(amount))
            return {"id": "sell-terminal-stop"}

        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": "closed", "filled": str(self.sells[-1])}

    client = Client()
    broker = Broker(client)
    result = cleanup_new_spot_base(
        broker, client, symbol="BTC/USDT", base_asset="BTC",
        initial_free_base=Decimal("0"), drill_base_amount=Decimal("1.0"),
        min_amount=Decimal("0.001"), min_cost=Decimal("0"),
        stop_id=None, stop_client_id="stop-terminal-client",
    )

    assert client.lookups[0] == (None, {"origClientOrderId": "stop-terminal-client"})
    assert result["ok"] is True
    assert client.base_free == Decimal("0.0")
    assert broker.sells == [Decimal("1.0")]


def test_main_requires_explicit_exclusive_testnet_account_attestation(monkeypatch):
    from scripts import binance_spot_testnet_acceptance as drill

    monkeypatch.setenv("ATLAS_BINANCE_TESTNET", "true")
    monkeypatch.delenv("ATLAS_TESTNET_ACCOUNT_EXCLUSIVE", raising=False)
    with pytest.raises(SystemExit, match="ATLAS_TESTNET_ACCOUNT_EXCLUSIVE=true"):
        drill.main()


def test_binance_spot_harness_requires_opt_in_sandbox_and_verified_cleanup_boundary():
    source = (ROOT / "scripts/binance_spot_testnet_acceptance.py").read_text(encoding="utf-8")
    assert 'os.getenv("ATLAS_BINANCE_TESTNET", "").strip().lower() != "true"' in source
    assert 'os.getenv("ATLAS_TESTNET_ACCOUNT_EXCLUSIVE", "").strip().lower() != "true"' in source
    assert "client.fetch_open_orders(symbol)" in source
    assert "free_delta != drill_cap" in source
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


def test_cleanup_refuses_ambiguous_balance_increase_instead_of_selling_unrelated_credit():
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
    # The delta includes 0.3 BTC that did not come from this drill; fail closed.
    assert broker.sells == []
    assert client.base_free == Decimal("1.5")
    assert result["ok"] is False
    assert any("refusing to sell potentially unrelated base" in error for error in result["errors"])


def test_net_filled_base_subtracts_fees_charged_in_base_asset():
    order = {
        "filled": "0.25",
        "fees": [
            {"currency": "BTC", "cost": "0.0004"},
            {"currency": "USDT", "cost": "0.01"},
        ],
    }
    assert _net_filled_base(order, "BTC") == Decimal("0.2496")


def test_cleanup_refuses_to_sell_when_balance_delta_exceeds_drill_fill():
    class FakeClient:
        def __init__(self):
            self.base_free = Decimal("1.2")
            self.sells = []

        def fetch_balance(self):
            return {"free": {"BTC": str(self.base_free)}}

        def fetch_open_orders(self, symbol):
            return []

    class FakeBroker:
        def __init__(self, client):
            self.client = client
            self.sells = []

        def normalize_amount(self, symbol, amount):
            return str(amount)

        def market_order(self, symbol, side, amount, client_order_id):
            self.sells.append((side, Decimal(str(amount))))
            return {"id": "should-not-sell"}

    client = FakeClient()
    broker = FakeBroker(client)
    result = cleanup_new_spot_base(
        broker,
        client,
        symbol="BTC/USDT",
        base_asset="BTC",
        initial_free_base=Decimal("0"),
        drill_base_amount=Decimal("1.0"),
        min_amount=Decimal("0.001"),
        stop_id=None,
        stop_client_id=None,
    )

    assert result["ok"] is False
    assert broker.sells == []
    assert any("above this drill's net filled amount" in e for e in result["errors"])


def test_cleanup_requires_terminal_stop_state_not_just_missing_open_order():
    class FakeClient:
        def __init__(self):
            self.base_free = Decimal("1")
            self.cancelled = []

        def cancel_order(self, order_id, symbol):
            self.cancelled.append(order_id)
            return {"id": order_id, "status": "open"}

        def fetch_open_orders(self, symbol):
            # A stale/lagging listing is not proof that the stop is gone.
            return []

        def fetch_order(self, order_id, symbol):
            return {"id": order_id, "status": "open"}

        def fetch_balance(self):
            return {"free": {"BTC": str(self.base_free)}}

    class FakeBroker:
        def __init__(self):
            self.sells = []

        def normalize_amount(self, symbol, amount):
            return str(amount)

        def market_order(self, symbol, side, amount, client_order_id):
            self.sells.append(amount)
            return {"id": "should-not-sell"}

    client = FakeClient()
    broker = FakeBroker()
    result = cleanup_new_spot_base(
        broker,
        client,
        symbol="BTC/USDT",
        base_asset="BTC",
        initial_free_base=Decimal("0"),
        drill_base_amount=Decimal("1"),
        min_amount=Decimal("0.001"),
        stop_id="stop-still-open",
        stop_client_id="client-stop",
    )
    assert result["ok"] is False
    assert broker.sells == []
    assert "refusing to flatten while a stop may remain live" in " ".join(result["errors"])


def test_cleanup_classifies_below_minimum_notional_as_dust():
    class FakeClient:
        def fetch_balance(self):
            return {"free": {"BTC": "0.1"}}

        def fetch_open_orders(self, symbol):
            return []

    class FakeBroker:
        def __init__(self):
            self.sells = []

        def ticker(self, symbol):
            return {"last": "100"}

        def normalize_amount(self, symbol, amount):
            return str(amount)

        def market_order(self, symbol, side, amount, client_order_id):
            self.sells.append(amount)
            return {"id": "unexpected-sell"}

    client = FakeClient()
    broker = FakeBroker()
    result = cleanup_new_spot_base(
        broker,
        client,
        symbol="BTC/USDT",
        base_asset="BTC",
        initial_free_base=Decimal("0"),
        drill_base_amount=Decimal("0.1"),
        min_amount=Decimal("0.001"),
        min_cost=Decimal("15"),
        min_cost_reference_mins=0,
        stop_id=None,
        stop_client_id=None,
    )
    assert result["ok"] is True
    assert broker.sells == []
    assert result["remaining_base"] == "0.1"
    assert result["residual_dust"] == "0.1"


def test_dedicated_testnet_account_must_start_without_base_holdings():
    from scripts.binance_spot_testnet_acceptance import require_empty_starting_base

    require_empty_starting_base(Decimal("0"), "BTC")
    with pytest.raises(RuntimeError, match="dedicated account with zero base balance"):
        require_empty_starting_base(Decimal("0.001"), "BTC")


def test_ambiguous_buy_submission_can_be_reconciled_by_client_order_id():
    class FakeClient:
        def __init__(self):
            self.calls = []

        def fetch_order(self, order_id, symbol, params):
            self.calls.append((order_id, symbol, params))
            return {"id": "buy-recovered", "status": "closed", "filled": "0.5", "fee": {"currency": "BTC", "cost": "0.001"}}

    class FakeBroker:
        def fetch_order(self, order_id, symbol):
            assert order_id == "buy-recovered"
            return {"id": order_id, "status": "closed", "filled": "0.5", "fee": {"currency": "BTC", "cost": "0.001"}}

    client = FakeClient()
    order_id, state, cap = _reconcile_buy_order(
        FakeBroker(),
        client,
        symbol="BTC/USDT",
        base_asset="BTC",
        client_order_id="atlas-tn-ambiguous",
        order_id=None,
    )
    assert client.calls == [(None, "BTC/USDT", {"origClientOrderId": "atlas-tn-ambiguous"})]
    assert order_id == "buy-recovered"
    assert state["status"] == "closed"
    assert cap == Decimal("0.499")


def test_partial_buy_is_canceled_and_final_fill_is_preserved(monkeypatch):
    from scripts import binance_spot_testnet_acceptance as drill

    monkeypatch.setattr(drill.time, "sleep", lambda _: None)

    class FakeClient:
        def __init__(self):
            self.canceled = []

        def cancel_order(self, order_id, symbol):
            self.canceled.append(order_id)
            return {"id": order_id, "status": "canceled"}

    class FakeBroker:
        def __init__(self):
            self.calls = 0

        def fetch_order(self, order_id, symbol):
            self.calls += 1
            if self.calls <= 2:
                return {"id": order_id, "status": "open", "filled": "0.4"}
            return {"id": order_id, "status": "canceled", "filled": "0.4", "fee": {"currency": "BTC", "cost": "0.01"}}

    client, broker = FakeClient(), FakeBroker()
    order_id, state, cap = _reconcile_buy_order(
        broker,
        client,
        symbol="BTC/USDT",
        base_asset="BTC",
        client_order_id="atlas-tn-partial",
        order_id="buy-partial",
    )
    assert order_id == "buy-partial"
    assert client.canceled
    assert state["status"] == "canceled"
    assert cap == Decimal("0.39")


def test_market_order_min_notional_uses_filter_reference_window():
    market = {
        "limits": {"cost": {"min": 5}},
        "info": {"filters": [
            {"filterType": "MIN_NOTIONAL", "minNotional": "10", "applyToMarket": True, "avgPriceMins": 5},
            {"filterType": "NOTIONAL", "minNotional": "20", "applyMinToMarket": True, "avgPriceMins": 5},
            {"filterType": "MIN_NOTIONAL", "minNotional": "100", "applyToMarket": False, "avgPriceMins": 0},
        ]},
    }
    assert _market_order_min_notional_rule(market) == [
        (Decimal("10"), 5),
        (Decimal("20"), 5),
    ]


def test_inactive_market_notional_filters_do_not_reappear_through_generic_cost_limit():
    market = {
        "limits": {"cost": {"min": 25}},
        "info": {"filters": [
            {"filterType": "MIN_NOTIONAL", "minNotional": "25", "applyToMarket": False, "avgPriceMins": 5},
            {"filterType": "NOTIONAL", "minNotional": "30", "applyMinToMarket": False, "avgPriceMins": 1},
        ]},
    }
    assert _market_order_min_notional_rule(market) == []


def test_market_order_min_notional_preserves_distinct_reference_windows():
    market = {
        "limits": {"cost": {"min": 5}},
        "info": {
            "filters": [
                {"filterType": "MIN_NOTIONAL", "minNotional": "10", "applyToMarket": True, "avgPriceMins": 0},
                {"filterType": "NOTIONAL", "minNotional": "20", "applyMinToMarket": True, "avgPriceMins": 5},
                {"filterType": "NOTIONAL", "minNotional": "99", "applyMinToMarket": False, "avgPriceMins": 1},
            ]
        },
    }
    assert _market_order_min_notional_rule(market) == [
        (Decimal("10"), 0),
        (Decimal("20"), 5),
    ]


def test_market_order_reference_price_requires_matching_average_window():
    class FakeClient:
        def publicGetAvgPrice(self, params):
            assert params == {"symbol": "BTCUSDT"}
            return {"mins": 5, "price": "101.25"}

        def market(self, symbol):
            return {"id": "BTCUSDT"}

    client = FakeClient()
    assert _market_order_reference_price(client, "BTC/USDT", 5, Decimal("99")) == Decimal("101.25")
    assert _market_order_reference_price(client, "BTC/USDT", 1, Decimal("99")) is None


def test_unknown_market_reference_never_certifies_residual_as_dust():
    class FakeClient:
        def fetch_balance(self):
            return {"free": {"BTC": "0.1"}}

        def fetch_open_orders(self, symbol):
            return []

    class FakeBroker:
        def ticker(self, symbol):
            return {"last": "100"}

        def normalize_amount(self, symbol, amount):
            return str(amount)

        def market_order(self, symbol, side, amount, client_order_id):
            raise RuntimeError("exchange reference window could not be established")

    result = cleanup_new_spot_base(
        FakeBroker(),
        FakeClient(),
        symbol="BTC/USDT",
        base_asset="BTC",
        initial_free_base=Decimal("0"),
        drill_base_amount=Decimal("0.1"),
        min_amount=Decimal("0.001"),
        min_cost=Decimal("15"),
        min_cost_reference_mins=None,
        stop_id=None,
        stop_client_id=None,
    )
    assert result["ok"] is False
    assert result["remaining_base"] == "0.1"
    assert result["residual_dust"] is None
    assert any("unverified" in e for e in result["errors"])



def _configure_main_fake(monkeypatch, scenario):
    from scripts import binance_spot_testnet_acceptance as drill

    monkeypatch.setenv("ATLAS_BINANCE_TESTNET", "true")
    monkeypatch.setenv("ATLAS_TESTNET_ACCOUNT_EXCLUSIVE", "true")
    monkeypatch.setenv("ATLAS_TESTNET_BINANCE_API_KEY", "test-key")
    monkeypatch.setenv("ATLAS_TESTNET_BINANCE_API_SECRET", "test-secret")
    state = {
        "balance": Decimal("0"),
        "buy_client_id": None,
        "buy_status": "open",
        "buy_filled": Decimal("0"),
        "buy_amount": Decimal("0"),
        "sell_amount": Decimal("0"),
        "canceled": [],
        "stop_id": None,
        "stop_client_id": None,
        "stop_cancelled": False,
        "buy_fetch_calls": 0,
    }

    class FakeClient:
        urls = {
            "api": {
                "public": "https://testnet.binance.vision/api/v3",
                "private": "https://testnet.binance.vision/api/v3",
            }
        }

        def fetch_balance(self):
            return {"free": {"BTC": str(state["balance"]), "USDT": "1000"}}

        def fetch_order(self, order_id, symbol, params=None):
            # The ambiguous-submission path deliberately calls fetch_order(None, ...);
            # resolve the retained client ID for both buy and protective-stop orders.
            if order_id is not None and order_id == state["stop_id"]:
                return {"id": order_id, "status": "canceled" if state["stop_cancelled"] else "open"}
            if params and params.get("origClientOrderId") == state["buy_client_id"]:
                return {"id": "buy-1", "status": state["buy_status"], "filled": str(state["buy_filled"])}
            if params and params.get("origClientOrderId") == state["stop_client_id"] and state["stop_id"]:
                return {"id": state["stop_id"], "status": "canceled" if state["stop_cancelled"] else "open"}
            raise AssertionError(f"unexpected raw client order lookup: {order_id!r}, {params!r}")

        def fetch_open_orders(self, symbol):
            if state["stop_id"] and not state["stop_cancelled"]:
                return [{"id": state["stop_id"], "clientOrderId": state["stop_client_id"], "info": {}}]
            return []

        def create_order(self, symbol, order_type, side, amount, price, params):
            state["stop_id"] = "stop-1"
            state["stop_client_id"] = params["clientOrderId"]
            return {"id": state["stop_id"]}

        def cancel_order(self, order_id, symbol):
            state["canceled"].append(order_id)
            if order_id == state["stop_id"]:
                state["stop_cancelled"] = True
                return {"id": order_id, "status": "canceled"}
            assert order_id == "buy-1"
            state["buy_status"] = "canceled"
            return {"id": order_id, "status": "canceled"}

    client = FakeClient()

    class FakeBroker:
        config = SimpleNamespace(sandbox=True)

        def connect(self):
            return client

        def market_info(self, symbol):
            return {
                "limits": {"cost": {"min": "0"}, "amount": {"min": "0.001"}},
                "info": {"orderTypes": ["STOP_LOSS"], "filters": []},
            }

        def ticker(self, symbol):
            return {"last": "100"}

        def normalize_amount(self, symbol, amount):
            return str(Decimal(str(amount)).quantize(Decimal("0.000001")))

        def normalize_price(self, symbol, price):
            return str(Decimal(str(price)).quantize(Decimal("0.01")))

        def market_order(self, symbol, side, amount, client_order_id):
            qty = Decimal(str(amount))
            if side == "buy":
                state["buy_client_id"] = client_order_id
                if scenario == "lost_response":
                    state["buy_status"] = "closed"
                    state["buy_filled"] = qty
                    state["buy_amount"] = qty
                    state["balance"] += qty
                    raise RuntimeError("simulated lost submission response")
                if scenario == "stop_then_reconcile_error":
                    state["buy_status"] = "closed"
                    state["buy_filled"] = qty
                    state["buy_amount"] = qty
                    state["balance"] += qty
                    return {"id": "buy-1"}
                state["buy_status"] = "open"
                state["buy_filled"] = Decimal("0.04")
                state["buy_amount"] = qty
                state["balance"] += Decimal("0.04")
                return {"id": "buy-1"}
            assert side == "sell"
            state["sell_amount"] = qty
            state["balance"] -= qty
            return {"id": "sell-1"}

        def fetch_order(self, order_id, symbol):
            if order_id == "buy-1":
                state["buy_fetch_calls"] += 1
                if scenario == "stop_then_reconcile_error" and state["buy_fetch_calls"] >= 2:
                    raise RuntimeError("simulated reconciliation endpoint outage after stop creation")
                return {
                    "id": "buy-1",
                    "status": state["buy_status"],
                    "filled": str(state["buy_filled"]),
                }
            if order_id == "sell-1":
                return {"id": "sell-1", "status": "closed", "filled": str(state["sell_amount"])}
            raise AssertionError(f"unexpected broker order lookup: {order_id!r}")

    broker = FakeBroker()
    monkeypatch.setattr(
        drill, "build_customer_binance_broker", lambda *args, **kwargs: broker
    )
    if scenario == "partial":
        original_wait = drill._wait_for_order

        def wait_for_partial_then_cleanup(active_broker, order_id, symbol, timeout_seconds=30):
            if order_id == "buy-1":
                return active_broker.fetch_order(order_id, symbol)
            return original_wait(active_broker, order_id, symbol, timeout_seconds)

        monkeypatch.setattr(drill, "_wait_for_order", wait_for_partial_then_cleanup)
        monkeypatch.setattr(drill.time, "sleep", lambda _: None)

    return drill, state


def test_main_reconciles_and_flattens_buy_accepted_before_submission_response_was_lost(monkeypatch):
    drill, state = _configure_main_fake(monkeypatch, "lost_response")

    with pytest.raises(RuntimeError, match="simulated lost submission response"):
        drill.main()

    assert state["buy_client_id"].startswith("atlas-tn-")
    assert state["balance"] == Decimal("0")
    assert state["sell_amount"] == state["buy_amount"]
    assert state["canceled"] == []


def test_main_cancels_protective_stop_when_final_buy_reconciliation_fails(monkeypatch):
    drill, state = _configure_main_fake(monkeypatch, "stop_then_reconcile_error")

    with pytest.raises(RuntimeError, match="could not be verified"):
        drill.main()

    assert state["stop_id"] == "stop-1"
    assert state["stop_cancelled"] is True
    assert "stop-1" in state["canceled"]
    # The buy state was not verified at cleanup time, so no sell may be submitted.
    assert state["sell_amount"] == Decimal("0")
    assert state["balance"] > Decimal("0")


def test_main_cancels_partial_buy_then_flattens_its_confirmed_final_fill(monkeypatch):
    drill, state = _configure_main_fake(monkeypatch, "partial")

    with pytest.raises(RuntimeError, match="did not fill completely"):
        drill.main()

    assert state["canceled"] == ["buy-1"]
    assert state["sell_amount"] == Decimal("0.04")
    assert state["balance"] == Decimal("0")
