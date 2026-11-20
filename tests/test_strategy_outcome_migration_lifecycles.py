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
        7: {"fee": "0.30", "mode": "PAPER", "filled_quantity": "2"},
        11: {"fee": "0.05", "mode": "PAPER", "filled_quantity": "1"},
    }
    reconstruct = MIGRATION["_reconstruct_lifecycle_fee_total"]
    common = {
        "entry_fee_fraction": Decimal("1"),
        "closing_fee_fractions": {11: Decimal("1")},
    }

    assert reconstruct(
        7, [11], source_trades, reused_entry_order=True, **common
    ) is None
    assert reconstruct(
        7, [11], source_trades, reused_entry_order=False, **common
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


def test_reversal_close_quantity_excludes_the_new_opposite_position_leg():
    order = {"filled_quantity": "8"}
    reversal_episode = {"entry_quantity": "3", "status": "OPEN"}

    assert MIGRATION["_expected_closing_order_quantity"](
        order, reversal_episode
    ) == Decimal("5")


def test_reversal_close_fee_and_new_entry_fee_are_prorated_without_overlap():
    source_trades = {
        7: {"fee": "0.20", "mode": "PAPER"},
        11: {"fee": "0.80", "mode": "PAPER"},
    }
    reversal_order = {"filled_quantity": "8"}
    reversal_episode = {"entry_quantity": "3", "status": "OPEN"}
    expected_close = MIGRATION["_expected_closing_order_quantity"](
        reversal_order, reversal_episode
    )
    close_fraction = MIGRATION["_closing_order_fee_fraction"](
        reversal_order, reversal_episode
    )
    entry_fraction = MIGRATION["_entry_order_fee_fraction"](
        reversal_order, reversal_episode, reused_entry_order=False
    )

    old_position_fee = MIGRATION["_reconstruct_lifecycle_fee_total"](
        7,
        [11],
        source_trades,
        reused_entry_order=False,
        entry_fee_fraction=Decimal("1"),
        closing_fee_fractions={11: close_fraction},
    )
    new_position_fee = MIGRATION["_reconstruct_lifecycle_fee_total"](
        11,
        [],
        source_trades,
        reused_entry_order=False,
        entry_fee_fraction=entry_fraction,
        closing_fee_fractions={},
    )

    assert expected_close == Decimal("5")
    assert close_fraction == Decimal("0.625")
    assert entry_fraction == Decimal("0.375")
    assert old_position_fee == Decimal("0.700")
    assert new_position_fee == Decimal("0.300")
    assert old_position_fee + new_position_fee - Decimal("0.20") == Decimal("0.80")


def test_invalid_or_missing_reversal_quantity_fails_closed_for_old_close():
    order = {"filled_quantity": "8"}

    assert MIGRATION["_expected_closing_order_quantity"](
        order, {"entry_quantity": "9", "status": "OPEN"}
    ) is None
    assert MIGRATION["_expected_closing_order_quantity"](
        order, {"entry_quantity": "3", "status": "UNKNOWN"}
    ) is None


def test_unverified_fee_reconstruction_preserves_existing_historical_fee():
    persist = MIGRATION["_fee_value_to_persist"]

    assert persist(
        Decimal("0"),
        Decimal("0.85"),
        fee_basis_complete=False,
    ) == Decimal("0.85")
    assert persist(
        Decimal("0.45"),
        Decimal("0.85"),
        fee_basis_complete=True,
    ) == Decimal("0.45")


def test_cross_role_reused_order_ids_are_not_reconstructed_from_latest_episode():
    role_rows = [
        {"trade_id": 100, "closing_trade_id": 200},
        {"trade_id": 200, "closing_trade_id": None},
        {"trade_id": 300, "closing_trade_id": 400},
    ]

    assert MIGRATION["_cross_role_reused_order_ids"](
        role_rows, {100, 200, 300, 400}
    ) == {200}


def test_unreused_order_ids_remain_eligible_for_reconciliation():
    role_rows = [
        {"trade_id": 100, "closing_trade_id": 200},
        {"trade_id": 300, "closing_trade_id": 400},
    ]

    assert MIGRATION["_cross_role_reused_order_ids"](
        role_rows, {100, 200, 300, 400}
    ) == set()
