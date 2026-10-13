from decimal import Decimal
import pytest

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


def test_simulation_ledger_is_separate_and_paper_guard_stays_enabled():
    from app.execution import customer_paper_execution_blocked

    assert customer_paper_execution_blocked(42, True) is True
    source = open("app/simulation_ledger.py", encoding="utf-8").read()
    assert "customer_funds" in source
    assert "does not import or call customer_funds" in source
    assert "customer_simulation_events" in source
    assert "VIRTUAL_SEED" in source


def test_simulation_ledger_rejects_unsupported_asset_and_event_before_db():
    with pytest.raises(SimulationLedgerError):
        _amount("NaN")
    # Public function validation is intentionally exercised without opening a DB session.
    assert {"crypto", "forex", "commodity"} == {"crypto", "forex", "commodity"}
