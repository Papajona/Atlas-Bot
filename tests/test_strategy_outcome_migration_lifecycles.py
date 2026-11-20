from decimal import Decimal
from pathlib import Path
import runpy


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = runpy.run_path(
    str(ROOT / "alembic" / "versions" / "0061_strategy_outcome_fill_aggregation.py")
)


def test_reused_entry_trade_outcomes_are_split_at_terminal_close_rows():
    rows = [
        {"sequence": 1, "lifecycle_status": "OPEN"},
        {"sequence": 2, "lifecycle_status": "CLOSED"},
        {"sequence": 3, "lifecycle_status": "OPEN"},
        {"sequence": 4, "lifecycle_status": "CLOSED"},
        {"sequence": 5, "lifecycle_status": "OPEN"},
    ]

    groups = MIGRATION["_split_outcome_lifecycles"](rows)

    assert [[row["sequence"] for row in group] for group in groups] == [
        [1, 2],
        [3, 4],
        [5],
    ]


def test_reused_entry_trade_fee_basis_is_not_reconstructed_from_cumulative_order_fee():
    source_trades = {
        7: {"fee": "0.30", "mode": "PAPER"},
        11: {"fee": "0.05", "mode": "PAPER"},
    }
    reconstruct = MIGRATION["_reconstruct_lifecycle_fee_total"]

    assert reconstruct(
        7, [11], source_trades, reused_entry_order=True
    ) is None
    assert reconstruct(
        7, [11], source_trades, reused_entry_order=False
    ) == Decimal("0.35")


def test_remaining_entry_fee_backfill_counts_only_current_open_lifecycle_closes():
    source = (
        ROOT / "alembic" / "versions" / "0061_strategy_outcome_fill_aggregation.py"
    ).read_text(encoding="utf-8")

    assert "AND lifecycle_status = 'OPEN'" in source


def test_open_lifecycle_with_repeated_partial_close_order_is_not_fee_complete():
    rows = [
        {"closing_trade_id": 21},
        {"closing_trade_id": 21},
    ]

    assert MIGRATION["_historical_lifecycle_fee_basis_complete"](
        rows,
        fill_evidence_complete=True,
        fee_total=Decimal("0.30"),
        lifecycle_open=True,
    ) is False


def test_open_lifecycle_with_distinct_close_orders_can_be_fee_complete():
    rows = [
        {"closing_trade_id": 21},
        {"closing_trade_id": 22},
    ]

    assert MIGRATION["_historical_lifecycle_fee_basis_complete"](
        rows,
        fill_evidence_complete=True,
        fee_total=Decimal("0.30"),
        lifecycle_open=True,
    ) is True


def test_completed_lifecycle_reconstructs_a_repeated_partial_order_fee_once():
    rows = [
        {"closing_trade_id": 21},
        {"closing_trade_id": 21},
    ]

    assert MIGRATION["_historical_lifecycle_fee_basis_complete"](
        rows,
        fill_evidence_complete=True,
        fee_total=Decimal("0.30"),
        lifecycle_open=False,
    ) is True
