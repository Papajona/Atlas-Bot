import asyncio
from decimal import Decimal

import pytest

from app.execution import customer_paper_execution_blocked
from app.simulation_ledger import (
    SimulationLedgerError,
    _amount,
    create_simulation_account,
    post_simulation_event,
)


def test_simulation_amount_rejects_nan_and_infinity():
    with pytest.raises(SimulationLedgerError):
        _amount("NaN")
    with pytest.raises(SimulationLedgerError):
        _amount("Infinity")


def test_simulation_account_rejects_unsupported_asset_before_database_access():
    with pytest.raises(SimulationLedgerError, match="Invalid simulation account identity"):
        asyncio.run(create_simulation_account(42, "equities"))


def test_simulation_event_rejects_unsupported_asset_before_database_access():
    with pytest.raises(SimulationLedgerError, match="Unsupported simulation event"):
        asyncio.run(post_simulation_event(
            customer_id=42,
            account_id=7,
            idempotency_key="test-event",
            event_type="ORDER_FILL",
            balance_delta="-1",
            asset_class="equities",
        ))


def test_simulation_seed_cannot_be_posted_as_an_ordinary_event():
    with pytest.raises(SimulationLedgerError, match="only created with a new account"):
        asyncio.run(post_simulation_event(
            customer_id=42,
            account_id=7,
            idempotency_key="fake-seed",
            event_type="VIRTUAL_SEED",
            balance_delta="10000",
            asset_class="forex",
        ))


def test_simulation_account_requires_nonnegative_virtual_limits():
    with pytest.raises(SimulationLedgerError, match="must be nonnegative"):
        asyncio.run(create_simulation_account(
            42, "forex", initial_virtual_balance=Decimal("-1")
        ))
    with pytest.raises(SimulationLedgerError, match="must be nonnegative"):
        asyncio.run(create_simulation_account(
            42, "forex", daily_loss_limit=Decimal("-1")
        ))


def test_customer_paper_execution_guard_remains_enabled_until_integration_is_verified():
    assert customer_paper_execution_blocked(42, True) is True
    assert customer_paper_execution_blocked(42, False) is False
    assert customer_paper_execution_blocked(None, True) is False
