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
    assert "SELECT id, filled_quantity, fee, mode" in source


def test_remaining_entry_fee_backfill_only_trusts_deterministic_paper_fees():
    reconstruct = MIGRATION["_reconstruct_remaining_entry_fee"]
    remaining = Decimal("1")
    closed = Decimal("1")

    assert reconstruct(
        {"filled_quantity": "2", "fee": "0.40", "mode": "PAPER"},
        remaining,
        closed,
    ) == Decimal("0.20")
    assert reconstruct(
        {"filled_quantity": "2", "fee": "0.40", "mode": "PAPER_SHADOW"},
        remaining,
        closed,
    ) == Decimal("0.20")

    # Historical live fee fields had no source-quality provenance: even a
    # reconciled quantity and non-null fee must not certify an estimated fee.
    assert reconstruct(
        {"filled_quantity": "2", "fee": "0.40", "mode": "LIVE"},
        remaining,
        closed,
    ) is None
    assert reconstruct(
        {"filled_quantity": "2", "fee": "0.40"},
        remaining,
        closed,
    ) is None


def test_completed_learning_episode_is_quarantined_when_historical_fees_are_unknown():
    should_quarantine = MIGRATION["_learning_episode_requires_fee_quarantine"]
    source = (
        ROOT / "alembic" / "versions" / "0061_strategy_outcome_fill_aggregation.py"
    ).read_text(encoding="utf-8")
    assert "SET status = 'FEE_RECONCILIATION_PENDING'" in source
    assert "replay_status = 'PENDING'" in source
    assert "learning_memory_json = '{}'" in source

    assert should_quarantine(
        {"status": "COMPLETED"},
        lifecycle_closed=True,
        fee_basis_complete=False,
    ) is True
    assert should_quarantine(
        {"status": "COMPLETED"},
        lifecycle_closed=True,
        fee_basis_complete=True,
    ) is False
    assert should_quarantine(
        {"status": "OPEN"},
        lifecycle_closed=True,
        fee_basis_complete=False,
    ) is False
    assert should_quarantine(
        {"status": "COMPLETED"},
        lifecycle_closed=False,
        fee_basis_complete=False,
    ) is False


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


def test_reversal_close_quantity_uses_old_position_not_later_add_ons_to_new_leg():
    old_position_episode = {"entry_quantity": "10", "status": "COMPLETED"}
    reversal_order = {"id": 20, "filled_quantity": "12"}
    # The reversal initially opened 2 units, then later add-ons raised the
    # reversal episode to 5. That later total must not determine the old close.
    later_reversal_episode = {"entry_quantity": "5", "status": "OPEN"}

    allocations = MIGRATION["_allocate_lifecycle_closing_quantities"](
        [reversal_order], old_position_episode, lifecycle_closed=True
    )

    assert Decimal(str(later_reversal_episode["entry_quantity"])) == Decimal("5")
    # The allocator, rather than the single-order helper, owns reversal allocation;
    # it reconciles the close against the old completed lifecycle.
    assert allocations == {20: Decimal("10")}
    assert MIGRATION["_closing_order_fee_fraction"](
        reversal_order, allocations[20]
    ) == Decimal("0.8333333333333333333333333333")


def test_reversal_close_quantity_consumes_prior_closes_before_reversal_leg():
    old_position_episode = {"entry_quantity": "10", "status": "COMPLETED"}
    closing_orders = [
        {"id": 10, "filled_quantity": "3"},
        {"id": 20, "filled_quantity": "10"},
    ]

    allocations = MIGRATION["_allocate_lifecycle_closing_quantities"](
        closing_orders, old_position_episode, lifecycle_closed=True
    )

    assert allocations == {
        10: Decimal("3"),
        20: Decimal("7"),
    }
    assert MIGRATION["_closing_order_fee_fraction"](
        closing_orders[1], allocations[20]
    ) == Decimal("0.7")


def test_missing_zero_pnl_closing_order_prevents_completed_lifecycle_eligibility():
    old_position_episode = {"entry_quantity": "10", "status": "COMPLETED"}
    # A zero-PnL close for 2 units is absent. The surviving order records only 8.
    recorded_closing_orders = [
        {"id": 10, "filled_quantity": "3"},
        {"id": 20, "filled_quantity": "5"},
    ]

    assert MIGRATION["_allocate_lifecycle_closing_quantities"](
        recorded_closing_orders,
        old_position_episode,
        lifecycle_closed=True,
    ) is None


def test_missing_zero_pnl_fill_within_reversal_order_fails_per_order_reconciliation():
    old_position_episode = {"entry_quantity": "10", "status": "COMPLETED"}
    closing_orders = [
        {"id": 10, "filled_quantity": "3"},
        {"id": 20, "filled_quantity": "10"},
    ]
    allocations = MIGRATION["_allocate_lifecycle_closing_quantities"](
        closing_orders, old_position_episode, lifecycle_closed=True
    )

    assert allocations == {10: Decimal("3"), 20: Decimal("7")}
    assert MIGRATION["_reconcile_closing_order_fill_evidence"](
        allocations[20], Decimal("5"), Decimal("490")
    ) is False


def test_reversal_entry_fee_basis_is_withheld_when_later_add_ons_hide_original_leg():
    reversal_order = {"filled_quantity": "10"}
    # This quantity includes the original 2-unit reversal leg plus later add-ons.
    reversal_episode = {"entry_quantity": "5", "status": "OPEN"}

    assert MIGRATION["_entry_order_fee_fraction"](
        reversal_order,
        reversal_episode,
        reused_entry_order=True,
    ) is None


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
