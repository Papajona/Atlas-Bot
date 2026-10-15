"""Behavioral regressions for fail-closed exchange position parsing and emergency flattening."""
import asyncio

import app.execution as execution


def test_contract_size_multiplier_is_never_mistaken_for_open_quantity():
    row = {"symbol": "BTC/USDT:USDT", "contractSize": 100, "side": "long"}
    assert execution._position_amount_from_exchange_row(row) is None


def test_explicit_contract_quantity_wins_over_contract_size_multiplier():
    row = {"symbol": "BTC/USDT:USDT", "contracts": "0.5", "contractSize": 100, "side": "long"}
    assert execution._position_amount_from_exchange_row(row) == 0.5


def test_generic_amount_or_quantity_is_not_assumed_to_be_contract_units():
    assert execution._position_amount_from_exchange_row({"symbol": "BTC/USDT:USDT", "amount": "0.5"}) is None
    assert execution._position_amount_from_exchange_row({"symbol": "BTC/USDT:USDT", "quantity": "0.5"}) is None


def test_zero_quantity_is_distinct_from_missing_quantity():
    assert execution._position_amount_from_exchange_row({"symbol": "BTC/USDT:USDT", "contracts": "0"}) == 0.0
    assert execution._position_amount_from_exchange_row({"symbol": "BTC/USDT:USDT"}) is None


def test_conflicting_zero_and_nonzero_position_fields_are_unverifiable():
    row = {
        "symbol": "BTC/USDT:USDT",
        "contracts": "0",
        "info": {"positionAmt": "-0.25"},
    }
    assert execution._position_amount_from_exchange_row(row) is None


def test_malformed_or_non_finite_quantity_does_not_fall_back_to_other_fields():
    assert execution._position_amount_from_exchange_row({
        "symbol": "BTC/USDT:USDT", "contracts": "NaN", "quantity": "0.5", "contractSize": 100
    }) is None
    assert execution._position_amount_from_exchange_row({
        "symbol": "BTC/USDT:USDT", "contracts": "Infinity"
    }) is None


def test_emergency_flatten_refuses_to_guess_when_quantity_is_missing():
    class FakeBroker:
        def __init__(self):
            self.market_orders = []
            self.fetch_calls = 0

        def fetch_positions(self):
            self.fetch_calls += 1
            return [{"symbol": "BTC/USDT:USDT", "contractSize": 100, "side": "long"}]

        def market_order(self, *args):
            self.market_orders.append(args)
            return {"id": "unexpected"}

    broker = FakeBroker()
    failures = []
    canceled = []

    asyncio.run(execution._flatten_exchange_positions(broker, "test", failures, canceled))

    assert broker.market_orders == []
    assert not canceled
    assert any("quantity is missing or invalid" in item.get("error", "") for item in failures)
    assert any("could not be verified" in item.get("error", "") for item in failures)


def test_emergency_flatten_uses_contracts_not_the_contract_size_multiplier():
    class FakeBroker:
        def __init__(self):
            self.market_orders = []
            self.fetch_calls = 0

        def fetch_positions(self):
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                return [{"symbol": "BTC/USDT:USDT", "contracts": "0.5", "contractSize": 100, "side": "long"}]
            return []

        def market_order(self, *args):
            self.market_orders.append(args)
            return {"id": "close-1"}

    broker = FakeBroker()
    failures = []
    canceled = []

    asyncio.run(execution._flatten_exchange_positions(broker, "test", failures, canceled))

    assert len(broker.market_orders) == 1
    assert broker.market_orders[0][0:3] == ("BTC/USDT:USDT", "sell", 0.5)
    assert canceled == [{"scope": "test", "position_symbol": "BTC/USDT:USDT", "order_id": "close-1"}]
    assert failures == []
